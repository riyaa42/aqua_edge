"""Phase 6 — Combined Offline, LoRa Outage & Supply-Event Validation.

Tests all 8 required combined scenarios using the real Store, FlatNode, TankNode,
Gateway, binary signed packets (packet.py), and a simulated LoRa link with outages:
  1. Normal operation with LoRa connectivity.
  2. Internet unavailable, LoRa operational (offline edge + gateway detection).
  3. Temporary LoRa interruption (30 min).
  4. Long LoRa outage (3.5 hours) with buffered events.
  5. Supply interruption during normal connectivity.
  6. Supply interruption during a LoRa outage.
  7. Leak-induced tank drainage during a LoRa outage.
  8. Simultaneous supply interruption and leakage during a LoRa outage.

Verifies local alert generation, SD-card buffering, post-outage delivery,
duplicate suppression and timestamp consistency. The "internet unavailable" case is
the same as any other run: nothing here uses the internet. Send priority (alerts first) is
checked in the edge/aqua_edge.py self-check, not here.
"""
import os
import tempfile


from edge.aqua_edge import FlatNode, TankNode, DAY  # noqa: E402
from gateway.core import Gateway, Topology  # noqa: E402
from tests.offline_radio import exchange  # noqa: E402
from hal.packet import sim_key  # noqa: E402


class OutageRadioHAL:
    key = staticmethod(sim_key)

    def __init__(self, name, clock, readings, inboxes, outage_windows, storage_path=None):
        self.name = name
        self.clock = clock
        self.readings = readings
        self.inboxes = inboxes
        self.outage_windows = outage_windows
        self.storage_path = storage_path
        inboxes[name] = []
        self.leds = []

    def now(self):
        return self.clock[0]

    def read(self):
        return self.readings[0][self.name]

    def _link_down(self):
        t = self.clock[0]
        return any(s <= t < e for s, e in self.outage_windows)

    def send(self, msg, cad=False):
        if self._link_down():
            return  # silently dropped on air during LoRa outage
        for n, box in self.inboxes.items():
            if n != self.name:
                box.append(msg)

    def receive(self):
        msgs, self.inboxes[self.name] = self.inboxes[self.name], []
        return msgs

    def led(self, colour):
        self.leds.append((self.clock[0], colour))


def run_scenario(name, outage_windows=(), supply_cut=False, tank_leak=False, flat_leak=False):
    flats = ["B-401", "B-301", "B-201", "B-101", "B-G01"]
    clock, readings, inboxes = [0.0], [{}], {}
    td = tempfile.mkdtemp(prefix=f"offline_{name}_")
    hal = lambda n: OutageRadioHAL(
        n, clock, readings, inboxes, outage_windows,
        storage_path=os.path.join(td, f"{n}_q.jsonl") if n != "GATEWAY" else None,
    )
    nodes = [FlatNode(f, hal(f)) for f in flats] + [TankNode("B-TANK", hal("B-TANK"))]
    gw = Gateway(hal("GATEWAY"), Topology.load().only(flats))

    for m in range(36 * 60):
        t = m * 60.0
        clock[0] = t
        # each flat draws 0.05 L/s for 10 min in every 20: ordinary use, never a "leak"
        use = 0.05 if m % 20 < 10 else 0.0
        leaking = flat_leak and t >= 26 * 3600
        demand = use * len(flats) + (0.5 if leaking else 0.0)
        inflow, outflow, level, p_drop = 0.125, demand, 1.5, 0.0
        if t >= 26 * 3600:
            if supply_cut and not tank_leak:
                inflow, level, p_drop = 0.0, 0.35, 0.8
            elif tank_leak and not supply_cut:
                outflow, level, p_drop = demand + 1.675, 0.35, 0.7
            elif supply_cut and tank_leak:
                inflow, outflow, level, p_drop = 0.0, demand + 1.475, 0.30, 1.1

        r = {
            f: {
                "flow_lps": use + (0.5 if leaking and f == "B-201" else 0.0),
                "pressure_m": 10.0 - p_drop - (0.7 if leaking and f == "B-201" else 0.0),
            }
            for f in flats
        }
        r["B-TANK"] = {
            "outflow_lps": outflow,
            "inflow_lps": inflow,
            "level_m": level,
            "ph": 7.2, "tds_ppm": 250.0, "turbidity_ntu": 0.5, "temp_c": 25.0,
        }
        readings[0] = r
        for n in nodes:
            n.step()
        exchange(nodes, gw, inboxes)

    # Check queue drained after connectivity returns (all outages end before 36h)
    remaining_q = sum(len(n.store.queue) for n in nodes)
    # Check duplicate suppression in gw.log: (n, s) must be unique
    keys = [(m["n"], m["s"]) for m in gw.log]
    assert len(keys) == len(set(keys)), f"{name}: duplicate packet logged!"
    # Check timestamp consistency: every logged packet has t <= receive time (causal)
    assert all(m["t"] <= m["rx"] for m in gw.log), f"{name}: message stamped after it arrived!"
    # Flat leak alerts may only come from the flat that has the leak
    wrong = [(m["n"], m["a"], m["t"]) for m in gw.log
             if m["k"] == "ALERT" and m["a"].startswith("LEAK_") and not (flat_leak and m["n"] == "B-201")]
    assert not wrong, f"{name}: false flat leak alerts {wrong}"
    events = [e["event"] for e in gw.events]
    alerts = [(m["n"], m["a"], m["t"]) for m in gw.log if m["k"] == "ALERT"]
    return {
        "scenario": name,
        "remaining_queue": remaining_q,
        "unique_packets": len(keys),
        "events": events,
        "alerts": alerts,
    }


def main():
    results = []
    # 1. Normal with LoRa
    r1 = run_scenario("1_normal_lora")
    assert r1["remaining_queue"] == 0 and "PIPE_LEAK" not in r1["events"] and "SUPPLY_EVENT" not in r1["events"]
    results.append(r1)

    # 2. Internet unavailable, LoRa operational (local edge + gateway fully offline)
    r2 = run_scenario("2_offline_no_internet_lora_ok", flat_leak=True)
    assert r2["remaining_queue"] == 0 and any(a == "LEAK_PRESSURE_DROP" for _, a, _ in r2["alerts"])
    results.append(r2)

    # 3. Temporary LoRa interruption (30 min: 26h..26.5h)
    r3 = run_scenario("3_temp_lora_outage_30m", outage_windows=[(26 * 3600, 26.5 * 3600)], flat_leak=True)
    assert r3["remaining_queue"] == 0 and any(a == "LEAK_PRESSURE_DROP" for _, a, _ in r3["alerts"])
    results.append(r3)

    # 4. Long LoRa outage (3.5 h: 26h..29.5h) with buffered flat leak
    r4 = run_scenario("4_long_lora_outage_3h30m", outage_windows=[(26 * 3600, 29.5 * 3600)], flat_leak=True)
    assert r4["remaining_queue"] == 0 and any(a == "LEAK_PRESSURE_DROP" for _, a, _ in r4["alerts"])
    results.append(r4)

    # 5. Supply interruption during normal connectivity
    r5 = run_scenario("5_supply_cut_normal_lora", supply_cut=True)
    assert r5["remaining_queue"] == 0 and "SUPPLY_EVENT" in r5["events"] and "PIPE_LEAK" not in r5["events"]
    results.append(r5)

    # 6. Supply interruption during 3.5h LoRa outage
    r6 = run_scenario("6_supply_cut_during_outage", outage_windows=[(26 * 3600, 29.5 * 3600)], supply_cut=True)
    assert r6["remaining_queue"] == 0 and "SUPPLY_EVENT" in r6["events"]
    results.append(r6)

    # 7. Leak-induced tank drainage during 3.5h LoRa outage
    r7 = run_scenario("7_leak_drain_during_outage", outage_windows=[(26 * 3600, 29.5 * 3600)], tank_leak=True)
    assert r7["remaining_queue"] == 0 and "PIPE_LEAK" in r7["events"] and "LEAK_TANK_DRAIN" in r7["events"]
    results.append(r7)

    # 8. Simultaneous supply interruption + leakage during 3.5h LoRa outage
    r8 = run_scenario("8_simultaneous_during_outage", outage_windows=[(26 * 3600, 29.5 * 3600)], supply_cut=True, tank_leak=True)
    assert r8["remaining_queue"] == 0 and "PIPE_LEAK" in r8["events"] and "LEAK_TANK_DRAIN" in r8["events"]
    results.append(r8)

    for r in results:
        print(f"{r['scenario']}: queue={r['remaining_queue']} pkts={r['unique_packets']} events={r['events']}")
    print("ALL 8 OFFLINE + SUPPLY SCENARIOS PASSED")


if __name__ == "__main__":
    main()
