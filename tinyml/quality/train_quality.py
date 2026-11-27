"""Train, quantize, cross-validate, and externally evaluate the 4-feature TinyML water-quality model.

    uv run python -m tinyml.quality.train_quality

Writes tinyml/quality/quality_model.json and tinyml/quality/results.txt.
Method and sources: tinyml/quality/README.md.

Design:
  - 4 inputs matching Sharma et al. [1]: [Temperature_C, pH, log1p(TDS_ppm), log1p(Turbidity_NTU)]
  - Temperature sampled from an identical overlapping distribution (4-35 C) across all 3 classes.
  - Expanded contamination mixtures:
      * RUNOFF: varied concentrations (including low-turbidity 2-300 NTU, pH 5.4-7.4, low TDS)
      * CHEMICAL: acidic (pH 2.0-5.0), neutral high-TDS (pH 6.5-8.5), and alkaline (pH 9.5-12.5),
        across overlapping turbidity (0.2-80 NTU) so turbidity alone cannot shortcut to RUNOFF.
  - Leakage-free Stratified 5-Fold CV: source Kenya Safe rows split FIRST, mixtures built per fold.
  - Overlapping RUNOFF vs CHEMICAL 2-class discrimination test on held-out source samples.
  - External evaluation (never used in training/tuning): Kenya Unsafe, GECCO 2019, CSU ROSS 2019,
    and Loddon Catchment storm-event before/during/after analysis.
"""
import csv
import json
import math
import os
import time
from datetime import datetime

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
from tinyml.quality.quality_model import QualityModel, QualityMonitor  # noqa: E402

CLASSES = ["NORMAL", "RUNOFF", "CHEMICAL"]
PH_LO, PH_HI, TDS_MAX, TURB_MAX = 6.5, 8.5, 500.0, 5.0  # IS 10500:2012
COND_TO_TDS = 0.65
MODEL_PATH = os.path.join(HERE, "quality_model.json")
rng = np.random.default_rng(7)


# ------------------------------------------------------------------ Data loaders
def load_kenya():
    with open(os.path.join(HERE, "data", "kenya", "water_quality_dataset1.csv")) as f:
        rows = list(csv.DictReader(f))
    x = np.array(
        [[float(r["Temperature_C"]), float(r["pH"]), float(r["TDS_ppm"]), float(r["Turbidity_NTU"])] for r in rows]
    )
    return x, np.array([r["Label"] == "Safe" for r in rows])


def load_gecco():
    with open(os.path.join(HERE, "data", "gecco", "5_gecco2019_all_water_quality.csv")) as f:
        rows = [r for r in csv.DictReader(f) if "NA" not in (r["Tp"], r["pH"], r["Cond"], r["Turb"])]
    x = np.array(
        [
            [float(r["Tp"]), float(r["pH"]), float(r["Cond"]) * 1e4 * COND_TO_TDS, float(r["Turb"])]
            for r in rows
        ]
    )
    return x, np.array([r["Event"] == "TRUE" for r in rows])


def load_csu_ross():
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
        x = np.array([store[k] for k in keys], dtype=float)
        valid = (x[:, 1] >= 0) & (x[:, 1] <= 14) & (x[:, 2] >= 0) & (x[:, 3] >= 0)
        out[mode] = x[valid]
    return out


def load_loddon_series():
    """Return sorted hourly (epoch_s, [temp, ph, tds, turb]) and set of storm epoch_s."""
    storm_epochs = set()
    with open(os.path.join(HERE, "data", "loddon", "data", "StormWaterQualityData.csv")) as f:
        for r in csv.DictReader(f):
            dt = datetime.strptime(f"{r['Date'].strip()} {r['Time'].strip()[:2]}:00:00", "%d/%m/%Y %H:%M:%S")
            storm_epochs.add(int(dt.timestamp()))

    rows = []
    with open(os.path.join(HERE, "data", "loddon", "data", "HighFrequencySensorDataFlowandSonde.csv")) as f:
        for r in csv.DictReader(f):
            vals = [r["SONDETEMP"], r["SONDEPH"], r["SONDECOND"], r["SONDETURBIDITY"]]
            if any(v in ("", "NA", "-", "NaN") for v in vals):
                continue
            try:
                temp, ph, cond, turb = map(float, vals)
                dt = datetime.strptime(f"{r['SONDEDATE'].strip()} {r['SONDETIME'].strip()[:2]}:00:00", "%d/%m/%Y %H:%M:%S")
            except ValueError:
                continue
            rows.append((int(dt.timestamp()), [temp, ph, cond * COND_TO_TDS, turb]))
    rows.sort(key=lambda t: t[0])
    return rows, storm_epochs


# ------------------------------------------------------------------ Mixing & Labels
def unsafe_is10500(x):
    return (x[:, 1] < PH_LO) | (x[:, 1] > PH_HI) | (x[:, 2] > TDS_MAX) | (x[:, 3] > TURB_MAX)


def unsafe_trinkwv(x):
    return (x[:, 1] < 6.5) | (x[:, 1] > 9.5) | (x[:, 2] > 1625.0) | (x[:, 3] > 1.0)


def mix_ph(ph_a, ph_b, f):
    net = (1 - f) * (10 ** -ph_a - 10 ** (ph_a - 14)) + f * (10 ** -ph_b - 10 ** (ph_b - 14))
    return -np.log10((net + np.sqrt(net ** 2 + 4e-14)) / 2)


def logu(lo, hi, n):
    return np.exp(rng.uniform(math.log(lo), math.log(hi), n))


def make_samples(base, n):
    """Build balanced NORMAL / RUNOFF / CHEMICAL mixtures with overlapping feature ranges."""
    b = base[rng.integers(len(base), size=n)].copy()
    b[:, 0] = rng.uniform(4.0, 35.0, n)  # overlapping temperature (4-35 C) across all 3 classes
    b[:, 2] *= rng.uniform(0.8, 2.5, n)

    kind = rng.integers(3, size=n)  # 0 normal, 1 runoff, 2 chemical
    x = b.copy()

    # Normal municipal water spans pH 6.55-8.49 naturally across utilities (Kenya ~7.2, EU ~8.48)
    m_norm = kind == 0
    if m_norm.sum():
        shift = rng.uniform(-0.4, 1.2, m_norm.sum())
        x[m_norm, 1] = np.clip(x[m_norm, 1] + shift, 6.55, 8.49)

    # Runoff: varied concentrations (low-to-high turbidity, pH 5.4-7.4, low-to-moderate TDS)
    m_run = kind == 1
    nr = m_run.sum()
    if nr:
        ph_r = rng.uniform(5.4, 7.4, nr)
        tds_r = rng.uniform(10.0, 250.0, nr)
        turb_r = logu(2.5, 300.0, nr)
        f_r = logu(0.005, 0.9, nr)
        x[m_run, 1] = mix_ph(b[m_run, 1], ph_r, f_r)
        x[m_run, 2] = (1 - f_r) * b[m_run, 2] + f_r * tds_r
        x[m_run, 3] = (1 - f_r) * b[m_run, 3] + f_r * turb_r

    # Chemical: acidic (vinegar/acid), neutral (salts/solvents), and alkaline (bleach/detergent),
    # across overlapping turbidity (0.2-80 NTU)
    m_chem = kind == 2
    nc = m_chem.sum()
    if nc:
        regime = rng.integers(3, size=nc)  # 0 acidic, 1 neutral high-TDS, 2 alkaline
        ph_c = np.where(
            regime == 0,
            rng.uniform(2.0, 5.0, nc),
            np.where(regime == 1, rng.uniform(6.5, 8.5, nc), rng.uniform(9.5, 12.5, nc)),
        )
        tds_c = logu(1500.0, 60000.0, nc)
        turb_c = logu(0.2, 80.0, nc)  # includes turbid chemicals (e.g. detergents)
        f_c = logu(0.001, 0.15, nc)
        x[m_chem, 1] = mix_ph(b[m_chem, 1], ph_c, f_c)
        x[m_chem, 2] = (1 - f_c) * b[m_chem, 2] + f_c * tds_c
        x[m_chem, 3] = (1 - f_c) * b[m_chem, 3] + f_c * turb_c

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
    loddon_rows, storm_epochs = load_loddon_series()
    safe = kx[ksafe]

    out = []
    say = lambda s: (print(s), out.append(s))

    # 1. Leakage-free Stratified 5-Fold CV
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

    _, cv_metrics, _ = prf1(
        np.repeat([0, 1, 2], cm_total.sum(1)),
        np.concatenate([np.repeat([0, 1, 2], cm_total[c]) for c in range(3)]),
    )
    say(
        f"1. Leakage-free 5-Fold CV (expanded mixtures, mean +/- std):\n"
        f"   float32 acc: {np.mean(accs_f):.4f} +/- {np.std(accs_f):.4f} | "
        f"int8 acc: {np.mean(accs_q):.4f} +/- {np.std(accs_q):.4f} | "
        f"int8/float agree: {np.mean(agrees):.4f} | macro-F1: {np.mean(f1s_q):.4f}"
    )
    for cname in CLASSES:
        p, r, f1 = cv_metrics[cname]
        say(f"   {cname:9s}: P={p:.4f}  R={r:.4f}  F1={f1:.4f}")
    say("   CV confusion (rows=true, cols=int8): " + "; ".join(f"{CLASSES[i]} {cm_total[i].tolist()}" for i in range(3)))

    # Train final model on 80% Kenya Safe, reserve 20% held-out source rows
    split = int(0.8 * len(safe))
    xtr_all, ytr_all = make_samples(safe[:split], 90000)
    xte, yte = make_samples(safe[split:], 20000)
    ztr_all = feats(xtr_all)
    mean, std = ztr_all.mean(0), ztr_all.std(0)
    W, B = train((ztr_all - mean) / std, ytr_all)
    in_scale, layers = quantize(W, B, (ztr_all - mean) / std)
    model = save_and_load_model(mean, std, in_scale, layers, MODEL_PATH)
    q = lambda x: np.array([CLASSES.index(model.classify(*r)) for r in x])

    # 2. Dedicated RUNOFF vs CHEMICAL Overlapping-Range Experiment (on held-out source samples)
    mask_rc = yte != 0
    x_rc, y_rc = xte[mask_rc], yte[mask_rc]
    p_rc = q(x_rc)
    # Overlap sub-region: Turbidity 5..40 NTU and pH 5.5..8.5
    ov = (x_rc[:, 3] >= 5.0) & (x_rc[:, 3] <= 40.0) & (x_rc[:, 1] >= 5.5) & (x_rc[:, 1] <= 8.5)
    r_true = (y_rc[ov] == 1)
    c_true = (y_rc[ov] == 2)
    r_pred_r = int((p_rc[ov][r_true] == 1).sum())
    r_pred_c = int((p_rc[ov][r_true] == 2).sum())
    c_pred_r = int((p_rc[ov][c_true] == 1).sum())
    c_pred_c = int((p_rc[ov][c_true] == 2).sum())
    ov_acc = (r_pred_r + c_pred_c) / max(ov.sum(), 1)
    say(
        f"2. RUNOFF vs CHEMICAL Overlap Experiment (held-out sources, Turb 5-40 NTU & pH 5.5-8.5, N={ov.sum()}):\n"
        f"   2-class accuracy in overlap zone: {ov_acc:.4f}\n"
        f"   2-class confusion: RUNOFF(N={r_true.sum()}) -> [RUNOFF={r_pred_r}, CHEM={r_pred_c}] | "
        f"CHEMICAL(N={c_true.sum()}) -> [RUNOFF={c_pred_r}, CHEM={c_pred_c}]"
    )

    # 3. Held-out Kenya Safe & Unsafe
    safe_ho = safe[split:]
    psafe = q(safe_ho)
    fpr_kenya = (psafe != 0).mean()
    ux = kx[~ksafe]
    pu = q(ux)
    over_is = unsafe_is10500(ux)
    say(
        f"3. Held-out Kenya Safe (N={len(safe_ho)}): FPR={fpr_kenya:.4f} ({(psafe != 0).sum()} flagged)\n"
        f"   Kenya Unsafe (N={len(ux)}, over IS 10500 N={over_is.sum()}): TPR={np.mean(pu[over_is] != 0):.4f}, "
        f"classes={dict(zip(CLASSES, np.bincount(pu, minlength=3).tolist()))}"
    )

    # 4. GECCO 2019 (IS 10500 vs German TrinkwV)
    pg = q(gx)
    norm = ~gev
    in_is = norm & ~unsafe_is10500(gx)
    in_de = norm & ~unsafe_trinkwv(gx)
    say(
        f"4. GECCO 2019 (normal N={norm.sum()}):\n"
        f"   Under IS 10500 (N={in_is.sum()} compliant): FPR={np.mean(pg[in_is] != 0):.4f}\n"
        f"   Under German TrinkwV (N={in_de.sum()} compliant, pH>8.5 in {np.mean(gx[norm, 1] > 8.5):.4f}): "
        f"TinyML flags {np.mean(pg[in_de] != 0):.4f} (trained on IS 10500 cutoffs)"
    )

    # 5. CSU ROSS 2019 River Dataset
    xf, xr = csu["final"], csu["raw"]
    pf_csu = q(xf)
    over_csu = unsafe_is10500(xf)
    in_csu = ~over_csu
    say(
        f"5. CSU ROSS 2019 (N_final={len(xf)}): IS-compliant N={in_csu.sum()} FPR={np.mean(pf_csu[in_csu] != 0):.4f}; "
        f"over-IS N={over_csu.sum()} TPR={np.mean(pf_csu[over_csu] != 0):.4f} "
        f"(RUNOFF={(pf_csu[over_csu] == 1).sum()}, CHEM={(pf_csu[over_csu] == 2).sum()})"
    )

    # 6. Loddon Catchment: Before (-6..-1h) vs During (storm) vs After (+1..+6h) Storm Events
    # Compare relative changes rather than treating all turbid river water as runoff
    pre_rows, dur_rows, post_rows = [], [], []
    for ts, row in loddon_rows:
        if ts in storm_epochs:
            dur_rows.append(row)
        elif any((ts + h * 3600) in storm_epochs for h in range(1, 7)):
            pre_rows.append(row)
        elif any((ts - h * 3600) in storm_epochs for h in range(1, 7)):
            post_rows.append(row)
    def loddon_stats(name, rows):
        a = np.array(rows)
        preds = q(a)
        return (
            f"   {name:18s} (N={len(a):3d}): med Turb={np.median(a[:,3]):5.1f} NTU, "
            f"med TDS={np.median(a[:,2]):4.0f} ppm, med pH={np.median(a[:,1]):.2f} | "
            f"RUNOFF={np.mean(preds==1):.3f}, CHEM={np.mean(preds==2):.3f}, NORM={np.mean(preds==0):.3f}"
        )
    say("6. Loddon Catchment Storm-Window Comparison (before/during/after):")
    say(loddon_stats("Pre-storm (-6..-1h)", pre_rows))
    say(loddon_stats("During storm", dur_rows))
    say(loddon_stats("Post-storm (+1..6h)", post_rows))

    # 7. Temperature Stress Test & Drift & Footprint
    x_norm = xte[yte == 0].copy()
    temp_fprs = [f"{int(tv)}C:{(q(np.column_stack([np.full(len(x_norm), tv), x_norm[:, 1:]])) != 0).mean():.4f}" for tv in (10, 15, 20, 25, 30, 35)]
    say(f"7. Temperature stress test on NORMAL (N={len(x_norm)}): " + ", ".join(temp_fprs))

    n_w = sum(len(l["w"]) * len(l["w"][0]) for l in layers)
    n_b = sum(len(l["b"]) for l in layers)
    t1 = time.perf_counter()
    for r in xte[:2000]:
        model.classify(*r)
    us = (time.perf_counter() - t1) / 2000 * 1e6
    say(
        f"8. Footprint (4->16->8->3): {n_w} int8 weights + {n_b} int32 biases = {n_w + 4 * n_b} bytes; "
        f"{us:.0f} us/reading on host (not measured on a Raspberry Pi 3B)"
    )
    say(f"Done in {time.time() - t0:.1f} s")

    with open(os.path.join(HERE, "results.txt"), "w") as f:
        f.write("\n".join(out) + "\n")

    assert np.mean(accs_q) >= 0.97 and np.mean(agrees) >= 0.99
    assert fpr_kenya <= 0.01
    assert np.mean(pg[in_is] != 0) <= 0.05, np.mean(pg[in_is] != 0)
    print("tinyml checks passed")


if __name__ == "__main__":
    main()
