"""Comprehensive Leakage-Detection Validation Campaign (Evaluation Only).

Runs independent WNTR simulations against the firmware
WITHOUT modifying any detection thresholds or algorithms:
  1. Flat-level leakage detection across multiple flats, floors, severities, and
     independent bursty household-demand seeds (with causal timestamp checks and
     small-leak flow/pressure diagnostics).
  2. Main downtake/riser leakage across all 5 floors (B-D4..B-D0), 0.5 & 1.5 L/s,
     multiple demand seeds, and cross-floor localization accuracy.
  3. Supply-event discrimination in full WNTR simulations (6 scenarios).
  4. 7-day normal-operation robustness (baseline noise + elevated pressure noise/drift).

Uses single-pass WNTRSimulator(wn).run_sim() with scheduled WNTR Controls (same
mechanism as make_scenario.py) and streams 1-minute readings causally to the
unmodified firmware (FlatNode, TankNode, Gateway).

Outputs JSON summary to leak_detection/results/validation_campaign.json.
"""
import json
import os
import sys
import tempfile
from multiprocessing import Pool
import numpy as np
import wntr
from wntr.network.controls import Rule, Control, AndCondition, SimTimeCondition, ValueCondition, ControlAction

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [
    os.path.join(ROOT, "firmware", "edge_node"),
    os.path.join(ROOT, "firmware", "gateway"),
    os.path.join(ROOT, "firmware", "communication"),
    os.path.join(ROOT, "simulation", "live"),
    os.path.join(ROOT, "simulation", "webots"),
    os.path.join(ROOT, "simulation", "scripts"),
]

from aqua_edge import FlatNode, TankNode, DAY, HOUR  # noqa: E402
from gateway import Gateway  # noqa: E402
from packet import sim_key  # noqa: E402
from water import LEAK_SIZES_LPS  # noqa: E402
import make_scenario as M  # noqa: E402
import build_society as S  # noqa: E402

LEAK_ALERTS = {"LEAK_NIGHT_FLOW", "LEAK_CONTINUOUS_FLOW", "LEAK_PRESSURE_DROP"}
CONSUMPTION_ALERTS = {"ABNORMAL_CONSUMPTION"}
SUPPLY_EVENTS = {"SUPPLY_EVENT", "LEAK_TANK_DRAIN", "SUPPLY_UNCERTAIN"}


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


def build_wn(demand_seed=42, hours=54, wn_modifier=None):
    days = max(2, (hours + 23) // 24)
    S.BUILDINGS = ["B"]
    wn = S.build(leak=False, bursty=True, seed=demand_seed)
    if days > 2:
        rng = np.random.default_rng(demand_seed)
        for jname in wn.junction_name_list:
            if jname[-1].isdigit() and jname[-2] == "0":
                wn.get_pattern(f"{jname}_pat").multipliers = np.array(
                    S.make_bursty_pattern(rng, days=days, step_min=5)
                )
    wn.options.time.duration = hours * 3600 - 60
    wn.options.time.hydraulic_timestep = 60
    wn.options.time.report_timestep = 60
    tank, pump = wn.get_node("B-TANK"), wn.get_link("B-PUMP")
    wn.remove_control("B-ON")
    wn.remove_control("B-OFF")
    wn.add_control(
        "B-ON",
        Rule(ValueCondition(tank, "level", "<", S.PUMP_ON_BELOW_M), [ControlAction(pump, "status", 1)], priority=1, name="B-ON"),
    )
    wn.add_control(
        "B-OFF",
        Rule(ValueCondition(tank, "level", ">", S.PUMP_OFF_ABOVE_M), [ControlAction(pump, "status", 0)], priority=1, name="B-OFF"),
    )
    if wn_modifier:
        wn_modifier(wn)
    return wn


def schedule_leak(wn, site, size, start_s):
    lps = LEAK_SIZES_LPS[size]
    node = wn.get_node(site)
    x, y = node.coordinates
    wn.add_junction(f"{site}-LEAK", elevation=node.elevation, coordinates=(x + 3, y - 3))
    wn.add_pipe(f"{site}-LEAKPIPE", site, f"{site}-LEAK", length=1, diameter=0.05, roughness=130, initial_status="CLOSED")
    p_est = max(S.ROOF + 2.2 - node.elevation, 0.5)
    wn.get_node(f"{site}-LEAK").emitter_coefficient = lps / 1000.0 / np.sqrt(p_est)
    wn.add_control(
        "LEAK-START",
        Control._time_control(wn, start_s, "SIM_TIME", False, ControlAction(wn.get_link(f"{site}-LEAKPIPE"), "status", 1)),
    )


def run_causal_firmware(wn, sensor_seed=7, extra_p_noise=0.0, p_drift_amp=0.0, diag_site=None, diag_start=None):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        res = wntr.sim.EpanetSimulator(wn).run_sim(file_prefix=os.path.join(td, "sim"))
    rng = np.random.default_rng(sensor_seed)
    flats = [n for n in wn.junction_name_list if n[-1].isdigit() and n[-2] == "0"]
    meters = [f"{f}-P" for f in flats] + ["B-DT4", "B-RISE"]
    gain = {m: 1 + rng.uniform(-M.FLOW_GAIN_ERR, M.FLOW_GAIN_ERR) for m in meters}

    clock, readings, inboxes = [0.0], [None], {}
    hal = lambda n: SimHAL(n, clock, readings, inboxes)
    nodes = [FlatNode(f, hal(f)) for f in flats] + [TankNode("B-TANK", hal("B-TANK"))]
    gw = Gateway(hal("GATEWAY"), flats, "B-TANK")
    node_map = {n.id: n for n in nodes}

    times = res.node["pressure"].index.values
    p_df = res.node["pressure"][flats]
    f_arrs = {f: res.link["flowrate"][f"{f}-P"].values * 1000.0 for f in flats}
    dt4 = res.link["flowrate"]["B-DT4"].values * 1000.0
    rise = res.link["flowrate"]["B-RISE"].values * 1000.0
    lvl = res.node["pressure"]["B-TANK"].values

    diag = []
    for idx, t in enumerate(times):
        t_f = float(t)
        clock[0] = t_f
        drift = p_drift_amp * np.sin(2 * np.pi * t_f / (6 * 3600)) if p_drift_amp > 0 else 0.0
        r = {}
        for f in flats:
            fl = max(float(f_arrs[f][idx]) * gain[f"{f}-P"] + float(rng.normal(0, M.FLOW_NOISE_LPS)), 0.0)
            pr = float(p_df[f].iloc[idx]) + float(rng.normal(0, M.PRESSURE_NOISE_M)) + (float(rng.normal(0, extra_p_noise)) if extra_p_noise > 0 else 0.0) + drift
            r[f] = {"flow_lps": fl, "pressure_m": pr}
        tod = (t_f % M.DAY) / M.H
        r["B-TANK"] = {
            "outflow_lps": max(float(dt4[idx]) * gain["B-DT4"] + float(rng.normal(0, M.FLOW_NOISE_LPS)), 0.0),
            "inflow_lps": max(float(rise[idx]) * gain["B-RISE"] + float(rng.normal(0, M.FLOW_NOISE_LPS)), 0.0),
            "level_m": float(lvl[idx]) + float(rng.normal(0, M.LEVEL_NOISE_M)),
            "ph": 7.2 + float(rng.normal(0, 0.05)),
            "tds_ppm": 250.0 + float(rng.normal(0, 5.0)),
            "turbidity_ntu": 0.5 + float(rng.normal(0, 0.1)),
            "temp_c": 26.0 + 2.0 * np.sin((tod - 9) / 24 * 2 * np.pi) + float(rng.normal(0, 0.1)),
        }
        if diag_site and diag_start is not None and diag_start <= t_f < diag_start + 30 * 60:
            diag.append((t_f - diag_start, r[diag_site]["flow_lps"], r[diag_site]["pressure_m"]))
        readings[0] = r
        for n in nodes:
            n.step()
            assert n.hal.now() == t_f
        gw.step()
    return flats, gw, node_map, diag


def pressure_labels(gw, flat=None, since=0):
    """-> (flat pressure alerts the gateway labelled possible_cause=PIPE_LEAK, unlabelled ones)."""
    ev = [e for e in gw.events
          if e.get("alert") == "LEAK_PRESSURE_DROP" and e["t"] >= since and flat in (None, e["node"])]
    n = sum(e.get("possible_cause") == "PIPE_LEAK" for e in ev)
    return n, len(ev) - n


def _flat_job(args):
    flat, size, demand_seed = args
    start_s = 34 * 3600
    wn = build_wn(demand_seed=demand_seed, hours=54, wn_modifier=lambda w: schedule_leak(w, flat, size, start_s))
    _, gw, node_map, diag = run_causal_firmware(wn, sensor_seed=1000 + demand_seed, diag_site=flat, diag_start=start_s)
    target_alerts = [
        (m["t"], m["a"], round((m["t"] - start_s) / 60, 1))
        for m in gw.log
        if m["k"] == "ALERT" and m["n"] == flat and m["a"] in LEAK_ALERTS and m["t"] >= start_s
    ]
    pre_alerts = [m for m in gw.log if m["k"] == "ALERT" and m["a"] in LEAK_ALERTS and m["t"] < start_s]
    neighbor_leaks = [
        (m["t"], m["n"], m["a"])
        for m in gw.log
        if m["k"] == "ALERT" and m["n"] != flat and m["a"] in LEAK_ALERTS and m["t"] >= start_s
    ]
    cons_alerts = [m for m in gw.log if m["k"] == "ALERT" and m["a"] in CONSUMPTION_ALERTS]
    fnode = node_map[flat]
    p_base_10 = fnode.base["pressure_m"].mean(10)
    p_std_10 = fnode.base["pressure_m"].std(10)
    f_base_10 = fnode.base["flow_lps"].mean(10)
    f_std_10 = fnode.base["flow_lps"].std(10)
    first = target_alerts[0] if target_alerts else None
    return {
        "flat": flat,
        "size": size,
        "lps": LEAK_SIZES_LPS[size],
        "demand_seed": demand_seed,
        "detected": first is not None,
        "first_mechanism": first[1] if first else None,
        "latency_min": first[2] if first else None,
        "all_target_alerts": [(a, lat) for _, a, lat in target_alerts],
        "pre_leak_false_alerts": len(pre_alerts),
        "real_pressure_alerts_labelled": pressure_labels(gw, flat, start_s)[0],
        "neighbor_false_leaks": len(neighbor_leaks),
        "target_consumption_alerts": sum(1 for m in cons_alerts if m["n"] == flat and m["t"] >= start_s),
        "neighbor_consumption_alerts": sum(1 for m in cons_alerts if not (m["n"] == flat and m["t"] >= start_s)),
        "mean_leak_window_flow": round(float(np.mean([d[1] for d in diag])), 4) if diag else None,
        "mean_p_drop_vs_h10": round(float(p_base_10 - np.mean([d[2] for d in diag])), 4) if diag else None,
        "h10_p_std": round(float(p_std_10), 4),
        "h10_f_mean": round(float(f_base_10), 4),
        "h10_f_std": round(float(f_std_10), 4),
    }


def _downtake_job(args):
    site, size, demand_seed = args
    start_s = 34 * 3600
    expected_floor = site.split("-")[1][1]
    if expected_floor == "0":
        expected_floor = "G"
    wn = build_wn(demand_seed=demand_seed, hours=48, wn_modifier=lambda w: schedule_leak(w, site, size, start_s))
    _, gw, _, _ = run_causal_firmware(wn, sensor_seed=2000 + demand_seed)
    pipe_leaks = [e for e in gw.events if e["event"] == "PIPE_LEAK"]
    pre_pipe = [e for e in pipe_leaks if e["t"] < start_s]
    post_pipe = [e for e in pipe_leaks if e["t"] >= start_s]
    flat_false_leaks = [m for m in gw.log if m["k"] == "ALERT" and m["a"] in LEAK_ALERTS]
    first = post_pipe[0] if post_pipe else None
    suspected = first.get("suspected_floor") if first else None
    drops = first.get("floor_drops") if first else None
    conf = first.get("localization_confidence") if first else None
    top2 = first.get("suspected_range") or ([k for k, _ in sorted(drops.items(), key=lambda kv: -kv[1])[:2]] if drops else [])
    return {
        "site": site,
        "expected_floor": expected_floor,
        "size": size,
        "lps": LEAK_SIZES_LPS[size],
        "demand_seed": demand_seed,
        "detected": first is not None,
        "latency_min": round((first["t"] - start_s) / 60, 1) if first else None,
        "pre_pipe_false": len(pre_pipe),
        "suspected_floor": suspected,
        "suspected_range": top2,
        "localization_confidence": conf,
        "top1_hit": suspected == expected_floor if first else False,
        "top2_hit": expected_floor in top2 if first else False,
        "drops": drops,
        "flat_false_leaks": len(flat_false_leaks),
        "flat_pressure_labelled": pressure_labels(gw)[0],
        "flat_pressure_unlabelled": pressure_labels(gw)[1],
    }


def _supply_job(name):
    if name == "low_tank_no_interruption_no_leak":
        def mod(wn):
            wn.remove_control("B-ON")
            wn.remove_control("B-OFF")
            pump = wn.get_link("B-PUMP")
            # Drain tank to ~0.4 m during 24h-26.5h, then run pump at matched low speed so inflow > 0 while level stays low
            wn.add_control("SCEN-OFF", Rule(AndCondition(SimTimeCondition(wn, ">=", 24 * 3600), SimTimeCondition(wn, "<", 26.5 * 3600)), [ControlAction(pump, "status", 0)], priority=5, name="SCEN-OFF"))
            wn.add_control("SCEN-ON", Rule(SimTimeCondition(wn, ">=", 26.5 * 3600), [ControlAction(pump, "status", 1)], priority=4, name="SCEN-ON"))
            wn.add_control("SCEN-THROTTLE", Rule(SimTimeCondition(wn, ">=", 26.5 * 3600), [ControlAction(pump, "base_speed", 0.45)], priority=4, name="SCEN-THROTTLE"))
        wn = build_wn(demand_seed=42, hours=36, wn_modifier=mod)
        _, gw, _, _ = run_causal_firmware(wn, sensor_seed=3001)
    elif name == "pump_failure_no_leak":
        def mod(wn):
            pump = wn.get_link("B-PUMP")
            wn.add_control("PUMP-FAIL", Rule(AndCondition(SimTimeCondition(wn, ">=", 24 * 3600), SimTimeCondition(wn, "<", 45 * 3600)), [ControlAction(pump, "status", 0)], priority=5, name="PUMP-FAIL"))
        wn = build_wn(demand_seed=42, hours=46, wn_modifier=mod)
        _, gw, _, _ = run_causal_firmware(wn, sensor_seed=3002)
    elif name == "municipal_supply_interruption":
        def mod(wn):
            wn.add_control("MUNI-CUT", Rule(AndCondition(SimTimeCondition(wn, ">=", 24 * 3600), SimTimeCondition(wn, "<", 45 * 3600)), [ControlAction(wn.get_link("B-MAIN"), "status", 0)], priority=5, name="MUNI-CUT"))
            wn.add_control("MUNI-PUMP", Rule(AndCondition(SimTimeCondition(wn, ">=", 24 * 3600), SimTimeCondition(wn, "<", 45 * 3600)), [ControlAction(wn.get_link("B-PUMP"), "status", 0)], priority=5, name="MUNI-PUMP"))
        wn = build_wn(demand_seed=42, hours=46, wn_modifier=mod)
        _, gw, _, _ = run_causal_firmware(wn, sensor_seed=3003)
    elif name == "leak_abnormal_tank_drain":
        def mod(wn):
            tank, pump = wn.get_node("B-TANK"), wn.get_link("B-PUMP")
            wn.remove_control("B-ON")
            wn.add_control("B-ON", Rule(ValueCondition(tank, "level", "<", 0.25), [ControlAction(pump, "status", 1)], priority=1, name="B-ON"))
            schedule_leak(wn, "B-D1", "large", 26 * 3600)
        wn = build_wn(demand_seed=42, hours=36, wn_modifier=mod)
        _, gw, _, _ = run_causal_firmware(wn, sensor_seed=3004)
    elif name == "simultaneous_supply_and_leak":
        def mod(wn):
            pump = wn.get_link("B-PUMP")
            wn.add_control("SIMUL-CUT", Rule(AndCondition(SimTimeCondition(wn, ">=", 26 * 3600), SimTimeCondition(wn, "<", 36 * 3600)), [ControlAction(pump, "status", 0)], priority=5, name="SIMUL-CUT"))
            schedule_leak(wn, "B-D1", "large", 26 * 3600)
        wn = build_wn(demand_seed=42, hours=36, wn_modifier=mod)
        _, gw, _, _ = run_causal_firmware(wn, sensor_seed=3005)
    else:
        wn = build_wn(demand_seed=77, hours=48)
        _, gw, _, _ = run_causal_firmware(wn, sensor_seed=3006)

    ev_list = [(round(e["t"] / 3600, 2), e["event"]) for e in gw.events if e["event"] in ("PIPE_LEAK", "SUPPLY_EVENT", "LEAK_TANK_DRAIN", "SUPPLY_UNCERTAIN")]
    return {"scenario": name, "events": ev_list}


def _normal7_job(args):
    label, demand_seed, extra_p_noise, p_drift_amp = args
    wn = build_wn(demand_seed=demand_seed, hours=7 * 24)
    flats, gw, _, _ = run_causal_firmware(wn, sensor_seed=4000 + demand_seed, extra_p_noise=extra_p_noise, p_drift_amp=p_drift_amp)
    flat_leaks = [m for m in gw.log if m["k"] == "ALERT" and m["a"] in LEAK_ALERTS]
    pressure_leaks = [m for m in gw.log if m["k"] == "ALERT" and m["a"] == "LEAK_PRESSURE_DROP"]
    cons_alerts = [m for m in gw.log if m["k"] == "ALERT" and m["a"] in CONSUMPTION_ALERTS]
    pipe_leaks = [e for e in gw.events if e["event"] == "PIPE_LEAK"]
    supply_evs = [e for e in gw.events if e["event"] in SUPPLY_EVENTS]
    return {
        "label": label,
        "demand_seed": demand_seed,
        "days": 7,
        "flats": len(flats),
        "flat_leak_alerts": len(flat_leaks),
        "pressure_leak_alerts": len(pressure_leaks),
        "pipe_leak_alerts": len(pipe_leaks),
        "supply_event_alerts": len(supply_evs),
        "consumption_alerts": len(cons_alerts),
        "details_flat_leaks": [(m["t"], m["n"], m["a"]) for m in flat_leaks[:10]],
        "details_supply": [(e["t"], e["event"]) for e in supply_evs[:10]],
    }


def main():
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    workers = max(os.cpu_count() - 2, 1)

    flat_jobs = [
        (f, z, s)
        for f in ("B-401", "B-203", "B-G01")
        for z in ("small", "medium", "large")
        for s in (101, 202, 303)
    ]
    downtake_jobs = [
        (site, z, s)
        for site in ("B-D4", "B-D3", "B-D2", "B-D1", "B-D0")
        for z in ("medium", "large")
        for s in (101, 202)
    ]
    supply_jobs = [
        "low_tank_no_interruption_no_leak",
        "pump_failure_no_leak",
        "municipal_supply_interruption",
        "leak_abnormal_tank_drain",
        "simultaneous_supply_and_leak",
        "normal_variable_demand",
    ]
    normal7_jobs = [
        ("7d_baseline_noise", 501, 0.0, 0.0),
        ("7d_elevated_noise_drift", 602, 0.10, 0.25),
    ]

    flat_res = [_flat_job(j) for j in flat_jobs]
    print(f"Completed flat_jobs: {len(flat_res)}", flush=True)
    dt_res = [_downtake_job(j) for j in downtake_jobs]
    print(f"Completed downtake_jobs: {len(dt_res)}", flush=True)
    sup_res = [_supply_job(j) for j in supply_jobs]
    print(f"Completed supply_jobs: {len(sup_res)}", flush=True)
    norm_res = [_normal7_job(j) for j in normal7_jobs]
    print(f"Completed normal7_jobs: {len(norm_res)}", flush=True)

    out = {
        "flat_leaks": flat_res,
        "downtake_leaks": dt_res,
        "supply_wntr": sup_res,
        "normal_7day": norm_res,
    }
    out_path = os.path.join(HERE, "results", "validation_campaign.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Saved validation results to {out_path}")


if __name__ == "__main__":
    main()
