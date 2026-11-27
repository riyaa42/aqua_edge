"""Targeted Robustness Checks for Supply-Event Detection (Day-1 Baseline & Inflow Suppression).

Tests:
  1. Day-1 baseline robustness:
     a) Day-1 contains a supply interruption (tank drops to 0.25 m).
     b) Day-1 has unusual tank cycling / shallow cycle (stays >= 1.1 m).
     c) Day-1 starts at an abnormally low initial tank level (0.35 m for 15 min).
  2. Inflow suppression robustness (inflow_lps >= 0.1):
     a) Genuine supply interruption with 0.00 L/s residual inflow.
     b) Partial/throttled supply interruption with 0.08 L/s residual inflow (< 0.1).
     c) Partial/throttled supply interruption with 0.15 L/s residual inflow (>= 0.1, below demand).
     d) Leak-induced tank drainage with active pump inflow (2.0 L/s).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [os.path.join(ROOT, "firmware")]

from aqua_edge import TankNode, FlatNode, DAY  # noqa: E402
from gateway import Gateway  # noqa: E402
from packet import sim_key  # noqa: E402


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


def run_profile(hours, tank_fn):
    flats = ["B-401", "B-201", "B-G01"]
    clock, readings, inboxes = [0.0], [{}], {}
    hal = lambda n: SimHAL(n, clock, readings, inboxes)
    tank = TankNode("B-TANK", hal("B-TANK"))
    nodes = [FlatNode(f, hal(f)) for f in flats] + [tank]
    gw = Gateway(hal("GATEWAY"), flats, "B-TANK")

    for m in range(hours * 60):
        t = m * 60.0
        clock[0] = t
        level, inflow, outflow = tank_fn(m, t)
        # an outflow of 1 L/s or more in a profile is a pipe leak on top of normal 0.15 L/s use;
        # flats draw their share in 10 min of every 20 (ordinary use, never a "leak")
        demand = outflow if outflow < 1.0 else 0.15
        use = 2 * demand / len(flats) if m % 20 < 10 else 0.0
        outflow = use * len(flats) + (outflow - demand)
        r = {f: {"flow_lps": use, "pressure_m": 10.0 + level} for f in flats}
        r["B-TANK"] = {
            "level_m": level, "inflow_lps": inflow, "outflow_lps": outflow,
            "ph": 7.2, "tds_ppm": 250.0, "turbidity_ntu": 0.5, "temp_c": 25.0,
        }
        readings[0] = r
        for n in nodes:
            n.step()
        gw.step()

    assert not [m for m in gw.log if m["k"] == "ALERT" and m["a"].startswith("LEAK_")], "false flat leak"
    evs = [(round(e["t"] / 3600, 2), e["event"]) for e in gw.events if e["event"] in ("SUPPLY_EVENT", "SUPPLY_UNCERTAIN", "LEAK_TANK_DRAIN", "PIPE_LEAK")]
    return tank.min_level, evs


def main():
    # 1a. Day-1 interruption (tank dips to 0.25 m on Day 1, then genuine cut to 0.35 m on Day 2)
    def p1a(m, t):
        if 12 * 3600 <= t < 14 * 3600:
            return 0.25, 0.0, 0.15
        if 30 * 3600 <= t < 34 * 3600:
            return 0.35, 0.0, 0.15
        return 1.2 + 0.6 * ((m % 480) < 240), 0.15, 0.15

    # 1b. Day-1 shallow cycling (1.1..1.9 m on Day 1, normal cycle down to 0.55 m on Day 2)
    def p1b(m, t):
        if t < DAY:
            return 1.1 + 0.8 * ((m % 480) < 240), 0.15, 0.15
        return 0.55 + 1.3 * ((m % 480) < 240), 0.15, 0.15

    # 1c. Day-1 abnormally low initial tank level (0.35 m for first 15 min, then normal)
    def p1c(m, t):
        if t < 900:
            return 0.35, 2.0, 0.15
        if 30 * 3600 <= t < 34 * 3600:
            return 0.38, 0.0, 0.15
        return 0.6 + 1.2 * ((m % 480) < 240), 0.15, 0.15

    print("=== 1. Day-1 Baseline Robustness ===")
    for name, fn in (("1a_day1_interruption", p1a), ("1b_day1_shallow_cycle", p1b), ("1c_day1_low_init", p1c)):
        ml, evs = run_profile(40, fn)
        print(f"  {name}: learned_min_level={ml:.3f} m | events={evs}")

    print("\n=== 2. Inflow Suppression Robustness (inflow_lps >= 0.1) ===")
    for label, res_in, out_lps in (
        ("2a_zero_inflow_cut", 0.00, 0.15),
        ("2b_residual_0.08_cut", 0.08, 0.15),
        ("2c_residual_0.15_cut_vs_0.30_out", 0.15, 0.30),
        ("2d_leak_drain_active_pump_2.0", 2.00, 1.80),
    ):
        def p2(m, t, ri=res_in, ol=out_lps):
            if t >= 30 * 3600:
                return 0.35, ri, ol
            return 1.2 + 0.6 * ((m % 480) < 240), 0.15, 0.15
        ml, evs = run_profile(36, p2)
        print(f"  {label}: learned_min_level={ml:.3f} m | events={evs}")


if __name__ == "__main__":
    main()
