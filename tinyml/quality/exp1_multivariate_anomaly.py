"""Experiment 1: Multivariate Anomaly Detection on Normal Water-Quality Readings.

Compares:
  1. Fixed regulatory thresholds (TrinkwV for GECCO, IS 10500 for Kenya)
  2. Univariate Z-score baseline (max |z_i|, no cross-feature correlation)
  3. Mahalanobis distance (linear multivariate covariance baseline)
  4. Isolation Forest (sklearn, 50 trees)
  5. TinyML Autoencoder (4 -> 8 -> 2 -> 8 -> 4, float32 + int8 quantized)

Evaluates on:
  A. GECCO 2019 real utility data (train on first 50% normal, test on second 50% + labelled events)
  B. Kenya Safe + In-Range Correlated Anomalies (strictly inside IS 10500 limits)
  C. Edge suitability (parameter bytes and host inference time per reading)

Writes tinyml/quality/exp1_results.txt.
"""
import csv
import math
import os
import pickle
import time

import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.metrics import roc_auc_score

HERE = os.path.dirname(os.path.abspath(__file__))
from tinyml.quality.quality_model import check_standards  # noqa: E402

COND_TO_TDS = 0.65
rng = np.random.default_rng(42)


def load_gecco():
    with open(os.path.join(HERE, "data", "gecco", "5_gecco2019_all_water_quality.csv")) as f:
        rows = [r for r in csv.DictReader(f) if "NA" not in (r["Tp"], r["pH"], r["Cond"], r["Turb"])]
    x = np.array(
        [[float(r["Tp"]), float(r["pH"]), float(r["Cond"]) * 1e4 * COND_TO_TDS, float(r["Turb"])] for r in rows]
    )
    ev = np.array([r["Event"] == "TRUE" for r in rows])
    return x, ev


def load_kenya_safe():
    with open(os.path.join(HERE, "data", "kenya", "water_quality_dataset1.csv")) as f:
        rows = [r for r in csv.DictReader(f) if r["Label"] == "Safe"]
    return np.array(
        [[float(r["Temperature_C"]), float(r["pH"]), float(r["TDS_ppm"]), float(r["Turbidity_NTU"])] for r in rows]
    )


def feats(x):
    return np.column_stack(
        [x[:, 0], x[:, 1], np.log1p(np.maximum(x[:, 2], 0)), np.log1p(np.maximum(x[:, 3], 0))]
    )


# ------------------------------------------------------------------ TinyML Autoencoder (4->8->2->8->4)
class TinyAutoencoder:
    def __init__(self, sizes=(4, 8, 2, 8, 4)):
        self.sizes = sizes
        self.W = [rng.normal(0, math.sqrt(2 / a), (a, b)) for a, b in zip(sizes[:-1], sizes[1:])]
        self.B = [np.zeros(b) for b in sizes[1:]]

    def forward(self, z):
        acts = [z]
        for l, (w, b) in enumerate(zip(self.W, self.B)):
            a = acts[-1] @ w + b
            acts.append(np.maximum(a, 0) if l < len(self.W) - 1 else a)
        return acts

    def fit(self, z, epochs=80, lr=0.01, batch=128):
        params = self.W + self.B
        m1 = [np.zeros_like(p) for p in params]
        m2 = [np.zeros_like(p) for p in params]
        t = 0
        for _ in range(epochs):
            order = rng.permutation(len(z))
            for s in range(0, len(z), batch):
                i = order[s : s + batch]
                acts = self.forward(z[i])
                d = 2.0 * (acts[-1] - z[i]) / (len(i) * z.shape[1])
                gW, gB = [None] * len(self.W), [None] * len(self.W)
                for l in range(len(self.W) - 1, -1, -1):
                    gW[l], gB[l] = acts[l].T @ d, d.sum(0)
                    if l > 0:
                        d = (d @ self.W[l].T) * (acts[l] > 0)
                t += 1
                for p, g, a, v in zip(params, gW + gB, m1, m2):
                    a[:] = 0.9 * a + 0.1 * g
                    v[:] = 0.999 * v + 0.001 * g * g
                    p -= lr * (a / (1 - 0.9 ** t)) / (np.sqrt(v / (1 - 0.999 ** t)) + 1e-8)

    def mse(self, z):
        rec = self.forward(z)[-1]
        return np.mean((rec - z) ** 2, axis=1)

    def quantize_int8(self, z_cal):
        """int8 weights plus one fixed activation scale per layer, calibrated once on training data."""
        acts = self.forward(z_cal)
        self.Wq, self.sw, self.sa = [], [], []
        for l, w in enumerate(self.W):
            s = max(np.abs(w).max() / 127.0, 1e-12)
            self.sw.append(s)
            self.sa.append(max(np.percentile(np.abs(acts[l]), 99.9) / 127.0, 1e-12))
            self.Wq.append(np.round(w / s).astype(np.int8))

    def mse_int8(self, z):
        a = z
        for l, (wq, sw, sa, b) in enumerate(zip(self.Wq, self.sw, self.sa, self.B)):
            aq = np.clip(np.round(a / sa), -128, 127).astype(np.int32)
            out = (aq @ wq.astype(np.int32)) * (sa * sw) + b
            a = np.maximum(out, 0) if l < len(self.Wq) - 1 else out
        return np.mean((a - z) ** 2, axis=1)

    def size_bytes(self):
        return sum(w.size for w in self.W) + 4 * sum(b.size for b in self.B)


def score_metrics(y_true, scores, thr):
    pred = scores > thr
    tp = int(((y_true == 1) & (pred == 1)).sum())
    fp = int(((y_true == 0) & (pred == 1)).sum())
    fn = int(((y_true == 1) & (pred == 0)).sum())
    tn = int(((y_true == 0) & (pred == 0)).sum())
    p = tp / max(tp + fp, 1)
    r = tp / max(tp + fn, 1)
    f1 = 2 * p * r / max(p + r, 1e-12)
    fpr = fp / max(fp + tn, 1)
    auc = roc_auc_score(y_true, scores) if len(np.unique(y_true)) == 2 else float("nan")
    return p, r, f1, fpr, auc, pred


def run_benchmark(name, x_tr_norm, x_cal_norm, x_test, y_test, standard_name):
    z_tr = feats(x_tr_norm)
    mu, sd = z_tr.mean(0), np.maximum(z_tr.std(0), 1e-6)
    z_trn = (z_tr - mu) / sd
    z_caln = (feats(x_cal_norm) - mu) / sd
    z_ten = (feats(x_test) - mu) / sd

    # 1. Fixed regulatory threshold baseline
    std_pred = np.array([not check_standards(r[1], r[2], r[3], standard_name)[0] for r in x_test], dtype=int)
    p_s, r_s, f1_s, fpr_s, _, _ = score_metrics(y_test, std_pred.astype(float), 0.5)

    # 2. Univariate Z-score
    uni_cal = np.max(np.abs(z_caln), axis=1)
    uni_te = np.max(np.abs(z_ten), axis=1)

    # 3. Mahalanobis distance
    cov = np.cov(z_trn, rowvar=False) + 1e-5 * np.eye(4)
    inv_cov = np.linalg.inv(cov)
    mah_cal = np.einsum("ij,jk,ik->i", z_caln, inv_cov, z_caln)
    mah_te = np.einsum("ij,jk,ik->i", z_ten, inv_cov, z_ten)

    # 4. Isolation Forest (50 trees)
    iso = IsolationForest(n_estimators=50, random_state=42, n_jobs=-1)
    iso.fit(z_trn)
    iso_cal = -iso.score_samples(z_caln)
    iso_te = -iso.score_samples(z_ten)

    # 5. TinyML Autoencoder (float32 + int8)
    ae = TinyAutoencoder((4, 8, 2, 8, 4))
    ae.fit(z_trn, epochs=80, lr=0.01)
    ae.quantize_int8(z_trn)
    ae_cal = ae.mse(z_caln)
    ae_te = ae.mse(z_ten)
    ae8_cal = ae.mse_int8(z_caln)
    ae8_te = ae.mse_int8(z_ten)

    lines = [f"=== {name} (N_test={len(y_test)}, anomalies={int(y_test.sum())}) ==="]
    lines.append(f"  {'Detector':28s} | {'P':>6s} {'R':>6s} {'F1':>6s} {'FPR':>6s} {'AUC':>6s}")
    lines.append("  " + "-" * 68)
    lines.append(f"  {f'Fixed Threshold ({standard_name})':28s} | {p_s:6.3f} {r_s:6.3f} {f1_s:6.3f} {fpr_s:6.3f} {'n/a':>6s}")

    # Calibrate threshold at 98th percentile of held-out normal (target FPR ~ 2%)
    preds_map = {}
    for dname, cal_s, te_s in (
        ("Univariate Z-score", uni_cal, uni_te),
        ("Mahalanobis Distance", mah_cal, mah_te),
        ("Isolation Forest (50 trees)", iso_cal, iso_te),
        ("TinyML Autoencoder (float32)", ae_cal, ae_te),
        ("TinyML Autoencoder (int8)", ae8_cal, ae8_te),
    ):
        thr = np.quantile(cal_s, 0.98)
        p, r, f1, fpr, auc, pred = score_metrics(y_test, te_s, thr)
        preds_map[dname] = pred
        lines.append(f"  {dname:28s} | {p:6.3f} {r:6.3f} {f1:6.3f} {fpr:6.3f} {auc:6.3f}")

    # Event-level recall if contiguous events exist (GECCO)
    if y_test.sum() > 0 and "GECCO" in name:
        ev_bool = y_test.astype(bool)
        starts = np.flatnonzero(ev_bool & ~np.r_[False, ev_bool[:-1]])
        ends = np.flatnonzero(ev_bool & ~np.r_[ev_bool[1:], False])
        lines.append(f"  Event-level recall ({len(starts)} test events):")
        lines.append(f"    Fixed Threshold ({standard_name}): {sum(np.any(std_pred[a:b+1]) for a,b in zip(starts,ends))}/{len(starts)}")
        for dname, pred in preds_map.items():
            hits = sum(np.any(pred[a:b + 1]) for a, b in zip(starts, ends))
            lines.append(f"    {dname:28s}: {hits}/{len(starts)}")

    # Edge suitability timings on 2000 samples
    if "GECCO" in name:
        t1 = time.perf_counter()
        ae.mse_int8(z_ten[:2000])
        ae_us = (time.perf_counter() - t1) / 2000 * 1e6
        t2 = time.perf_counter()
        -iso.score_samples(z_ten[:2000])
        iso_us = (time.perf_counter() - t2) / 2000 * 1e6
        iso_bytes = len(pickle.dumps(iso))
        lines.append("\n  Edge Suitability:")
        lines.append(f"    TinyML Autoencoder (int8): {ae.size_bytes()} bytes (weights+biases), {ae_us:.1f} us/sample")
        lines.append(f"    Isolation Forest (50 trees): {iso_bytes/1024:.1f} KB pickled, {iso_us:.1f} us/sample")
        lines.append(f"    Mahalanobis Distance: {4*4*4 + 4*4} bytes (cov+mean), <1 us/sample")
    return lines


def main():
    out = []
    # A. GECCO 2019: train on first 40% normal, calibrate on next 10% normal, test on last 50% (with events)
    gx, gev = load_gecco()
    mid = len(gx) // 2
    norm_first_half = gx[:mid][~gev[:mid]]
    c_idx = int(0.8 * len(norm_first_half))
    out += run_benchmark(
        "A. GECCO 2019 Real Utility (Test = second 50%)",
        norm_first_half[:c_idx],
        norm_first_half[c_idx:],
        gx[mid:],
        gev[mid:].astype(int),
        "TRINKW_V",
    )

    # B. Kenya Safe + In-Range Multivariate Anomalies (strictly inside IS 10500 limits)
    ksafe = load_kenya_safe()
    perm = rng.permutation(len(ksafe))
    ksafe = ksafe[perm]
    n_tr = int(0.6 * len(ksafe))
    n_cal = int(0.8 * len(ksafe))
    x_tr, x_cal, x_te_norm = ksafe[:n_tr], ksafe[n_tr:n_cal], ksafe[n_cal:]

    # Build in-range correlated anomalies: values inside IS 10500 (pH 6.55-8.45, TDS 20-495, Turb 0.1-4.9)
    # but breaking normal correlations (e.g. high TDS + low pH + elevated turbidity simultaneously, or
    # extreme corner combinations absent in normal tap water)
    n_anom = len(x_te_norm)
    x_anom = x_te_norm.copy()
    x_anom[:, 0] = rng.uniform(15.0, 32.0, n_anom)
    x_anom[:, 1] = rng.choice([6.55, 8.45], n_anom) + rng.normal(0, 0.03, n_anom)
    x_anom[:, 1] = np.clip(x_anom[:, 1], 6.52, 8.48)
    x_anom[:, 2] = rng.uniform(380.0, 495.0, n_anom)
    x_anom[:, 3] = rng.uniform(2.5, 4.9, n_anom)
    x_te = np.vstack([x_te_norm, x_anom])
    y_te = np.array([0] * len(x_te_norm) + [1] * len(x_anom))

    out.append("")
    out += run_benchmark(
        "B. Kenya Safe + In-Range Correlated Anomalies (all inside IS 10500)",
        x_tr,
        x_cal,
        x_te,
        y_te,
        "IS_10500",
    )

    txt = "\n".join(out)
    print(txt)
    with open(os.path.join(HERE, "exp1_results.txt"), "w") as f:
        f.write(txt + "\n")


if __name__ == "__main__":
    main()
