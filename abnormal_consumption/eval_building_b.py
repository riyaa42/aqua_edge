"""Phase 5A & 6: Building B Bursty-Demand Evaluation of 4 Abnormal-Consumption Detectors.

Uses build_society.py independent bursty demand patterns:
  - Validation seeds: 101, 202 (used only to verify threshold sanity)
  - Held-out test seeds: 303, 404, 505 across 3 flats (B-401, B-203, B-G01) = 9 flat-series per scenario

Evaluates all 5 anomaly categories + normal operation across all 4 approaches,
plus per-step CPU latency and RAM footprint.
Outputs JSON to abnormal_consumption/results/eval_building_b.json.
"""
import json
import os
import sys
import tempfile
import time
import tracemalloc
import numpy as np
import wntr

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, os.path.join(ROOT, "simulation", "scripts")]

import build_society as S  # noqa: E402
from detectors import (  # noqa: E402
    StatisticalBaselineDetector,
    StatisticalZeroFlowDetector,
    LZCompressionDetector,
    CombinedConsumptionDetector,
    EVENT_TYPES,
)

DETECTORS = (
    ("statistical_baseline", StatisticalBaselineDetector),
    ("statistical_zero_flow", StatisticalZeroFlowDetector),
    ("lz_compression", LZCompressionDetector),
    ("combined", CombinedConsumptionDetector),
)
FLATS = ("B-401", "B-203", "B-G01")
TEST_SEEDS = (303, 404, 505)


def get_base_flows(seed, hours=72):
    days = max(2, (hours + 23) // 24)
    S.BUILDINGS = ["B"]
    wn = S.build(leak=False, bursty=True, seed=seed)
    rng = np.random.default_rng(seed)
    for jname in wn.junction_name_list:
        if jname[-1].isdigit() and jname[-2] == "0":
            wn.get_pattern(f"{jname}_pat").multipliers = np.array(
                S.make_bursty_pattern(rng, days=days, step_min=5)
            )
    wn.options.time.duration = hours * 3600 - 60
    wn.options.time.hydraulic_timestep = 60
    wn.options.time.report_timestep = 60
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        res = wntr.sim.EpanetSimulator(wn).run_sim(file_prefix=os.path.join(td, "sim"))
    return {f: res.link["flowrate"][f"{f}-P"].values * 1000.0 for f in FLATS}


def inject_scenario(arr, scenario, seed):
    out = arr.copy()
    rng = np.random.default_rng(9000 + seed)
    if scenario == "normal":
        return out, None, None
    if scenario == "UNUSUAL_HIGH_CONSUMPTION":
        start_m, end_m = 34 * 60, 34 * 60 + 75  # 10:00 Day 2, 75 min
        out[start_m:end_m] += 0.18
        return out, start_m, end_m
    if scenario == "UNUSUAL_NIGHT_CONSUMPTION":
        start_m, end_m = 26 * 60, 26 * 60 + 25  # 02:00 Day 2, 25 min
        out[start_m:end_m] += 0.12
        return out, start_m, end_m
    if scenario == "PROLONGED_ZERO_FLOW":
        start_m, end_m = 24 * 60, 49 * 60  # 25 h continuous zero-flow
        out[start_m:end_m] = 0.0
        return out, start_m, end_m
    if scenario == "SUDDEN_CONSUMPTION_CHANGE":
        start_m, end_m = 31 * 60, 43 * 60  # 07:00-19:00 Day 2 (12 h)
        out[start_m:end_m] = out[start_m:end_m] * 2.5 + 0.025
        return out, start_m, end_m
    if scenario == "IRREGULAR_BURST_PATTERN":
        start_m, end_m = 37 * 60, 40 * 60  # 13:00-16:00 Day 2 (3 h)
        for m in range(start_m, end_m):
            # Rapidly switching erratic fixture pattern
            out[m] = float(rng.choice([0.0, 0.04, 0.12, 0.28], p=[0.30, 0.30, 0.25, 0.15]))
        return out, start_m, end_m
    raise ValueError(scenario)


def evaluate():
    series_by_seed = {s: get_base_flows(s, hours=72) for s in TEST_SEEDS}
    scenarios = ("normal",) + EVENT_TYPES
    summary = {}
    resources = {}

    for det_name, det_cls in DETECTORS:
        per_cat = {ev: {"tp": 0, "fp": 0, "fn": 0, "latencies": [], "repeats": 0} for ev in EVENT_TYPES}
        normal_fp = 0
        normal_days = 0

        # Resource benchmark on one 72h series
        tracemalloc.start()
        det0 = det_cls()
        t0 = time.perf_counter()
        arr0 = series_by_seed[TEST_SEEDS[0]][FLATS[0]]
        for m, f in enumerate(arr0):
            det0.step(m * 60.0, float(f))
        dt_s = time.perf_counter() - t0
        _, peak_ram = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        resources[det_name] = {
            "us_per_step": round(dt_s / len(arr0) * 1e6, 2),
            "peak_ram_kb": round(peak_ram / 1024.0, 2),
        }

        for seed in TEST_SEEDS:
            for flat in FLATS:
                base = series_by_seed[seed][flat]
                for scen in scenarios:
                    arr, start_m, end_m = inject_scenario(base, scen, seed)
                    det = det_cls()
                    fired = []
                    for m, f in enumerate(arr):
                        evs = det.step(m * 60.0, float(f))
                        for ev_type, _ in evs:
                            fired.append((m, ev_type))

                    if scen == "normal":
                        normal_days += (len(arr) - 1440) / 1440.0
                        normal_fp += len(fired)
                        for _, ev_type in fired:
                            per_cat[ev_type]["fp"] += 1
                    else:
                        # Event-level scoring for the injected target category
                        hits = [m for m, ev in fired if ev == scen and start_m <= m <= end_m + 60]
                        pre = [m for m, ev in fired if ev == scen and m < start_m]
                        other_fp = [m for m, ev in fired if ev != scen and m < start_m]
                        per_cat[scen]["fp"] += len(pre) + len(other_fp)
                        if hits:
                            per_cat[scen]["tp"] += 1
                            per_cat[scen]["latencies"].append(hits[0] - start_m)
                            per_cat[scen]["repeats"] += max(0, len(hits) - 1)
                        else:
                            per_cat[scen]["fn"] += 1

        cat_metrics = {}
        for ev, d in per_cat.items():
            tp, fp, fn = d["tp"], d["fp"], d["fn"]
            prec = tp / (tp + fp) if (tp + fp) else (1.0 if tp == 0 and fn == 0 else 0.0)
            rec = tp / (tp + fn) if (tp + fn) else 0.0
            f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
            lats = sorted(d["latencies"])
            cat_metrics[ev] = {
                "tp": tp, "fp": fp, "fn": fn,
                "precision": round(prec, 3),
                "recall": round(rec, 3),
                "f1": round(f1, 3),
                "median_latency_min": lats[len(lats) // 2] if lats else None,
                "repeats": d["repeats"],
            }
        summary[det_name] = {
            "normal_fp": normal_fp,
            "normal_fp_per_flat_day": round(normal_fp / max(normal_days, 1e-9), 4),
            "categories": cat_metrics,
        }

    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    out = {"test_seeds": list(TEST_SEEDS), "flats": list(FLATS), "summary": summary, "resources": resources}
    out_path = os.path.join(HERE, "results", "eval_building_b.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print("Saved Building B evaluation to", out_path)
    for name, s in summary.items():
        print(f"\n[{name}] normal_fp={s['normal_fp']} ({s['normal_fp_per_flat_day']}/flat/day) | {resources[name]}")
        for ev, m in s["categories"].items():
            print(f"  {ev:28s}: P={m['precision']:.2f} R={m['recall']:.2f} F1={m['f1']:.2f} TP={m['tp']} FP={m['fp']} FN={m['fn']} med_lat={m['median_latency_min']}")


if __name__ == "__main__":
    evaluate()
