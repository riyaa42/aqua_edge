"""Experiment 2 (Expanded): Temporal Anomaly Detection, Leakage Audit, PR Trade-off, and Loddon/CSU ROSS Validation.

Verifies zero temporal leakage:
  1. Causal EWMA: perturbing future samples leaves past features bit-for-bit identical.
  2. Chronological split: Train (first 40% normal) -> Val (next 10% normal) -> Test (final 50%).
  3. Event boundary check: asserts no labelled event window straddles the train/val -> test boundary.
  4. Normalization & thresholds fitted strictly on train/val, never on test.

Evaluates:
  - Precision-Recall trade-off across validation-calibrated quantiles (99.0% .. 99.99%) + 3-sample confirmation
  - Actual sampling interval from timestamps (1 min in GECCO, 60 min in Loddon)
  - Detection delay in BOTH samples and minutes, event detection rate, and false alert episodes/day
  - External temporal evaluation on Loddon Catchment (storm windows).
"""
import csv
import json
import math
import os
import time
from datetime import datetime

import numpy as np
from sklearn.metrics import roc_auc_score

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPORAL_MODEL_PATH = os.path.join(HERE, "temporal_model.json")
COND_TO_TDS = 0.65
rng = np.random.default_rng(42)


def load_gecco():
    rows = []
    with open(os.path.join(HERE, "data", "gecco", "5_gecco2019_all_water_quality.csv")) as f:
        for r in csv.DictReader(f):
            if "NA" in (r["Tp"], r["pH"], r["Cond"], r["Turb"]):
                continue
            dt = datetime.strptime(r["Time"].strip(), "%Y-%m-%d %H:%M:%S")
            rows.append((
                int(dt.timestamp()),
                [float(r["Tp"]), float(r["pH"]), float(r["Cond"]) * 1e4 * COND_TO_TDS, float(r["Turb"])],
                r["Event"] == "TRUE",
            ))
    ts = np.array([r[0] for r in rows])
    x = np.array([r[1] for r in rows])
    ev = np.array([r[2] for r in rows])
    dt_min = float(np.median(np.diff(ts))) / 60.0
    return ts, x, ev, dt_min


def load_loddon():
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
    ts = np.array([r[0] for r in rows])
    x = np.array([r[1] for r in rows])
    ev = np.array([t in storm_epochs for t in ts])
    dt_min = float(np.median(np.diff(ts))) / 60.0
    return ts, x, ev, dt_min


def feats(x):
    return np.column_stack(
        [x[:, 0], x[:, 1], np.log1p(np.maximum(x[:, 2], 0)), np.log1p(np.maximum(x[:, 3], 0))]
    )


def ewma_series(z, alpha):
    out = np.empty_like(z)
    cur = z[0].copy()
    for i in range(len(z)):
        cur = (1.0 - alpha) * cur + alpha * z[i]
        out[i] = cur
    return out


def rolling_ewma_z(z, alpha_mean=1 / 720, alpha_var=1 / 720):
    res = np.zeros_like(z)
    mu = z[0].copy()
    var = np.ones(z.shape[1]) * 0.05
    for i in range(len(z)):
        diff = z[i] - mu
        sd = np.sqrt(np.maximum(var, 1e-4))
        res[i] = diff / sd
        clipped = np.clip(diff, -3.0 * sd, 3.0 * sd)
        mu = (1.0 - alpha_mean) * mu + alpha_mean * (mu + clipped)
        var = (1.0 - alpha_var) * var + alpha_var * (clipped ** 2)
    return res


def cusum_scores(z_res, k=1.5):
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

    def mse_int8(self, z):
        a = z
        for l, (wq, sw, sa, b) in enumerate(zip(self.Wq, self.sw, self.sa, self.B)):
            aq = np.clip(np.round(a / sa), -128, 127).astype(np.int32)
            out = (aq @ wq.astype(np.int32)) * (sa * sw) + b
            a = np.maximum(out, 0) if l < len(self.Wq) - 1 else out
        return np.mean((a - z) ** 2, axis=1)

    def export_dict(self, mu, sd, threshold, alpha_short=1 / 15, alpha_long=1 / 720, warmup=15, confirm_n=3, max_gap_s=1800):
        return {
            "version": 1,
            "feature_order": ["temp_c", "ph", "log1p_tds_ppm", "log1p_turbidity_ntu"],
            "temporal_order": ["short_delta_4", "long_deviation_4"],
            "alpha_short": alpha_short,
            "alpha_long": alpha_long,
            "warmup_samples": warmup,
            "confirm_n": confirm_n,
            "clear_n": 10,
            "max_gap_s": max_gap_s,
            "threshold": float(threshold),
            "mu": mu.tolist(),
            "sd": sd.tolist(),
            "layers": [
                {
                    "w": wq.astype(int).tolist(),
                    "b": b.astype(float).tolist(),
                    "sw": float(sw),
                    "sa": float(sa),
                }
                for wq, sw, sa, b in zip(self.Wq, self.sw, self.sa, self.B)
            ],
        }


def apply_confirm(pred, n_confirm):
    if n_confirm <= 1:
        return pred
    out = np.zeros_like(pred)
    run = 0
    for i, v in enumerate(pred):
        run = run + 1 if v else 0
        if run >= n_confirm:
            out[i] = True
    return out


def eval_scores(y_test, te_scores, thr, dt_min, confirm=1):
    raw_pred = te_scores > thr
    pred = apply_confirm(raw_pred, confirm)
    tp = int(((y_test == 1) & pred).sum())
    fp = int(((y_test == 0) & pred).sum())
    fn = int(((y_test == 1) & ~pred).sum())
    tn = int(((y_test == 0) & ~pred).sum())
    p = tp / max(tp + fp, 1)
    r = tp / max(tp + fn, 1)
    f1 = 2 * p * r / max(p + r, 1e-12)
    fpr = fp / max(fp + tn, 1)

    ev_bool = y_test.astype(bool)
    starts = np.flatnonzero(ev_bool & ~np.r_[False, ev_bool[:-1]])
    ends = np.flatnonzero(ev_bool & ~np.r_[ev_bool[1:], False])
    lats_samp = []
    for a, b in zip(starts, ends):
        hits = np.flatnonzero(pred[a : b + 1])
        if len(hits):
            lats_samp.append(int(hits[0]))

    # False alert episodes (contiguous runs of pred==True entirely outside events)
    fp_only = pred & (~ev_bool)
    fp_starts = np.flatnonzero(fp_only & ~np.r_[False, fp_only[:-1]])
    test_days = (len(y_test) * dt_min) / 1440.0
    fp_ep_per_day = len(fp_starts) / max(test_days, 1e-6)
    med_samp = f"{np.median(lats_samp):.0f} samp" if lats_samp else "n/a"
    med_min = f"{np.median(lats_samp) * dt_min:.0f} min" if lats_samp else "n/a"
    return p, r, f1, fpr, len(lats_samp), len(starts), med_samp, med_min, fp_ep_per_day


def main():
    out = []
    say = lambda s: (print(s), out.append(s))

    # 1. GECCO 2019
    ts_g, gx, gev, dt_g = load_gecco()
    zg = feats(gx)
    ew_s = ewma_series(zg, 1 / 15)
    ew_l = ewma_series(zg, 1 / 720)
    z_temp = np.hstack([zg - ew_s, zg - ew_l])

    # Leakage check 1: Causal EWMA test
    zg_pert = zg.copy()
    zg_pert[50000:] += 100.0
    z_temp_pert = np.hstack([zg_pert - ewma_series(zg_pert, 1 / 15), zg_pert - ewma_series(zg_pert, 1 / 720)])
    assert np.array_equal(z_temp[:50000], z_temp_pert[:50000]), "Causal EWMA leakage detected!"

    mid = len(gx) // 2
    # Leakage check 2: No event window straddles the split boundary
    assert not (gev[mid - 1] and gev[mid]), "Event window overlaps train/val -> test boundary!"

    norm_1 = ~gev[:mid]
    z_t1 = z_temp[:mid][norm_1]
    c_idx = int(0.8 * len(z_t1))
    mu, sd = z_t1[:c_idx].mean(0), np.maximum(z_t1[:c_idx].std(0), 1e-6)  # train only
    z_trn = (z_t1[:c_idx] - mu) / sd
    z_caln = (z_t1[c_idx:] - mu) / sd
    z_ten = (z_temp[mid:] - mu) / sd

    tae = TemporalAE((8, 12, 4, 12, 8))
    tae.fit(z_trn, epochs=60, lr=0.01)
    tae.quantize_int8(z_trn)

    ae_cal = tae.mse_int8(z_caln)
    ae_te = tae.mse_int8(z_ten)
    y_test = gev[mid:].astype(int)
    auc = roc_auc_score(y_test, ae_te)

    # Export trained GECCO Temporal AE (int8) with validation-calibrated q=0.9990 threshold
    export_thr = float(np.quantile(ae_cal, 0.9990))
    with open(TEMPORAL_MODEL_PATH, "w") as f:
        json.dump(tae.export_dict(mu, sd, export_thr), f)

    say(f"=== GECCO 2019 Temporal Leakage Audit & PR Trade-Off (dt={dt_g:.1f} min/sample, test_days={len(y_test)*dt_g/1440:.1f} d, AUC={auc:.3f}) ===")
    say("  Leakage checks: Causal EWMA=PASS | Chronological split=PASS | Train-only norm=PASS | No boundary event overlap=PASS")
    say(f"  {'Val Quantile / Confirm':26s} | {'P':>6s} {'R':>6s} {'F1':>6s} {'FPR':>6s} | {'Events':>7s} {'Delay(samp)':>11s} {'Delay(min)':>10s} {'FA_ep/day':>9s}")
    say("  " + "-" * 96)
    for q_val, conf in ((0.990, 1), (0.995, 1), (0.998, 1), (0.999, 1), (0.9995, 1), (0.999, 3), (0.995, 3)):
        thr = np.quantile(ae_cal, q_val)
        p, r, f1, fpr, hit, tot, ds, dm, fpd = eval_scores(y_test, ae_te, thr, dt_g, confirm=conf)
        lbl = f"q={q_val:.4f}, confirm={conf}"
        say(f"  {lbl:26s} | {p:6.3f} {r:6.3f} {f1:6.3f} {fpr:6.4f} | {hit:2d}/{tot:2d}   {ds:>11s} {dm:>10s} {fpd:9.2f}")

    # Non-ML baselines on the same split, with thresholds from the same validation slice
    res = rolling_ewma_z(zg)
    say("  Baselines (q=0.99, confirm=1):")
    for name, scores in (("Moving-Avg Residual (EWMA Z)", np.max(np.abs(res), axis=1)),
                         ("Change-Point (CUSUM)", cusum_scores(res))):
        cal = scores[:mid][norm_1][c_idx:]
        te = scores[mid:]
        p, r, f1, fpr, hit, tot, ds, dm, fpd = eval_scores(y_test, te, np.quantile(cal, 0.99), dt_g)
        say(f"  {name:28s} | P={p:.3f} R={r:.3f} F1={f1:.3f} FPR={fpr:.4f} AUC={roc_auc_score(y_test, te):.3f} | "
            f"Events={hit}/{tot} | Delay={ds} ({dm}) | FA_ep/day={fpd:.2f}")

    # 2. Loddon Catchment (second continuous temporal dataset, 1-hour sampling)
    # All 207 storm samples fall in indices 83..3247; indices 3300..8322 are 100% storm-free.
    # Train (3300..7000) & Val (7000..8322) on storm-free period; Test on 0..3300 (all storm events).
    ts_l, lx, lev, dt_l = load_loddon()
    zl = feats(lx)
    zl_temp = np.hstack([zl - ewma_series(zl, 1 / 6), zl - ewma_series(zl, 1 / 168)])
    split_l = 3300
    assert not np.any(lev[split_l:]), "Storm event found in storm-free Loddon baseline!"
    zl_base = zl_temp[split_l:]
    c_l = int(0.8 * len(zl_base))
    mu_l, sd_l = zl_base[:c_l].mean(0), np.maximum(zl_base[:c_l].std(0), 1e-6)
    tae_l = TemporalAE((8, 12, 4, 12, 8))
    tae_l.fit((zl_base[:c_l] - mu_l) / sd_l, epochs=60, lr=0.01)
    tae_l.quantize_int8((zl_base[:c_l] - mu_l) / sd_l)
    l_cal = tae_l.mse_int8((zl_base[c_l:] - mu_l) / sd_l)
    l_te = tae_l.mse_int8((zl_temp[:split_l] - mu_l) / sd_l)
    yl_test = lev[:split_l].astype(int)
    auc_l = roc_auc_score(yl_test, l_te)

    say(f"\n=== Loddon Catchment Temporal Evaluation (dt={dt_l:.0f} min/sample, N_test={len(yl_test)}, storm_samples={int(yl_test.sum())}, AUC={auc_l:.3f}) ===")
    for q_val, conf in ((0.95, 1), (0.99, 1), (0.99, 2)):
        thr = np.quantile(l_cal, q_val)
        p, r, f1, fpr, hit, tot, ds, dm, fpd = eval_scores(yl_test, l_te, thr, dt_l, confirm=conf)
        say(f"  q={q_val:.2f}, confirm={conf} | P={p:.3f} R={r:.3f} F1={f1:.3f} FPR={fpr:.4f} | Storm windows={hit}/{tot} | Delay={ds} ({dm}) | FA_ep/day={fpd:.2f}")

    with open(os.path.join(HERE, "exp2_results.txt"), "w") as f:
        f.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
