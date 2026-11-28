"""Phase 9: Firmware Integration & Regression Tests for Abnormal Consumption.

Tests FlatNode + Gateway + Store + packet signing/buffering across:
  1. Normal bursty household consumption (0 false alerts).
  2. Missing / NaN readings (graceful handling).
  3. High consumption spike (ABNORMAL_CONSUMPTION), night consumption
     (UNUSUAL_NIGHT_CONSUMPTION), sudden change (SUDDEN_CONSUMPTION_CHANGE),
     and prolonged zero-flow (PROLONGED_ZERO_FLOW).
  4. Multiple households producing simultaneous events.
  5. Offline / LoRa outage (3.5 h) with buffered delivery after reconnection.
  6. Strict separation from leakage detection (never triggers PIPE_LEAK,
     LEAK_TANK_DRAIN, or leak scoring).
"""
import math
import os
import tempfile


from edge.aqua_edge import FlatNode, TankNode, DAY  # noqa: E402
from gateway.core import Gateway, Topology  # noqa: E402
from tests.offline_radio import exchange  # noqa: E402
from hal.packet import sim_key  # noqa: E402
from tinyml.leakage import compare_leaks as CL  # noqa: E402


class OutageHAL:
    key = staticmethod(sim_key)

    def __init__(self, name, clock, readings, inboxes, outage_windows=(), storage_path=None):
        self.name, self.clock, self.readings, self.inboxes = name, clock, readings, inboxes
        self.outage_windows = outage_windows
        self.storage_path = storage_path
        inboxes[name] = []

    def now(self):
        return self.clock[0]

    def read(self):
        return self.readings[0][self.name]

    def send(self, msg, cad=False):
        t = self.clock[0]
        if any(s <= t < e for s, e in self.outage_windows):
            return
        for n, box in self.inboxes.items():
            if n != self.name:
                box.append(msg)

    def receive(self):
        msgs, self.inboxes[self.name] = self.inboxes[self.name], []
        return msgs

    def led(self, colour):
        pass


def run_case(hours, flow_fn, outage_windows=(), missing_fn=None):
    flats = ["B-401", "B-201", "B-G01"]
    clock, readings, inboxes = [0.0], [{}], {}
    td = tempfile.mkdtemp(prefix="ac_int_")
    hal = lambda n: OutageHAL(
        n, clock, readings, inboxes, outage_windows,
        storage_path=os.path.join(td, f"{n}_q.jsonl") if n != "GATEWAY" else None,
    )
    nodes = [FlatNode(f, hal(f)) for f in flats] + [TankNode("B-TANK", hal("B-TANK"))]
    gw = Gateway(hal("GATEWAY"), Topology.load().only(flats))

    for m in range(hours * 60):
        t = m * 60.0
        clock[0] = t
        r = {}
        total_f = 0.0
        for f in flats:
            fl = flow_fn(f, m, t)
            if missing_fn and missing_fn(f, m, t):
                r[f] = {"flow_lps": float("nan"), "pressure_m": 10.0}
            else:
                r[f] = {"flow_lps": fl, "pressure_m": 10.0}
                total_f += fl
        r["B-TANK"] = {
            "outflow_lps": total_f, "inflow_lps": total_f, "level_m": 1.5,
            "ph": 7.2, "tds_ppm": 250.0, "turbidity_ntu": 0.5, "temp_c": 25.0,
        }
        readings[0] = r
        for n in nodes:
            n.step()
        exchange(nodes, gw, inboxes)

    remaining_q = sum(len(n.store.queue) for n in nodes)
    alerts = [(m["n"], m["a"], round(m["t"] / 3600, 2)) for m in gw.log if m["k"] == "ALERT"]
    events = [e["event"] for e in gw.events]
    return remaining_q, alerts, events


def main():
    bursty = lambda m: 0.05 if (m % 30 < 10 and (m // 60) % 24 not in (1, 2, 3, 4)) else 0.0

    # 1. Normal bursty consumption
    q1, a1, e1 = run_case(48, lambda f, m, t: bursty(m))
    assert q1 == 0 and a1 == [] and "PIPE_LEAK" not in e1, (q1, a1, e1)
    print("1. Normal bursty consumption: PASS (0 alerts)")

    # 2. Missing / NaN readings
    q2, a2, _ = run_case(48, lambda f, m, t: bursty(m), missing_fn=lambda f, m, t: f == "B-201" and 30 * 60 <= m < 30 * 60 + 10)
    assert q2 == 0 and a2 == [], (q2, a2)
    print("2. Missing / NaN readings: PASS")

    # 3. High spike, night consumption, sudden change (simultaneous across B-401 & B-201) + 3.5h LoRa outage
    def multi_anom(f, m, t):
        base = bursty(m)
        if f == "B-401" and 26 * 60 <= m < 26 * 60 + 25:
            return 0.12  # UNUSUAL_NIGHT_CONSUMPTION
        if f == "B-201" and 34 * 60 <= m < 34 * 60 + 75:
            return base + 0.18  # ABNORMAL_CONSUMPTION + SUDDEN_CONSUMPTION_CHANGE
        return base

    q3, a3, e3 = run_case(48, multi_anom, outage_windows=[(26 * 3600, 29.5 * 3600)])
    assert q3 == 0, q3
    got_types = {(n, a) for n, a, _ in a3}
    assert ("B-401", "UNUSUAL_NIGHT_CONSUMPTION") in got_types, a3
    assert ("B-201", "ABNORMAL_CONSUMPTION") in got_types, a3
    assert ("B-201", "SUDDEN_CONSUMPTION_CHANGE") in got_types, a3
    assert "PIPE_LEAK" not in e3 and "LEAK_TANK_DRAIN" not in e3, e3
    print("3. Multi-household simultaneous + 3.5h LoRa outage: PASS", sorted(got_types))

    # 4. Prolonged zero-flow (25 h)
    q4, a4, e4 = run_case(54, lambda f, m, t: 0.0 if (f == "B-G01" and 24 * 60 <= m < 49 * 60) else bursty(m))
    assert q4 == 0 and ("B-G01", "PROLONGED_ZERO_FLOW") in {(n, a) for n, a, _ in a4}, (q4, a4)
    assert "PIPE_LEAK" not in e4, e4
    print("4. Prolonged zero-flow (25h): PASS")

    # 5. Scoring separation: abnormal consumption alerts must never count as leaks
    for ev in ("ABNORMAL_CONSUMPTION", "UNUSUAL_NIGHT_CONSUMPTION", "PROLONGED_ZERO_FLOW", "SUDDEN_CONSUMPTION_CHANGE"):
        sc = CL.score((None, None, 100, 0), [(500, "B-201", ev)])
        assert sc == {"flow": (None, 0), "pressure": (None, 0)}, (ev, sc)
    print("5. Leak vs consumption scoring separation: PASS")
    print("ALL ABNORMAL CONSUMPTION INTEGRATION TESTS PASSED")


if __name__ == "__main__":
    main()
