"""Train, quantize, cross-validate, and externally evaluate the 4-feature TinyML water-quality model.

    uv run python tinyml/train_quality.py

Writes firmware/quality_model.json and tinyml/results.txt.
Method and sources: tinyml/README.md.

Key methodological features:
  - 4 inputs matching Sharma et al. [1]: [Temperature_C, pH, log1p(TDS_ppm), log1p(Turbidity_NTU)]
  - Temperature sampled from an identical overlapping distribution across all 3 classes
    (decorrelated from source identity so warm water is not falsely learned as unsafe).
  - Leakage-free Stratified 5-Fold Cross-Validation: source Kenya Safe readings are split
    into 5 folds BEFORE synthetic mixtures are generated inside each fold.
  - Clear separation between deterministic standards compliance (IS 10500 / TrinkwV)
    and TinyML contamination pattern classification (NORMAL / RUNOFF / CHEMICAL).
  - External validation on held-out Kenya, GECCO 2019 (German utility), and CSU ROSS
    high-frequency river sensor data (raw vs. QAQC + two-stage smoothing).
"""
import csv
import json
import math
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "firmware"))
from quality_model import QualityModel, QualityMonitor, check_standards  # noqa: E402

CLASSES = ["NORMAL", "RUNOFF", "CHEMICAL"]
PH_LO, PH_HI, TDS_MAX, TURB_MAX = 6.5, 8.5, 500.0, 5.0  # IS 10500:2012
COND_TO_TDS = 0.65                                       # TDS (ppm) ~ 0.65 * conductivity (uS/cm)
MODEL_PATH = os.path.join(ROOT, "firmware", "quality_model.json")
rng = np.random.default_rng(7)


# ------------------------------------------------------------------ Data loaders
def load_kenya():
    """Returns X [temp_c, ph, tds_ppm, turb_ntu], is_safe."""
    with open(os.path.join(HERE, "data", "kenya", "water_quality_dataset1.csv")) as f:
        rows = list(csv.DictReader(f))
    x = np.array(
        [[float(r["Temperature_C"]), float(r["pH"]), float(r["TDS_ppm"]), float(r["Turbidity_NTU"])] for r in rows]
    )
    return x, np.array([r["Label"] == "Safe" for r in rows])


def load_gecco():
    """Returns X [temp_c, ph, tds_ppm, turb_ntu], is_event."""
    with open(os.path.join(HERE, "data", "gecco", "5_gecco2019_all_water_quality.csv")) as f:
        rows = [r for r in csv.DictReader(f) if "NA" not in (r["Tp"], r["pH"], r["Cond"], r["Turb"])]
    x = np.array(
        [
            [float(r["Tp"]), float(r["pH"]), float(r["Cond"]) * 1e4 * COND_TO_TDS, float(r["Turb"])]
            for r in rows
        ]
    )
    return x, np.array([r["Event"] == "TRUE" for r in rows])


def load_csu_ross(max_rows_per_mode=None):
    """Load CSU ROSS 2019 (Poudre River) aligned by (site, DateTime_UTC).
    Returns dict {'final': (X, sites), 'raw': (X, sites)} where X is [temp_c, ph, tds_ppm, turb_ntu].
    """
    path = os.path.join(HERE, "data", "csu_ross", "CSU_ROSS_2019_cleaned.csv")
    wanted = {"Temperature": 0, "pH": 1, "Specific Conductivity": 2, "Turbidity": 3}
    buckets_final, buckets_raw = {}, {}
    with open(path) as f:
        for r in csv.DictReader(f):
            idx = wanted.get(r["parameter"])
            if idx is None:
                continue
            key = (r["site"], r["DateTime_UTC"])
            for val_col, store in (("value_final", buckets_final), ("value_raw", buckets_raw)):
                v = r[val_col]
                if v in ("", "NA", "NaN"):
                    continue
                try:
                    fv = float(v)
                except ValueError:
                    continue
                if idx == 2:
                    fv *= COND_TO_TDS
                arr = store.setdefault(key, [None, None, None, None])
                arr[idx] = fv

    out = {}
    for mode, store in (("final", buckets_final), ("raw", buckets_raw)):
        keys = sorted(k for k, v in store.items() if all(x is not None for x in v))
        if max_rows_per_mode:
            keys = keys[:max_rows_per_mode]
        x = np.array([store[k] for k in keys], dtype=float)
        # Filter physically impossible sensor glitches in raw (e.g. negative turbidity/TDS)
        valid = (x[:, 1] >= 0) & (x[:, 1] <= 14) & (x[:, 2] >= 0) & (x[:, 3] >= 0)
        sites = [keys[i][0] for i in range(len(keys)) if valid[i]]
        out[mode] = (x[valid], sites)
    return out


# ------------------------------------------------------------------ Mixing & Labels
def unsafe_is10500(x):
    """x columns: [temp_c, ph, tds_ppm, turb_ntu]."""
    return (x[:, 1] < PH_LO) | (x[:, 1] > PH_HI) | (x[:, 2] > TDS_MAX) | (x[:, 3] > TURB_MAX)


def unsafe_trinkwv(x):
    """German TrinkwV: pH 6.5-9.5, Conductivity <= 2500 uS/cm (~1625 ppm TDS), Turbidity <= 1.0 NTU."""
    return (x[:, 1] < 6.5) | (x[:, 1] > 9.5) | (x[:, 2] > 1625.0) | (x[:, 3] > 1.0)


def mix_ph(ph_a, ph_b, f):
    net = (1 - f) * (10 ** -ph_a - 10 ** (ph_a - 14)) + f * (10 ** -ph_b - 10 ** (ph_b - 14))
    return -np.log10((net + np.sqrt(net ** 2 + 4e-14)) / 2)


def logu(lo, hi, n):
    return np.exp(rng.uniform(math.log(lo), math.log(hi), n))


def make_samples(base, n):
    """Mix real normal water with runoff or chemical; sample temperature from the same
    overlapping distribution for all 3 classes so temperature cannot leak class identity.
    """
    b = base[rng.integers(len(base), size=n)].copy()
    # Overlapping temperature distribution across all 3 classes (10 C to 35 C)
    b[:, 0] = rng.uniform(10.0, 35.0, n)
    b[:, 2] *= rng.uniform(0.8, 2.5, n)  # Indian municipal TDS range (~100-400 ppm)

    kind = rng.integers(3, size=n)  # 0 normal, 1 runoff, 2 chemical
    end = {
        1: (rng.uniform(5.6, 7.0, n), rng.uniform(10, 150, n), logu(10, 300, n), logu(0.003, 0.9, n)),
        2: (rng.uniform(10.5, 12.5, n), logu(2000, 60000, n), rng.uniform(0.2, 3, n), logu(1e-4, 0.05, n)),
    }
    x = b.copy()
    for k, (ph, tds, turb, f) in end.items():
        m = kind == k
        x[m, 1] = mix_ph(b[m, 1], ph[m], f[m])
        x[m, 2] = (1 - f[m]) * b[m, 2] + f[m] * tds[m]
        x[m, 3] = (1 - f[m]) * b[m, 3] + f[m] * turb[m]

    # Sensor noise
    x[:, 0] += rng.normal(0, 0.2, n)
    x[:, 1] += rng.normal(0, 0.05, n)
    x[:, 2] *= rng.normal(1, 0.03, n)
    x[:, 3] = np.abs(x[:, 3] + rng.normal(0, 0.2, n))

    bad = unsafe_is10500(x)
    y = np.where(bad, kind, 0)
    keep = ~(bad & (kind == 0))
    x, y = x[keep], y[keep]
    per = min(np.bincount(y, minlength=3))
    idx = np.concatenate([rng.choice(np.flatnonzero(y == c), per, replace=False) for c in range(3)])
    rng.shuffle(idx)
    return x[idx], y[idx]


def feats(x):
    return np.column_stack(
        [x[:, 0], x[:, 1], np.log1p(np.maximum(x[:, 2], 0)), np.log1p(np.maximum(x[:, 3], 0))]
    )


# ------------------------------------------------------------------ 4 -> 16 -> 8 -> 3 MLP
def forward(W, B, z):
    acts = [z]
    for l, (w, b) in enumerate(zip(W, B)):
        a = acts[-1] @ w + b
        acts.append(np.maximum(a, 0) if l < 2 else a)
    return acts


def softmax(a):
    e = np.exp(a - a.max(1, keepdims=True))
    return e / e.sum(1, keepdims=True)


def train(z, y, sizes=(4, 16, 8, 3), epochs=50, lr=0.01, batch=128):
    W = [rng.normal(0, math.sqrt(2 / a), (a, b)) for a, b in zip(sizes[:-1], sizes[1:])]
    B = [np.zeros(b) for b in sizes[1:]]
    params = W + B
    m1 = [np.zeros_like(p) for p in params]
    m2 = [np.zeros_like(p) for p in params]
    Y, t = np.eye(3)[y], 0
    for _ in range(epochs):
        order = rng.permutation(len(z))
        for s in range(0, len(z), batch):
            i = order[s : s + batch]
            acts = forward(W, B, z[i])
            d = (softmax(acts[-1]) - Y[i]) / len(i)
            gW, gB = [None] * 3, [None] * 3
            for l in (2, 1, 0):
                gW[l], gB[l] = acts[l].T @ d, d.sum(0)
                if l:
                    d = (d @ W[l].T) * (acts[l] > 0)
            t += 1
            for p, g, a, v in zip(params, gW + gB, m1, m2):
                a[:] = 0.9 * a + 0.1 * g
                v[:] = 0.999 * v + 0.001 * g * g
                p -= lr * (a / (1 - 0.9 ** t)) / (np.sqrt(v / (1 - 0.999 ** t)) + 1e-8)
    return W, B


def quantize(W, B, zcal):
    acts = forward(W, B, zcal)
    s_in = np.percentile(np.abs(zcal), 99.9) / 127
    s, layers = s_in, []
    for l, (w, b) in enumerate(zip(W, B)):
        sw = np.abs(w).max() / 127
        s_out = np.percentile(acts[l + 1], 99.9) / 127 if l < 2 else None
        layers.append(
            {
                "w": np.round(w / sw).astype(int).tolist(),
                "b": np.round(b / (s * sw)).astype(int).tolist(),
                "m": float(s * sw / s_out) if s_out else None,
            }
        )
        s = s_out
    return float(s_in), layers


def save_and_load_model(mean, std, in_scale, layers, path):
    with open(path, "w") as f:
        json.dump(
            {"classes": CLASSES, "mean": mean.tolist(), "std": std.tolist(), "in_scale": in_scale, "layers": layers},
            f,
        )
    return QualityModel(path)


def prf1(y_true, y_pred):
    """Per-class Precision, Recall, F1 and macro F1."""
    cm = np.zeros((3, 3), int)
    np.add.at(cm, (y_true, y_pred), 1)
    metrics = {}
    for c, name in enumerate(CLASSES):
        tp = cm[c, c]
        fp = cm[:, c].sum() - tp
        fn = cm[c, :].sum() - tp
        p = tp / max(tp + fp, 1)
        r = tp / max(tp + fn, 1)
        f1 = 2 * p * r / max(p + r, 1e-12)
        metrics[name] = (p, r, f1)
    macro_f1 = sum(v[2] for v in metrics.values()) / 3
    return cm, metrics, macro_f1


# ------------------------------------------------------------------ Main
def main():
    t0 = time.time()
    kx, ksafe = load_kenya()
    gx, gev = load_gecco()
    csu = load_csu_ross()
    safe = kx[ksafe]

    out = []
    say = lambda s: (print(s), out.append(s))

    # ------------------------------------------------------------------
    # 1. Leakage-free Stratified 5-Fold Cross-Validation
    #    Split source Kenya Safe readings FIRST, generate mixtures per fold.
    # ------------------------------------------------------------------
    perm = rng.permutation(len(safe))
    src_folds = np.array_split(perm, 5)
    accs_f, accs_q, agrees, f1s_q = [], [], [], []
    cm_total = np.zeros((3, 3), int)
    tmp_model_path = os.path.join(HERE, "_cv_tmp_model.json")

    for k in range(5):
        val_src = safe[src_folds[k]]
        tr_src = np.vstack([safe[src_folds[j]] for j in range(5) if j != k])
        xtr, ytr = make_samples(tr_src, 45000)
        xval, yval = make_samples(val_src, 15000)
        ztr = feats(xtr)
        mean, std = ztr.mean(0), ztr.std(0)
        W, B = train((ztr - mean) / std, ytr)
        in_scale, layers = quantize(W, B, (ztr - mean) / std)
        m_cv = save_and_load_model(mean, std, in_scale, layers, tmp_model_path)

        pfl = forward(W, B, (feats(xval) - mean) / std)[-1].argmax(1)
        pq = np.array([CLASSES.index(m_cv.classify(*r)) for r in xval])
        cm_k, _, macro_f1 = prf1(yval, pq)
        cm_total += cm_k
        accs_f.append((pfl == yval).mean())
        accs_q.append((pq == yval).mean())
        agrees.append((pq == pfl).mean())
        f1s_q.append(macro_f1)

    if os.path.exists(tmp_model_path):
        os.remove(tmp_model_path)

    _, cv_metrics, cv_macro_f1 = prf1(
        np.repeat([0, 1, 2], cm_total.sum(1)),
        np.concatenate([np.repeat([0, 1, 2], cm_total[c]) for c in range(3)]),
    )
    say(
        f"1. Leakage-free 5-Fold CV on mixtures (mean +/- std across folds):\n"
        f"   float32 acc: {np.mean(accs_f):.4f} +/- {np.std(accs_f):.4f} | "
        f"int8 acc: {np.mean(accs_q):.4f} +/- {np.std(accs_q):.4f} | "
        f"int8/float agree: {np.mean(agrees):.4f} | macro-F1: {np.mean(f1s_q):.4f}"
    )
    for cname in CLASSES:
        p, r, f1 = cv_metrics[cname]
        say(f"   {cname:9s}: P={p:.4f}  R={r:.4f}  F1={f1:.4f}")
    say("   CV confusion (rows=true, cols=int8): " + "; ".join(f"{CLASSES[i]} {cm_total[i].tolist()}" for i in range(3)))

    # ------------------------------------------------------------------
    # Train final model on all Kenya Safe source rows (with 20% held out for drift/temp stress)
    # ------------------------------------------------------------------
    split = int(0.8 * len(safe))
    xtr_all, ytr_all = make_samples(safe[:split], 90000)
    xte, yte = make_samples(safe[split:], 20000)
    ztr_all = feats(xtr_all)
    mean, std = ztr_all.mean(0), ztr_all.std(0)
    W, B = train((ztr_all - mean) / std, ytr_all)
    in_scale, layers = quantize(W, B, (ztr_all - mean) / std)
    model = save_and_load_model(mean, std, in_scale, layers, MODEL_PATH)
    q = lambda x: np.array([CLASSES.index(model.classify(*r)) for r in x])

    # ------------------------------------------------------------------
    # 2. Held-out Kenya real readings (report N, FPR, TPR)
    # ------------------------------------------------------------------
    safe_ho = safe[split:]
    psafe = q(safe_ho)
    fpr_kenya = (psafe != 0).mean()
    say(f"2. Held-out Kenya Safe water (N={len(safe_ho)}): FPR={fpr_kenya:.4f} ({(psafe != 0).sum()} flagged)")

    ux = kx[~ksafe]
    pu = q(ux)
    over_is = unsafe_is10500(ux)
    say(
        f"   Real Kenya Unsafe water (N={len(ux)}): flagged={np.mean(pu != 0):.4f}; "
        f"of N={over_is.sum()} exceeding IS 10500: TPR={np.mean(pu[over_is] != 0):.4f}; "
        f"classes={dict(zip(CLASSES, np.bincount(pu, minlength=3).tolist()))}"
    )

    # ------------------------------------------------------------------
    # 3. GECCO 2019 (evaluate under BOTH German TrinkwV and IS 10500)
    # ------------------------------------------------------------------
    pg = q(gx)
    norm = ~gev
    in_is = norm & ~unsafe_is10500(gx)
    in_de = norm & ~unsafe_trinkwv(gx)
    say(
        f"3. GECCO 2019 German utility (normal N={norm.sum()}):\n"
        f"   Under IS 10500: {in_is.sum()} compliant -> TinyML NORMAL={np.mean(pg[in_is] == 0):.4f}, FPR={np.mean(pg[in_is] != 0):.4f}\n"
        f"   Under German TrinkwV (pH 6.5-9.5): {in_de.sum()} compliant (pH>8.5 in {np.mean(gx[norm, 1] > 8.5):.4f}); "
        f"TinyML flags {(pg[in_de] != 0).mean():.4f} of TrinkwV-compliant water because TinyML was trained on IS 10500 limits."
    )

    # ------------------------------------------------------------------
    # 4. CSU ROSS 2019 High-Frequency River Dataset (external environmental test)
    # ------------------------------------------------------------------
    xf, _ = csu["final"]
    xr, _ = csu["raw"]
    pf_csu = q(xf)
    pr_csu = q(xr)
    over_csu = unsafe_is10500(xf)
    # Compare raw single-sample vs 2-stage QualityMonitor (window=3 smoothing + 3-reading confirm)
    mon_raw = QualityMonitor(model, window=3, confirm_n=3, clear_n=10, standard="IS_10500")
    mon_fin = QualityMonitor(model, window=3, confirm_n=3, clear_n=10, standard="IS_10500")
    raw_alerts = 0
    for r in xr:
        _, _, _, ev = mon_raw.update(*r)
        if ev and ev[0] == "ALERT":
            raw_alerts += 1
    fin_alerts = 0
    for r in xf:
        _, _, _, ev = mon_fin.update(*r)
        if ev and ev[0] == "ALERT":
            fin_alerts += 1

    # On CSU ROSS rows that ARE within IS 10500 limits, check false-positive rate across wide temp range (0-25 C)
    in_csu = ~over_csu
    say(
        f"4. CSU ROSS 2019 River Dataset (N_final={len(xf)}, temp {xf[:, 0].min():.1f}..{xf[:, 0].max():.1f} C):\n"
        f"   IS-10500-compliant river rows (N={in_csu.sum()}): TinyML NORMAL={np.mean(pf_csu[in_csu] == 0):.4f}, FPR={np.mean(pf_csu[in_csu] != 0):.4f}\n"
        f"   Rows exceeding IS 10500 (N={over_csu.sum()}, mainly turbidity/runoff): TPR={np.mean(pf_csu[over_csu] != 0):.4f} "
        f"(RUNOFF={(pf_csu[over_csu] == 1).sum()}, CHEMICAL={(pf_csu[over_csu] == 2).sum()})\n"
        f"   Raw vs QAQC'd episodes (2-stage window=3 + confirm=3): raw={raw_alerts} alert episodes vs final={fin_alerts} alert episodes"
    )

    # ------------------------------------------------------------------
    # 5. Temperature Stress Test on NORMAL water (10 C to 35 C)
    # ------------------------------------------------------------------
    normal_mask = yte == 0
    x_norm = xte[normal_mask].copy()
    temp_fprs = []
    for t_val in (10.0, 15.0, 20.0, 25.0, 30.0, 35.0):
        xt = x_norm.copy()
        xt[:, 0] = t_val
        fpr_t = (q(xt) != 0).mean()
        temp_fprs.append(f"{int(t_val)}C:FPR={fpr_t:.4f}")
    say(f"5. Temperature stress test on NORMAL water (N={len(x_norm)}): " + ", ".join(temp_fprs))

    # ------------------------------------------------------------------
    # 6. Sensor Drift Test & Resource Footprint
    # ------------------------------------------------------------------
    drift = []
    for name, dx in (
        ("pH +0.2", (0, 0.2, 1, 0)),
        ("pH -0.2", (0, -0.2, 1, 0)),
        ("TDS +10%", (0, 0, 1.1, 0)),
        ("TDS -10%", (0, 0, 0.9, 0)),
        ("turb +1 NTU", (0, 0, 1, 1)),
    ):
        x2 = xte.copy()
        x2[:, 1] += dx[1]
        x2[:, 2] *= dx[2]
        x2[:, 3] += dx[3]
        y2 = np.where(unsafe_is10500(x2) | (yte != 0), yte, 0)
        drift.append(f"{name} {np.mean(q(x2) == y2):.4f}")
    say("6. Sensor drift accuracy: " + ", ".join(drift))

    n_w = sum(len(l["w"]) * len(l["w"][0]) for l in layers)
    n_b = sum(len(l["b"]) for l in layers)
    t1 = time.perf_counter()
    for r in xte[:2000]:
        model.classify(*r)
    us = (time.perf_counter() - t1) / 2000 * 1e6
    say(
        f"7. Model footprint (4->16->8->3): {n_w} int8 weights + {n_b} int32 biases = {n_w + 4 * n_b} bytes; "
        f"JSON {os.path.getsize(MODEL_PATH)} bytes; {us:.0f} us/reading on host (runs on Raspberry Pi 4 in <1 ms)"
    )
    say(f"Done in {time.time() - t0:.1f} s")

    with open(os.path.join(HERE, "results.txt"), "w") as f:
        f.write("\n".join(out) + "\n")

    # Sanity assertions
    assert np.mean(accs_q) >= 0.97 and np.mean(agrees) >= 0.99, (accs_q, agrees)
    assert fpr_kenya <= 0.01, fpr_kenya
    assert np.mean(pu[over_is] != 0) >= 0.95
    assert all((q(np.column_stack([np.full(len(x_norm), tv), x_norm[:, 1:]])) != 0).mean() <= 0.01 for tv in (10.0, 25.0, 35.0))
    print("tinyml checks passed")


if __name__ == "__main__":
    main()
