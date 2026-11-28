"""Cache County Household Consumption Validation (Evaluation Only).

Evaluates the downloaded HydroShare dataset (0b72cddfc51c45b188e0e6cd8927227e):
  - Audits sampling interval, units (pulses -> gal -> L/s), coverage, gaps, NaNs,
    and end-use labels (verifying no independent pipe-leak ground truth exists).
  - Resamples 4-second pulse counts to 60-second flow_lps. Minutes with no rows are recording
    gaps and stay missing (NaN), not zero. Each series starts at its first midnight, so the
    firmware's night and hourly rules see the right clock hours.
  - Measures the longest run of minutes with flow > CONT_FLOW_LPS over each full record
    (the evidence for CONT_FLOW_MIN).
  - Chronological split of the first 30 calendar days: Day 1 = LEARN_S (24h), Days 2..30 =
    held-out evaluation. Test days are counted from minutes that actually have data.
  - Evaluates false-positive rates on normal household demand across Sites 003, 006, 010, 020, 031.
  - Injects documented 0.05 L/s and 0.5 L/s synthetic leaks into held-out days of
    sites 006, 010, 020 to verify flow-based detection & latency. An injection is scored only
    if the 24 h after its start are at least 99% recorded, and only alerts that the same data
    without the leak does not raise count as detections.

Outputs JSON summary to tinyml/leakage/results/validation_cache_county.json.
"""
import json
import os
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

from edge.aqua_edge import FlatNode, DAY, CONT_FLOW_LPS, CONT_FLOW_MIN  # noqa: E402
from gateway.core import Gateway, Topology  # noqa: E402
from tests.offline_radio import exchange  # noqa: E402
from hal.packet import sim_key  # noqa: E402

DATA = os.path.join(ROOT, "simulation", "data", "cache_county")
GAL_TO_L = 3.785411784
LEAK_ALERTS = {"LEAK_NIGHT_FLOW", "LEAK_CONTINUOUS_FLOW", "LEAK_PRESSURE_DROP"}
CONSUMPTION_ALERTS = {"ABNORMAL_CONSUMPTION"}


class SimHAL:
    storage_path = None
    key = staticmethod(sim_key)

    def __init__(self, name, clock, readings, inboxes):
        self.name, self.clock, self.readings, self.inboxes = name, clock, readings, inboxes
        inboxes[name] = []

    def now(self):
        return self.clock[0]

    def read(self):
        return self.readings[0][self.name]

    def send(self, msg, cad=False):
        for n, box in self.inboxes.items():
            if n != self.name:
                box.append(msg)

    def receive(self):
        msgs, self.inboxes[self.name] = self.inboxes[self.name], []
        return msgs

    def led(self, colour):
        pass


def run_flow_series(flow_lps_arr, inject_lps=0.0, inject_start_min=None):
    clock, readings, inboxes = [0.0], [None], {}
    hal = lambda n: SimHAL(n, clock, readings, inboxes)
    node = FlatNode("B-101", hal("B-101"))
    gw = Gateway(hal("GATEWAY"), Topology.load().only(["B-101"]))

    for m, base_f in enumerate(flow_lps_arr):
        t = m * 60.0
        clock[0] = t
        f = float(base_f)
        if inject_start_min is not None and m >= inject_start_min:
            f += inject_lps
        readings[0] = {"B-101": {"flow_lps": f, "pressure_m": 15.0}}
        node.step()
        exchange([node], gw, inboxes)

    leak_alerts = [m for m in gw.log if m["k"] == "ALERT" and m["a"] in LEAK_ALERTS]
    cons_alerts = [m for m in gw.log if m["k"] == "ALERT" and m["a"] in CONSUMPTION_ALERTS]
    return leak_alerts, cons_alerts


def to_minutes(df, res_gal):
    """4 s pulse rows -> L/s per minute from the first midnight; unrecorded minutes stay NaN."""
    m1 = df.set_index("t")["Pulses"].resample("1min").sum(min_count=1) * res_gal * GAL_TO_L / 60.0
    return m1[m1.index >= m1.index[0].ceil("D")]


def continuous_runs(m1):
    """Lengths (min) of unbroken runs with flow above the limit; a missing minute breaks a run."""
    on = m1 > CONT_FLOW_LPS
    runs = on.groupby((~on).cumsum()).sum()
    return runs[runs > 0]


def main():
    sites = pd.read_csv(os.path.join(DATA, "sites.csv")).set_index("SiteID")
    sids = [3, 6, 10, 20, 31]
    audit = []
    fp_eval = []
    inj_eval = []

    for sid in sids:
        res_gal = float(sites.loc[sid, "MeterResolution"])
        irr = str(sites.loc[sid, "IrrigationType"])
        df = pd.read_csv(os.path.join(DATA, f"site{sid:03d}qc_data.csv"))
        t = pd.to_datetime(df["Time"])
        dt = t.diff().dt.total_seconds().dropna()
        nans = int(df["Pulses"].isna().sum())
        span_days = float((t.iloc[-1] - t.iloc[0]).total_seconds() / 86400.0)
        df["t"] = t
        m1 = to_minutes(df, res_gal)
        runs = continuous_runs(m1)

        ev = pd.read_csv(os.path.join(DATA, f"LabelledEvents_site_{sid:03d}.csv"))
        labels = ev["Label"].value_counts().to_dict() if "Label" in ev.columns else {}
        has_leak_label = any("leak" in str(k).lower() for k in labels)

        audit.append({
            "site_id": sid,
            "irrigation": irr,
            "span_days_incl_gaps": round(span_days, 1),
            "days_with_data": round(int(m1.notna().sum()) / 1440.0, 1),
            "max_continuous_flow_min": int(runs.max()),
            "runs_reaching_limit": int((runs >= CONT_FLOW_MIN).sum()),
            "dt_mode_s": float(dt.mode().iloc[0]),
            "gaps_gt_4s": int((dt > 4).sum()),
            "nan_pulses": nans,
            "has_leak_label": has_leak_label,
            "event_labels": {str(k): int(v) for k, v in labels.items()},
        })

        # Evaluate first 30 days (Day 1 = LEARN_S, Days 2..30 = 29 test days)
        n_min = min(len(m1), 30 * 1440)
        arr = m1.values[:n_min]
        eval_days = int(m1.iloc[1440:n_min].notna().sum()) / 1440.0
        leaks, cons = run_flow_series(arr)
        by_type = {}
        for m in leaks:
            by_type[m["a"]] = by_type.get(m["a"], 0) + 1
        fp_eval.append({
            "site_id": sid,
            "irrigation": irr,
            "eval_days": round(eval_days, 1),
            "leak_alerts": len(leaks),
            "leak_fp_per_day": round(len(leaks) / eval_days, 4) if eval_days else None,
            "by_type": by_type,
            "consumption_alerts": len(cons),
        })

        # Controlled leak injection on sites 6, 10 and 20
        if sid in (6, 10, 20):
            # 14-day slice (Day 1 learn, inject at Day 3 10:00 = min 2880 + 600 = 3480)
            sl = m1.values[: min(len(m1), 14 * 1440)]
            inj_min = 2 * 1440 + 600
            coverage = float(m1.iloc[inj_min : inj_min + 1440].notna().mean())
            base_alerts = {(m["t"], m["a"]) for m in run_flow_series(sl)[0]}
            for lps in (0.05, 0.5):
                if coverage < 0.99:
                    inj_eval.append({"site_id": sid, "inject_lps": lps, "scored": False,
                                     "coverage_24h": round(coverage, 3)})
                    continue
                la, _ = run_flow_series(sl, inject_lps=lps, inject_start_min=inj_min)
                post = [m for m in la if m["t"] >= inj_min * 60 and (m["t"], m["a"]) not in base_alerts]
                pre = [m for m in la if m["t"] < inj_min * 60]
                first = post[0] if post else None
                inj_eval.append({
                    "site_id": sid,
                    "inject_lps": lps,
                    "scored": True,
                    "coverage_24h": round(coverage, 3),
                    "detected": first is not None,
                    "mechanism": first["a"] if first else None,
                    "latency_min": round((first["t"] - inj_min * 60) / 60.0, 1) if first else None,
                    "pre_leak_fp": len(pre),
                })

    out = {"audit": audit, "false_positive_eval": fp_eval, "injected_leak_eval": inj_eval}
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    out_path = os.path.join(HERE, "results", "validation_cache_county.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print("Saved Cache County validation to", out_path)
    for r in audit:
        print("AUDIT:", {k: v for k, v in r.items() if k != "event_labels"})
    for r in fp_eval:
        print("FP:", r)
    for r in inj_eval:
        print("INJ:", r)


if __name__ == "__main__":
    main()
