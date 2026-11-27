"""Phase 5B & 5C: Cache County Real Household Evaluation of 4 Consumption Detectors.

Chronological split per home (Sites 003, 006, 010, 020, 031):
  - Days 1..7:   Train (establish household hourly & LZ78 baselines)
  - Days 8..14:  Validation (verify normal false-positive behaviour)
  - Days 15..30: Held-out Test (16 untouched days/home = 80 house-days)

Also evaluates controlled anomaly injections on held-out Days 15..30 of Sites 006, 010, 020
(clearly separated from naturally observed consumption).
Outputs JSON to abnormal_consumption/results/eval_cache_county.json.
"""
import json
import os
import sys
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [HERE]

from detectors import (  # noqa: E402
    StatisticalBaselineDetector,
    StatisticalZeroFlowDetector,
    LZCompressionDetector,
    CombinedConsumptionDetector,
    DAY_S,
    EVENT_TYPES,
)

DATA = os.path.join(ROOT, "simulation", "data", "cache_county")
GAL_TO_L = 3.785411784
DETECTORS = (
    ("statistical_baseline", StatisticalBaselineDetector),
    ("statistical_zero_flow", StatisticalZeroFlowDetector),
    ("lz_compression", LZCompressionDetector),
    ("combined", CombinedConsumptionDetector),
)


def load_site_1min(sid, res_gal, days=30):
    df = pd.read_csv(os.path.join(DATA, f"site{sid:03d}qc_data.csv"))
    df["t"] = pd.to_datetime(df["Time"])
    m1 = df.set_index("t")["Pulses"].resample("1min").sum().fillna(0.0) * res_gal * GAL_TO_L / 60.0
    return m1.values[: min(len(m1), days * 1440)]


def inject_heldout(arr, scen, seed):
    out = arr.copy()
    rng = np.random.default_rng(7000 + seed)
    base_m = 16 * 1440  # Day 17 (inside held-out Days 15..30)
    if scen == "UNUSUAL_HIGH_CONSUMPTION":
        s, e = base_m + 10 * 60, base_m + 10 * 60 + 75
        out[s:e] += 0.22
        return out, s, e
    if scen == "UNUSUAL_NIGHT_CONSUMPTION":
        s, e = base_m + 2 * 60, base_m + 2 * 60 + 25
        out[s:e] += 0.14
        return out, s, e
    if scen == "PROLONGED_ZERO_FLOW":
        s, e = base_m, base_m + 25 * 60
        out[s:e] = 0.0
        return out, s, e
    if scen == "SUDDEN_CONSUMPTION_CHANGE":
        s, e = base_m + 7 * 60, base_m + 19 * 60
        out[s:e] = out[s:e] * 2.5 + 0.03
        return out, s, e
    if scen == "IRREGULAR_BURST_PATTERN":
        s, e = base_m + 13 * 60, base_m + 16 * 60
        for m in range(s, e):
            out[m] = float(rng.choice([0.0, 0.05, 0.14, 0.30], p=[0.30, 0.30, 0.25, 0.15]))
        return out, s, e
    raise ValueError(scen)


def main():
    sites = pd.read_csv(os.path.join(DATA, "sites.csv")).set_index("SiteID")
    sids = [3, 6, 10, 20, 31]
    series = {sid: load_site_1min(sid, float(sites.loc[sid, "MeterResolution"]), days=30) for sid in sids}

    natural_fp = {}
    injected = {}
    for det_name, det_cls in DETECTORS:
        nat_rows = []
        for sid in sids:
            arr = series[sid]
            det = det_cls(learn_s=7 * DAY_S)  # Days 1..7 train
            val_evs, test_evs = [], []
            for m, f in enumerate(arr):
                evs = det.step(m * 60.0, float(f))
                for ev_type, _ in evs:
                    if 7 * 1440 <= m < 14 * 1440:
                        val_evs.append(ev_type)
                    elif m >= 14 * 1440:
                        test_evs.append(ev_type)
            test_days = (len(arr) - 14 * 1440) / 1440.0
            nat_rows.append({
                "site_id": sid,
                "irrigation": str(sites.loc[sid, "IrrigationType"]),
                "val_alerts": len(val_evs),
                "test_alerts": len(test_evs),
                "test_fp_per_day": round(len(test_evs) / max(test_days, 1e-9), 4),
            })
        natural_fp[det_name] = nat_rows

        inj_summary = {ev: {"tp": 0, "fn": 0, "latencies": []} for ev in EVENT_TYPES}
        for sid in (6, 10, 20):
            base = series[sid]
            for ev in EVENT_TYPES:
                arr, s, e = inject_heldout(base, ev, sid)
                det = det_cls(learn_s=7 * DAY_S)
                hits = []
                for m, f in enumerate(arr):
                    for ev_type, _ in det.step(m * 60.0, float(f)):
                        if ev_type == ev and s <= m <= e + 60:
                            hits.append(m - s)
                if hits:
                    inj_summary[ev]["tp"] += 1
                    inj_summary[ev]["latencies"].append(hits[0])
                else:
                    inj_summary[ev]["fn"] += 1
        injected[det_name] = inj_summary

    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    out = {"natural_fp": natural_fp, "injected_heldout": injected}
    out_path = os.path.join(HERE, "results", "eval_cache_county.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print("Saved Cache County consumption eval to", out_path)
    for name in natural_fp:
        tot_test = sum(r["test_alerts"] for r in natural_fp[name])
        tot_indoor = sum(r["test_alerts"] for r in natural_fp[name] if r["site_id"] in (6, 10, 20))
        print(f"\n[{name}] natural_test_alerts(all 5)={tot_test}, (006/010/020)={tot_indoor}")
        for ev, d in injected[name].items():
            print(f"  INJ {ev:28s}: TP={d['tp']}/3 FN={d['fn']} lats={d['latencies']}")


if __name__ == "__main__":
    main()
