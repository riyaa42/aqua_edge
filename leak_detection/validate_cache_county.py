"""Cache County Household Consumption Validation (Evaluation Only).

Evaluates the downloaded HydroShare dataset (0b72cddfc51c45b188e0e6cd8927227e):
  - Audits sampling interval, units (pulses -> gal -> L/s), coverage, gaps, NaNs,
    and end-use labels (verifying no independent pipe-leak ground truth exists).
  - Resamples 4-second pulse counts to 60-second flow_lps.
  - Chronological split: Day 1 = LEARN_S (24h), Days 2..N = held-out evaluation.
  - Evaluates false-positive rates on normal household demand across Sites 003, 006, 010, 020, 031.
  - Injects documented 0.05 L/s and 0.5 L/s synthetic leaks into held-out days of
    indoor-focused sites (006, 010, 020) to verify flow-based detection & latency.

Outputs JSON summary to leak_detection/results/validation_cache_county.json.
"""
import json
import os
import sys
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [os.path.join(ROOT, "firmware")]

from aqua_edge import FlatNode, DAY  # noqa: E402
from gateway import Gateway  # noqa: E402
from packet import sim_key  # noqa: E402

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

    def send(self, msg, at_s=None, cad=False):
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
    gw = Gateway(hal("GATEWAY"), ["B-101"], "B-TANK")

    for m, base_f in enumerate(flow_lps_arr):
        t = m * 60.0
        clock[0] = t
        f = float(base_f)
        if inject_start_min is not None and m >= inject_start_min:
            f += inject_lps
        readings[0] = {"B-101": {"flow_lps": f, "pressure_m": 15.0}}
        node.step()
        gw.step()

    leak_alerts = [m for m in gw.log if m["k"] == "ALERT" and m["a"] in LEAK_ALERTS]
    cons_alerts = [m for m in gw.log if m["k"] == "ALERT" and m["a"] in CONSUMPTION_ALERTS]
    return leak_alerts, cons_alerts


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
        days = float((t.iloc[-1] - t.iloc[0]).total_seconds() / 86400.0)
        df["t"] = t
        m1 = df.set_index("t")["Pulses"].resample("1min").sum().fillna(0.0) * res_gal * GAL_TO_L / 60.0

        ev = pd.read_csv(os.path.join(DATA, f"LabelledEvents_site_{sid:03d}.csv"))
        labels = ev["Label"].value_counts().to_dict() if "Label" in ev.columns else {}
        has_leak_label = any("leak" in str(k).lower() for k in labels)

        audit.append({
            "site_id": sid,
            "irrigation": irr,
            "days": round(days, 1),
            "dt_mode_s": float(dt.mode().iloc[0]),
            "gaps_gt_4s": int((dt > 4).sum()),
            "nan_pulses": nans,
            "has_leak_label": has_leak_label,
            "event_labels": {str(k): int(v) for k, v in labels.items()},
        })

        # Evaluate first 30 days (Day 1 = LEARN_S, Days 2..30 = 29 test days)
        n_min = min(len(m1), 30 * 1440)
        arr = m1.values[:n_min]
        eval_days = (n_min - 1440) / 1440.0
        leaks, cons = run_flow_series(arr)
        by_type = {}
        for m in leaks:
            by_type[m["a"]] = by_type.get(m["a"], 0) + 1
        fp_eval.append({
            "site_id": sid,
            "irrigation": irr,
            "eval_days": round(eval_days, 1),
            "leak_alerts": len(leaks),
            "leak_fp_per_day": round(len(leaks) / max(eval_days, 1e-9), 4),
            "by_type": by_type,
            "consumption_alerts": len(cons),
        })

        # Controlled leak injection on indoor-focused / non-anomalous sites (6, 10, 20)
        if sid in (6, 10, 20):
            # 14-day slice (Day 1 learn, inject at Day 3 10:00 = min 2880 + 600 = 3480)
            sl = m1.values[: min(len(m1), 14 * 1440)]
            inj_min = 2 * 1440 + 600
            for lps in (0.05, 0.5):
                la, _ = run_flow_series(sl, inject_lps=lps, inject_start_min=inj_min)
                post = [m for m in la if m["t"] >= inj_min * 60]
                pre = [m for m in la if m["t"] < inj_min * 60]
                first = post[0] if post else None
                inj_eval.append({
                    "site_id": sid,
                    "inject_lps": lps,
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
    for r in fp_eval:
        print("FP:", r)
    for r in inj_eval:
        print("INJ:", r)


if __name__ == "__main__":
    main()
