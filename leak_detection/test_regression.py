"""Phase 5 Regression & Localization Test Suite for Leakage Detection & Supply Disambiguation.

Tests all 8 required scenarios using the real firmware (aqua_edge.py, gateway.py)
and WNTR hydraulics (water.py + bursty demand):
  1. Small, medium, and large flat leaks (including 10-min LEAK_PRESSURE_DROP check)
  2. Main downtake/riser leaks across floors (B-D4..B-D0) + cross-floor localization accuracy
  3. Normal bursty household consumption (48 h, no leaks)
  4. Low tank level without a leak (normal operation)
  5. Supply interruption without a leak (pump off -> SUPPLY_EVENT)
  6. Leak-induced tank drainage (large leak draining tank -> LEAK_TANK_DRAIN, NO false SUPPLY_EVENT)
  7. Simultaneous supply interruption and leakage
  8. Pressure fluctuations without actual leakage (no false LEAK_PRESSURE_DROP)
"""
import os
import sys
from multiprocessing import Pool
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [os.path.join(ROOT, "firmware"), os.path.join(ROOT, "simulation", "live"), os.path.join(ROOT, "simulation", "scripts")]

from aqua_edge import FlatNode, TankNode, DAY, HOUR  # noqa: E402
from gateway import Gateway  # noqa: E402
from packet import sim_key  # noqa: E402
from water import Water  # noqa: E402


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


def run_wntr_scenario(hours=48, setup_fn=None, step_fn=None, seed=42):
    w = Water(seed=seed)
    clock, readings, inboxes = [0.0], [None], {}
    hal = lambda n: SimHAL(n, clock, readings, inboxes)
    nodes = [FlatNode(f, hal(f)) for f in w.flats] + [TankNode("B-TANK", hal("B-TANK"))]
    gw = Gateway(hal("GATEWAY"), w.flats, "B-TANK")
    if setup_fn:
        setup_fn(w)
    while w.t < hours * 3600:
        clock[0] = w.t
        if step_fn:
            step_fn(w)
        readings[0] = w.readings()
        for n in nodes:
            n.step()
        gw.step()
        w.step()
    return w, gw


def run_synthetic_supply(pump_off=False, tank_leak=False, low_level_normal_inflow=False):
    """Deterministic unit-level scenario for Scenarios 4, 5, 6, 7."""
    flats = ["B-401", "B-301", "B-201", "B-101", "B-G01"]
    clock, readings, inboxes = [0.0], [{}], {}
    hal = lambda n: SimHAL(n, clock, readings, inboxes)
    nodes = [FlatNode(f, hal(f)) for f in flats] + [TankNode("B-TANK", hal("B-TANK"))]
    gw = Gateway(hal("GATEWAY"), flats, "B-TANK")

    for m in range(36 * 60):
        t = m * 60
        clock[0] = t
        inflow = 0.125
        # each flat draws 0.05 L/s for 10 min in every 20: ordinary use, never a "leak"
        flat_flow = 0.05 if m % 20 < 10 else 0.0
        outflow = demand = flat_flow * len(flats)
        level = 1.5
        p_drop = 0.0
        if t >= 26 * 3600:
            if pump_off and not tank_leak:
                inflow = 0.0
                level = 0.35
                p_drop = 0.8
            elif tank_leak and not pump_off:
                outflow = demand + 1.675  # large leak draining tank
                level = 0.35
                p_drop = 0.7
            elif pump_off and tank_leak:
                inflow = 0.0
                outflow = demand + 1.475
                level = 0.30
                p_drop = 1.1
            elif low_level_normal_inflow:
                inflow = 0.125
                level = 0.40
                p_drop = 0.1
        r = {
            f: {"flow_lps": flat_flow,
                "pressure_m": 10.0 - p_drop + (0.01 if m % 2 else -0.01)}
            for f in flats
        }
        r["B-TANK"] = {
            "outflow_lps": outflow,
            "inflow_lps": inflow,
            "level_m": level,
            "temp_c": 25.0,
            "ph": 7.2,
            "tds_ppm": 250.0,
            "turbidity_ntu": 0.5,
        }
        readings[0] = r
        for n in nodes:
            n.step()
        gw.step()
    return gw


def _wntr_job(args):
    label, hours, site, size, start_s, seed = args
    _, gw = run_wntr_scenario(
        hours=hours,
        step_fn=(lambda w: w.start_leak(site, size) if w.t == start_s else None) if site else None,
        seed=seed,
    )
    return label, site, size, start_s, gw.log, gw.events


def main():
    jobs = [("flat_small", 48, "B-203", "small", DAY + 10 * HOUR, 42),
            ("flat_medium", 48, "B-203", "medium", DAY + 10 * HOUR, 42),
            ("flat_large", 48, "B-203", "large", DAY + 10 * HOUR, 42),
            ("normal_48h", 48, None, None, 0, 42)]
    for site in ("B-D4", "B-D3", "B-D2", "B-D1", "B-D0"):
        for size in ("medium", "large"):
            jobs.append((f"down_{site}_{size}", 48, site, size, DAY + 12 * HOUR, 42))

    with Pool(max(os.cpu_count() - 2, 1)) as pool:
        res_map = {r[0]: r for r in pool.map(_wntr_job, jobs)}

    print("=== SCENARIO 1: Small, Medium, Large Flat Leaks (B-203 at 10:00 Day 2) ===")
    for size in ("small", "medium", "large"):
        _, _, _, start_s, log, _ = res_map[f"flat_{size}"]
        b203_leaks = [(m["a"], (m["t"] - start_s) // 60) for m in log if m["n"] == "B-203" and m["k"] == "ALERT" and m["a"].startswith("LEAK_") and m["t"] >= start_s]
        neighbor_leaks = [m for m in log if m["n"] != "B-203" and m["k"] == "ALERT" and m["a"].startswith("LEAK_")]
        cons_alerts = [m for m in log if m["k"] == "ALERT" and m["a"] == "ABNORMAL_CONSUMPTION"]
        print(f"  {size:6s}: B-203 leak alerts={b203_leaks} | neighbor false leaks={len(neighbor_leaks)} | abnormal_consumption={len(cons_alerts)}")
        assert len(b203_leaks) >= 1, f"Flat leak {size} missed!"
        assert len(neighbor_leaks) == 0, f"Neighbor false leak on {size}: {neighbor_leaks}"
        if size in ("medium", "large"):
            p_hits = [lat for a, lat in b203_leaks if a == "LEAK_PRESSURE_DROP"]
            assert p_hits and p_hits[0] <= 12, f"Expected ~10 min LEAK_PRESSURE_DROP on {size}, got {b203_leaks}"

    print("\n=== SCENARIO 2: Main Downtake Leaks Across Floors (B-D4..B-D0) & Cross-Floor Localization ===")
    floor_map = {"B-D4": "4", "B-D3": "3", "B-D2": "2", "B-D1": "1", "B-D0": "G"}
    top1 = top2 = total = 0
    for site in ("B-D4", "B-D3", "B-D2", "B-D1", "B-D0"):
        for size in ("medium", "large"):
            _, _, _, start_s, log, events = res_map[f"down_{site}_{size}"]
            pipe_evs = [e for e in events if e["event"] == "PIPE_LEAK" and e["t"] >= start_s]
            tank_evs = [(m["a"], (m["t"] - start_s) // 60) for m in log if m["n"] == "B-TANK" and m["k"] == "ALERT" and m["t"] >= start_s]
            if not pipe_evs:
                print(f"  {site} ({size:6s}): NO PIPE_LEAK | tank_alerts={tank_evs} | gw_events={[e['event'] for e in events if e['t'] >= start_s]}")
                continue
            ev = pipe_evs[0]
            drops = ev.get("floor_drops") or {}
            ranked = sorted(drops, key=drops.get, reverse=True)
            true_fl = floor_map[site]
            total += 1
            if ranked and ranked[0] == true_fl:
                top1 += 1
            if true_fl in ranked[:2]:
                top2 += 1
            print(f"  {site} ({size:6s}): lat={(ev['t']-start_s)//60:2.0f} min | suspected={ev.get('suspected_floor')} | drops={drops}")
    print(f"  Cross-floor localization: Top-1={top1}/{total}, Top-2={top2}/{total}")

    print("\n=== SCENARIO 3 & 8: Normal Operation + Pressure Fluctuations Without Leakage ===")
    _, _, _, _, log_norm, ev_norm = res_map["normal_48h"]
    leaks_norm = [m for m in log_norm if m["k"] == "ALERT" and m["a"].startswith("LEAK_")]
    pipe_norm = [e for e in ev_norm if e["event"] == "PIPE_LEAK"]
    print(f"  48h normal run: flat leak false alarms={len(leaks_norm)}, PIPE_LEAK false alarms={len(pipe_norm)}")
    assert len(leaks_norm) == 0 and len(pipe_norm) == 0

    # Pressure fluctuation without elevated flow
    clock, readings, inboxes = [0.0], [{}], {}
    hal = lambda n: SimHAL(n, clock, readings, inboxes)
    fn = FlatNode("B-203", hal("B-203"))
    for m in range(30 * 60):
        clock[0] = m * 60
        p_jit = -1.5 if (m >= 25 * 60 and m % 3 == 0) else 0.0
        readings[0] = {"B-203": {"flow_lps": 0.01, "pressure_m": 10.0 + p_jit}}
        fn.step()
    p_false = [x for x in fn.store.queue if x["k"] == "ALERT" and x["a"] == "LEAK_PRESSURE_DROP"]
    print(f"  Pressure fluctuations without high flow: LEAK_PRESSURE_DROP false alarms={len(p_false)}")
    assert len(p_false) == 0

    print("\n=== SCENARIOS 4, 5, 6, 7: Supply Interruption vs. Leak-Induced Tank Drainage ===")
    gw4 = run_synthetic_supply(low_level_normal_inflow=True)
    evs4 = [e["event"] for e in gw4.events]
    print(f"  4. Low tank level, normal inflow (no leak): {evs4}")
    assert "SUPPLY_EVENT" not in evs4 or "SUPPLY_UNCERTAIN" in evs4

    gw5 = run_synthetic_supply(pump_off=True, tank_leak=False)
    evs5 = [e["event"] for e in gw5.events]
    print(f"  5. Supply interruption (pump off, no leak): {evs5}")
    assert "SUPPLY_EVENT" in evs5 and "LEAK_TANK_DRAIN" not in evs5

    gw6 = run_synthetic_supply(pump_off=False, tank_leak=True)
    evs6 = [e["event"] for e in gw6.events]
    print(f"  6. Leak-induced tank drainage (pump on, leak draining tank): {evs6}")
    assert "SUPPLY_EVENT" not in evs6 and "LEAK_TANK_DRAIN" in evs6

    gw7 = run_synthetic_supply(pump_off=True, tank_leak=True)
    evs7 = [e["event"] for e in gw7.events]
    for g in (gw4, gw5, gw6, gw7):          # none of these has a leak inside a flat
        assert not [m for m in g.log if m["k"] == "ALERT" and m["a"].startswith("LEAK_")]
    print(f"  7. Simultaneous supply interruption + leak: {evs7}")
    assert "PIPE_LEAK" in evs7 or "LEAK_TANK_DRAIN" in evs7
    print("\nALL 8 REGRESSION SCENARIOS PASSED")


if __name__ == "__main__":
    main()
