"""Experiment 2: Temporal Anomaly Detection on Water-Quality Sequences.

Examines sequences over time (sudden jumps, gradual deterioration, unusual combinations
of changing parameters, persistent deviations) on GECCO 2019 (train = first 50% normal,
test = second 50% with 28 real utility events).

Compares:
  1. Moving-Average Residual (Rolling EWMA Z-score)
  2. Change-Point Detection (Two-sided CUSUM on rolling EWMA residuals)
  3. Lightweight Temporal TinyML Autoencoder (8 -> 12 -> 4 -> 12 -> 8, float32 + int8)
     operating on [short-term delta (15-min), persistent deviation (12-h)] across all 4 sensors.

Reports: Point-wise P/R/F1, Normal FPR, ROC-AUC, Event-level Recall, Median Detection Latency,
and Edge Footprint. Writes tinyml/exp2_results.txt.
"""
import csv
import math
import os
import sys
import time

import numpy as np
from sklearn.metrics import roc_auc_score

HERE = os.path.dirname(os.path.abspath(__file__))
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


def feats(x):
    return np.column_stack(
        [x[:, 0], x[:, 1], np.log1p(np.maximum(x[:, 2], 0)), np.log1p(np.maximum(x[:, 3], 0))]
    )


def ewma_series(z, alpha):
    """Causal EWMA over time axis 0."""
    out = np.empty_like(z)
    cur = z[0].copy()
    for i in range(len(z)):
        cur = (1.0 - alpha) * cur + alpha * z[i]
        out[i] = cur
    return out


def rolling_ewma_z(z, alpha_mean=1 / 720, alpha_var=1 / 720):
    """Causal rolling EWMA mean and std -> per-feature z-score residuals."""
    res = np.zeros_like(z)
    mu = z[0].copy()
    var = np.ones(z.shape[1]) * 0.05
    for i in range(len(z)):
        diff = z[i] - mu
        sd = np.sqrt(np.maximum(var, 1e-4))
        res[i] = diff / sd
        # Robustly clamp update so extreme spikes don't corrupt rolling baseline
        clipped = np.clip(diff, -3.0 * sd, 3.0 * sd)
        mu = (1.0 - alpha_mean) * mu + alpha_mean * (mu + clipped)
        var = (1.0 - alpha_var) * var + alpha_var * (clipped ** 2)
    return res


def cusum_scores(z_res, k=1.5):
    """Two-sided multivariate CUSUM on rolling z-residuals."""
    scores = np.zeros(len(z_res))
    c = np.zeros(z_res.shape[1])
    for i in range(len(z_res)):
        c = np.maximum(0.0, c + np.abs(z_res[i]) - k)
        scores[i] = np.max(c)
    return scores


class TemporalAE:
    def __init__(self, sizes=(8, 12, 4, 12, 8)):
        self.W = [rng.normal(0, math.sqrt(2 / a), (a, b)) for a, b in zip(sizes[:-1], sizes[1:])]
        self.B = [np.zeros(b) for b in sizes[1:]]

    def forward(self, z):
        acts = [z]
        for l, (w, b) in enumerate(zip(self.W, self.B)):
            a = acts[-1] @ w + b
            acts.append(np.maximum(a, 0) if l < len(self.W) - 1 else a)
        return acts

    def fit(self, z, epochs=60, lr=0.01, batch=256):
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

    def quantize_int8(self, z_cal):
        acts = self.forward(z_cal)
        self.Wq, self.sw, self.sa = [], [], []
        for l, w in enumerate(self.W):
            s = max(np.abs(w).max() / 127.0, 1e-12)
            sa = max(np.percentile(np.abs(acts[l]), 99.9) / 127.0, 1e-12)
            self.sw.append(s)
            self.sa.append(sa)
            self.Wq.append(np.round(w / s).astype(np.int8))

    def mse(self, z):
        return np.mean((self.forward(z)[-1] - z) ** 2, axis=1)

    def mse_int8(self, z):
        a = z
        for l, (wq, sw, sa, b) in enumerate(zip(self.Wq, self.sw, self.sa, self.B)):
            aq = np.clip(np.round(a / sa), -128, 127).astype(np.int32)
            out = (aq @ wq.astype(np.int32)) * (sa * sw) + b
            a = np.maximum(out, 0) if l < len(self.Wq) - 1 else out
        return np.mean((a - z) ** 2, axis=1)

    def size_bytes(self):
        return sum(w.size for w in self.W) + 4 * sum(b.size for b in self.B)


def main():
    gx, gev = load_gecco()
    z = feats(gx)

    # Causal temporal features:
    #   short-term jump (15-min EWMA residual) + persistent deviation (12-h EWMA residual)
    ewma_short = ewma_series(z, alpha=1 / 15)
    ewma_long = ewma_series(z, alpha=1 / 720)
    z_temp = np.hstack([z - ewma_short, z - ewma_long])

    # Rolling EWMA Z-score & CUSUM
    z_res = rolling_ewma_z(z, alpha_mean=1 / 720, alpha_var=1 / 720)
    ma_scores = np.max(np.abs(z_res), axis=1)
    cusum_s = cusum_scores(z_res, k=1.5)

    # Split: first 50% (train 40% normal + calibrate 10% normal), test = second 50%
    mid = len(gx) // 2
    norm_mask_1 = ~gev[:mid]
    z_t1 = z_temp[:mid][norm_mask_1]
    c_idx = int(0.8 * len(z_t1))
    mu, sd = z_t1[:c_idx].mean(0), np.maximum(z_t1[:c_idx].std(0), 1e-6)
    z_trn = (z_t1[:c_idx] - mu) / sd
    z_caln = (z_t1[c_idx:] - mu) / sd
    z_ten = (z_temp[mid:] - mu) / sd

    tae = TemporalAE((8, 12, 4, 12, 8))
    tae.fit(z_trn, epochs=60, lr=0.01)
    tae.quantize_int8(z_trn)

    ma_cal = ma_scores[:mid][norm_mask_1][c_idx:]
    cu_cal = cusum_s[:mid][norm_mask_1][c_idx:]
    ae_cal = tae.mse(z_caln)
    ae8_cal = tae.mse_int8(z_caln)

    y_test = gev[mid:].astype(int)
    ev_bool = y_test.astype(bool)
    starts = np.flatnonzero(ev_bool & ~np.r_[False, ev_bool[:-1]])
    ends = np.flatnonzero(ev_bool & ~np.r_[ev_bool[1:], False])

    lines = [
        f"=== Experiment 2: Temporal Anomaly Detection on GECCO 2019 (N_test={len(y_test)}, events={len(starts)}) ===",
        f"  {'Detector':30s} | {'P':>6s} {'R':>6s} {'F1':>6s} {'FPR':>6s} {'AUC':>6s} | {'Events':>7s} {'MedLat':>8s}",
        "  " + "-" * 86,
    ]

    for name, cal_s, te_s in (
        ("Moving-Avg Residual (EWMA Z)", ma_cal, ma_scores[mid:]),
        ("Change-Point (CUSUM)", cu_cal, cusum_s[mid:]),
        ("Temporal TinyML AE (float32)", ae_cal, tae.mse(z_ten)),
        ("Temporal TinyML AE (int8)", ae8_cal, tae.mse_int8(z_ten)),
    ):
        thr = np.quantile(cal_s, 0.99)  # 1% target FPR on normal validation
        pred = te_s > thr
        tp = int(((y_test == 1) & pred).sum())
        fp = int(((y_test == 0) & pred).sum())
        fn = int(((y_test == 1) & ~pred).sum())
        tn = int(((y_test == 0) & ~pred).sum())
        p = tp / max(tp + fp, 1)
        r = tp / max(tp + fn, 1)
        f1 = 2 * p * r / max(p + r, 1e-12)
        fpr = fp / max(fp + tn, 1)
        auc = roc_auc_score(y_test, te_s)
        lats = []
        for a, b in zip(starts, ends):
            hits = np.flatnonzero(pred[a : b + 1])
            if len(hits):
                lats.append(int(hits[0]))
        med_lat = f"{np.median(lats):.0f} min" if lats else "n/a"
        lines.append(
            f"  {name:30s} | {p:6.3f} {r:6.3f} {f1:6.3f} {fpr:6.3f} {auc:6.3f} | {len(lats):2d}/{len(starts):2d}   {med_lat:>8s}"
        )

    t1 = time.perf_counter()
    tae.mse_int8(z_ten[:2000])
    us = (time.perf_counter() - t1) / 2000 * 1e6
    lines.append("")
    lines.append(f"  Edge Footprint (Temporal TinyML AE int8): {tae.size_bytes()} bytes (weights+biases), {us:.1f} us/sample")

    txt = "\n".join(lines)
    print(txt)
    with open(os.path.join(HERE, "exp2_results.txt"), "w") as f:
        f.write(txt + "\n")


if __name__ == "__main__":
    main()
