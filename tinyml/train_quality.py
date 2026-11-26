"""Build the data, train, quantize to 8 bits and test the tank node's water-quality model.

    uv run python tinyml/train_quality.py      # about 1 minute; needs numpy

Writes firmware/quality_model.json (the model the node runs) and tinyml/results.txt.
Fails loudly (assert) if any test drops below its pass mark. Method and sources: tinyml/README.md.

Data (download into tinyml/data/, see README):
  - Kenya water sources [Ooko et al., CC BY 4.0]: real pH, TDS, turbidity readings. "Safe" rows
    (treated tap and bottled water) are our NORMAL water; "Unsafe" rows test detection.
  - GECCO 2019 [Thuringer Fernwasserversorgung, CC BY 4.0]: real readings from a German water
    utility, a different site: tests false alarms on normal water and its labelled events.
No public data has runoff or chemical labels, so those samples are made by mixing real tap water
with rainwater runoff or an alkaline cleaning chemical (bleach, caustic soda), and labelled with
the Indian drinking-water standard IS 10500:2012: a mixture is unsafe if it breaks a limit.
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
from quality_model import QualityModel  # noqa: E402  (the exact code the node runs)

CLASSES = ["NORMAL", "RUNOFF", "CHEMICAL"]
PH_LO, PH_HI, TDS_MAX, TURB_MAX = 6.5, 8.5, 500, 5      # IS 10500: pH no relaxation, TDS acceptable,
                                                        # turbidity permissible (Kenya tap water reaches 4.5 NTU)
COND_TO_TDS = 0.65                                      # TDS (ppm) ~ 0.65 x conductivity (uS/cm)
MODEL_PATH = os.path.join(ROOT, "firmware", "quality_model.json")
rng = np.random.default_rng(7)


# ---------------------------------------------------------------- data
def load_kenya():
    with open(os.path.join(HERE, "data", "kenya", "water_quality_dataset1.csv")) as f:
        rows = list(csv.DictReader(f))
    x = np.array([[float(r["pH"]), float(r["TDS_ppm"]), float(r["Turbidity_NTU"])] for r in rows])
    return x, np.array([r["Label"] == "Safe" for r in rows])


def load_gecco():
    with open(os.path.join(HERE, "data", "gecco", "5_gecco2019_all_water_quality.csv")) as f:
        rows = [r for r in csv.DictReader(f) if "NA" not in (r["pH"], r["Cond"], r["Turb"])]
    x = np.array([[float(r["pH"]), float(r["Cond"]) * 1e4 * COND_TO_TDS, float(r["Turb"])] for r in rows])
    return x, np.array([r["Event"] == "TRUE" for r in rows])   # Cond in S/m; 1 S/m = 10,000 uS/cm


def unsafe(x):
    return (x[:, 0] < PH_LO) | (x[:, 0] > PH_HI) | (x[:, 1] > TDS_MAX) | (x[:, 2] > TURB_MAX)


def mix_ph(ph_a, ph_b, f):
    """pH of (1-f) parts of water a with f parts of water b: mix the excess acid (H+ minus OH-)."""
    net = (1 - f) * (10 ** -ph_a - 10 ** (ph_a - 14)) + f * (10 ** -ph_b - 10 ** (ph_b - 14))
    return -np.log10((net + np.sqrt(net ** 2 + 4e-14)) / 2)
    # ponytail: no buffering by the water's own alkalinity, so small chemical doses move pH more
    # than in real tap water; labels come from the resulting reading, so they stay consistent


def logu(lo, hi, n):
    return np.exp(rng.uniform(math.log(lo), math.log(hi), n))


def make_samples(base, n):
    """Mix real normal water with runoff or chemical; label by IS 10500. Returns x, y (class index)."""
    b = base[rng.integers(len(base), size=n)].copy()
    b[:, 1] *= rng.uniform(0.8, 2.5, n)                 # Indian supplies run 100 to 400 ppm; Kenya tap 30-210
    kind = rng.integers(3, size=n)                      # 0 none, 1 runoff, 2 chemical
    # runoff: rain is pH ~5.6; urban runoff carries soil (turbid) but few dissolved salts
    end = {1: (rng.uniform(5.6, 7.0, n), rng.uniform(10, 150, n), logu(10, 300, n), logu(0.003, 0.9, n)),
           # alkaline cleaner: diluted bleach or caustic soda, pH 10.5-12.5, 2-60 g/L dissolved
           2: (rng.uniform(10.5, 12.5, n), logu(2000, 60000, n), rng.uniform(0.2, 3, n), logu(1e-4, 0.05, n))}
    x = b.copy()
    for k, (ph, tds, turb, f) in end.items():
        m = kind == k
        x[m, 0] = mix_ph(b[m, 0], ph[m], f[m])
        x[m, 1] = (1 - f[m]) * b[m, 1] + f[m] * tds[m]
        x[m, 2] = (1 - f[m]) * b[m, 2] + f[m] * turb[m]
    x[:, 0] += rng.normal(0, 0.05, n)                   # cheap sensor noise
    x[:, 1] *= rng.normal(1, 0.03, n)
    x[:, 2] = np.abs(x[:, 2] + rng.normal(0, 0.2, n))
    bad = unsafe(x)
    y = np.where(bad, kind, 0)
    keep = ~(bad & (kind == 0))                         # clean base water pushed over a limit: unclear, drop
    x, y = x[keep], y[keep]
    per = min(np.bincount(y, minlength=3))              # balance the classes
    idx = np.concatenate([rng.choice(np.flatnonzero(y == c), per, replace=False) for c in range(3)])
    return x[idx], y[idx]


def feats(x):
    return np.column_stack([x[:, 0], np.log1p(np.maximum(x[:, 1], 0)), np.log1p(np.maximum(x[:, 2], 0))])


# ---------------------------------------------------------------- model: 3 -> 16 -> 8 -> 3
def train(z, y, sizes=(3, 16, 8, 3), epochs=60, lr=0.01, batch=128):
    W = [rng.normal(0, math.sqrt(2 / a), (a, b)) for a, b in zip(sizes[:-1], sizes[1:])]
    B = [np.zeros(b) for b in sizes[1:]]
    params = W + B
    m1 = [np.zeros_like(p) for p in params]
    m2 = [np.zeros_like(p) for p in params]
    Y, t = np.eye(3)[y], 0
    for _ in range(epochs):
        order = rng.permutation(len(z))
        for s in range(0, len(z), batch):
            i = order[s:s + batch]
            acts = forward(W, B, z[i])
            d = (softmax(acts[-1]) - Y[i]) / len(i)
            gW, gB = [None] * 3, [None] * 3
            for l in (2, 1, 0):
                gW[l], gB[l] = acts[l].T @ d, d.sum(0)
                if l:
                    d = (d @ W[l].T) * (acts[l] > 0)
            t += 1
            for p, g, a, v in zip(params, gW + gB, m1, m2):      # Adam
                a[:] = 0.9 * a + 0.1 * g
                v[:] = 0.999 * v + 0.001 * g * g
                p -= lr * (a / (1 - 0.9 ** t)) / (np.sqrt(v / (1 - 0.999 ** t)) + 1e-8)
    return W, B


def forward(W, B, z):
    acts = [z]
    for l, (w, b) in enumerate(zip(W, B)):
        a = acts[-1] @ w + b
        acts.append(np.maximum(a, 0) if l < 2 else a)
    return acts


def softmax(a):
    e = np.exp(a - a.max(1, keepdims=True))
    return e / e.sum(1, keepdims=True)


def quantize(W, B, zcal):
    """Post-training int8 quantization (per-tensor, symmetric), scales from a calibration set,
    like TFLite's representative dataset."""
    acts = forward(W, B, zcal)
    s_in = np.percentile(np.abs(zcal), 99.9) / 127
    s, layers = s_in, []
    for l, (w, b) in enumerate(zip(W, B)):
        sw = np.abs(w).max() / 127
        s_out = np.percentile(acts[l + 1], 99.9) / 127 if l < 2 else None
        layers.append({"w": np.round(w / sw).astype(int).tolist(),
                       "b": np.round(b / (s * sw)).astype(int).tolist(),
                       "m": float(s * sw / s_out) if s_out else None})
        s = s_out
    return float(s_in), layers


# ---------------------------------------------------------------- main
def main():
    t0 = time.time()
    kx, ksafe = load_kenya()
    gx, gev = load_gecco()
    safe = kx[ksafe]
    split = int(0.7 * len(safe))                         # tap water for training vs for testing: never shared
    xtr, ytr = make_samples(safe[:split], 90000)
    xte, yte = make_samples(safe[split:], 30000)
    ztr = feats(xtr)
    mean, std = ztr.mean(0), ztr.std(0)
    W, B = train((ztr - mean) / std, ytr)
    in_scale, layers = quantize(W, B, (ztr - mean) / std)
    with open(MODEL_PATH, "w") as f:
        json.dump({"classes": CLASSES, "mean": mean.tolist(), "std": std.tolist(),
                   "in_scale": in_scale, "layers": layers}, f)
    model = QualityModel(MODEL_PATH)
    q = lambda x: np.array([CLASSES.index(model.classify(*r)) for r in x])
    pf = lambda x: forward(W, B, (feats(x) - mean) / std)[-1].argmax(1)
    out = []
    say = lambda s: (print(s), out.append(s))

    # 1. Mixtures built on tap water the model never saw
    pq, pfl = q(xte), pf(xte)
    acc_f, acc_q, agree = (pfl == yte).mean(), (pq == yte).mean(), (pq == pfl).mean()
    say(f"1. Held-out mixtures ({len(yte)} samples, unseen tap water): float {acc_f:.4f}, int8 {acc_q:.4f}, "
        f"int8 agrees with float on {agree:.4f}")
    cm = np.zeros((3, 3), int)
    np.add.at(cm, (yte, pq), 1)
    say("   confusion (rows = true, cols = int8 model): " + "; ".join(
        f"{CLASSES[i]} {cm[i].tolist()}" for i in range(3)))

    # 2. Real readings: Kenya tap water not used for training, and real unsafe water
    fa = (q(safe[split:]) != 0).mean()
    say(f"2. Real Kenya safe water (test part, {len(safe) - split} readings): false alarms {fa:.4f}")
    ux = kx[~ksafe]
    pu = q(ux)
    over = unsafe(ux)
    say(f"   Real Kenya unsafe water ({len(ux)} readings, rivers, boreholes, sea): flagged {np.mean(pu != 0):.4f}; "
        f"of the {over.sum()} that break an IS 10500 limit, flagged {np.mean(pu[over] != 0):.4f}; "
        f"classes {dict(zip(CLASSES, np.bincount(pu, minlength=3).tolist()))}")

    # 3. A different site: German utility water (GECCO 2019)
    pg = q(gx)
    norm = ~gev
    in_lim = norm & ~unsafe(gx)
    say(f"3. GECCO normal readings ({norm.sum()}): called NORMAL {np.mean(pg[norm] == 0):.4f}; "
        f"of those inside IS 10500 limits ({in_lim.sum()}): {np.mean(pg[in_lim] == 0):.4f}; "
        f"pH above 8.5 in {np.mean(gx[norm, 0] > PH_HI):.4f} of normal readings")
    starts = np.flatnonzero(gev & ~np.r_[False, gev[:-1]])
    ends = np.flatnonzero(gev & ~np.r_[gev[1:], False])
    hit = sum(np.any(pg[a:b + 1] != 0) for a, b in zip(starts, ends))
    say(f"   GECCO labelled events: {hit} of {len(starts)} flagged, but not a fair score: the site's pH is "
        f"above the Indian limit most of the time anyway (event readings breaking a limit: "
        f"{np.mean(unsafe(gx[gev])):.4f}). The model follows IS 10500, not this utility's own normal")

    # 4. Sensor drift on the held-out mixtures (cheap probes drift between calibrations)
    drift = []
    for name, dx in (("pH +0.2", (0.2, 1, 0)), ("pH -0.2", (-0.2, 1, 0)), ("TDS +10%", (0, 1.1, 0)),
                     ("TDS -10%", (0, 0.9, 0)), ("turbidity +1 NTU", (0, 1, 1))):
        x2 = xte.copy()
        x2[:, 0] += dx[0]; x2[:, 1] *= dx[1]; x2[:, 2] += dx[2]
        y2 = np.where(unsafe(x2) | (yte != 0), yte, 0)            # label still follows the true water
        drift.append(f"{name} {np.mean(q(x2) == y2):.4f}")
    say("4. Accuracy with sensor drift: " + ", ".join(drift))

    # 5. The simulation's quality profiles (simulation/webots/make_scenario.py QUALITY)
    sim = {"NORMAL": (7.2, 250, 0.5), "RUNOFF": (6.4, 60, 25), "CHEMICAL": (8.8, 900, 0.6)}
    sim_ok = {}
    for want, (ph, tds, turb) in sim.items():
        xs = np.column_stack([rng.normal(ph, 0.05, 500), rng.normal(tds, 5, 500), np.abs(rng.normal(turb, 0.1, 500))])
        sim_ok[want] = float(np.mean(q(xs) == CLASSES.index(want)))
    say(f"5. Simulation profiles classified correctly: {sim_ok}")

    # 6. Size and speed on the node
    n_w = sum(len(l["w"]) * len(l["w"][0]) for l in layers)
    n_b = sum(len(l["b"]) for l in layers)
    t1 = time.perf_counter()
    for r in xte[:2000]:
        model.classify(*r)
    us = (time.perf_counter() - t1) / 2000 * 1e6
    say(f"6. Model: {n_w} int8 weights + {n_b} int32 biases = {n_w + 4 * n_b} bytes; "
        f"file {os.path.getsize(MODEL_PATH)} bytes; {us:.0f} us per reading on this laptop "
        f"(runs on Raspberry Pi 4 in under 1 ms; one reading a minute)")
    say(f"Done in {time.time() - t0:.0f} s")
    with open(os.path.join(HERE, "results.txt"), "w") as f:
        f.write("\n".join(out) + "\n")

    # pass marks
    assert acc_q >= 0.97 and agree >= 0.99, (acc_q, agree)
    assert fa <= 0.01, fa
    assert np.mean(pu[over] != 0) >= 0.95
    assert min(sim_ok.values()) == 1.0, sim_ok
    print("tinyml checks passed")


if __name__ == "__main__":
    main()
