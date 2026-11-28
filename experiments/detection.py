"""Experiment 4: controlled detection trials with known ground truth.

    uv run python -m experiments.detection [--trials 30] [--out experiments/results]

Each trial runs the real node classes and the real Gateway on a simulated clock, with encrypted
packets over a radio in memory. Day 1 is normal and is learned; one event is injected on day 2 into
one node, at a random time and size within the scenario's range. The nodes have no valve here, so an
alert never changes what is measured next. The data is synthetic: results show what the rules do on
these signals, not how they would do in a real building.

Normal flat use comes from the project's bursty demand generator (make_bursty_pattern in
simulation/scripts/build_society.py). Water-quality background has two noise levels: "steady", and
"replay", the noise of sensors.csv.
"""
import argparse
import csv
import json
import math
import os
import random
import sys
from multiprocessing import Pool

import numpy as np

from edge.aqua_edge import CONSUMPTION_RULES, FLAT_LEAK_RULES, FlatNode, TankNode
from detection.quality.quality_model import QualityModel, check_standards
from gateway.core import Gateway
from gateway.edge_based import GatewayBoard
from gateway.topology import Topology
from hal.packet import sim_key

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "simulation", "scripts"))
from build_society import LITRES_PER_FLAT_DAY, make_bursty_pattern  # noqa: E402

DAY, H = 86400, 3600
T0 = 1_800_000_000 // DAY * DAY         # a UTC midnight; the nodes' time of day
FLATS = ("B-101", "B-203", "B-301")
QUALITY_NOISE = {"steady": {"ph": 0.01, "tds": 1.0, "turb": 0.005, "temp": 0.03},
                 "replay": {"ph": 0.05, "tds": 5.0, "turb": 0.1, "temp": 0.1}}
ZERO_FLOW = ("PROLONGED_ZERO_FLOW",)
NOT_ALERTS = ("QUALITY_CLEARED", "QUALITY_TEMPORAL_CLEARED", "FLAT_LEAK_CLEARED", "CONSUMPTION_CLEARED",
              "ZERO_FLOW_CLEARED", "SUPPLY_FLOW_CLEARED", "VALVE_CLOSED", "VALVE_OPENED", "VALVE_FAULT")

# scenario -> (group, expected alert codes (any one counts), is it in the main set)
SCENARIOS = {
    "wq_normal": ("water", (), True), "wq_ph_low": ("water", ("QUALITY_LIMIT_VIOLATION",), True),
    "wq_ph_high": ("water", ("QUALITY_LIMIT_VIOLATION",), True), "wq_tds_high": ("water", ("QUALITY_LIMIT_VIOLATION",), True),
    "wq_turbidity_high": ("water", ("QUALITY_LIMIT_VIOLATION",), True), "wq_temperature_high": ("water", (), True),
    "wq_runoff_like": ("water", ("QUALITY_LIMIT_VIOLATION",), True),
    "wq_chemical_like": ("water", ("QUALITY_LIMIT_VIOLATION",), True),
    "leak_normal": ("leak", (), True), "leak_night_flow": ("leak", ("LEAK_NIGHT_FLOW",), True),
    "leak_continuous_flow": ("leak", ("LEAK_CONTINUOUS_FLOW",), True),
    "leak_flow_pressure_drop": ("leak", ("LEAK_PRESSURE_DROP",), True),
    # boundary cases, reported apart from the main scores
    "edge_night_flow_20min": ("leak", (), False), "edge_flow_150min": ("leak", (), False),
    "edge_high_flow_no_drop": ("leak", (), False), "edge_drop_no_flow": ("leak", (), False),
    "edge_long_legitimate_use": ("leak", (), False),
    "use_normal": ("use", (), True), "use_sudden_spike": ("use", ("ABNORMAL_CONSUMPTION", "SUDDEN_CONSUMPTION_CHANGE"), True),
    "use_unusual_night": ("use", ("UNUSUAL_NIGHT_CONSUMPTION",), True),
    "use_prolonged_zero": ("use", ZERO_FLOW, True),
}


class NodeHal:
    storage_path = None

    def __init__(self, sim, name):
        self.sim, self.name, self.inbox, self.readings = sim, name, [], {}

    def now(self):
        return self.sim.t

    sample_time = now

    def read(self):
        return self.readings

    def key(self, name):
        return sim_key(name)

    def send(self, raw, cad=False):
        self.sim.to_gateway.append((self.sim.t, raw))
        return True

    def receive(self):
        got, self.inbox = self.inbox, []
        return got

    def led(self, colour):
        pass


class Sim:
    def __init__(self, names):
        tree = Topology.load(os.path.join(ROOT, "gateway", "virtual_4node.json"))
        self.t, self.to_gateway = float(T0), []
        self.hals = {n: NodeHal(self, n) for n in names}
        self.nodes = {n: (TankNode if n == "B-TANK" else FlatNode)(n, h) for n, h in self.hals.items()}
        self.alerts = []                    # (node, code, sample time)
        self.gateway_alerts = []            # (node, code) as the gateway recorded them
        for name, node in self.nodes.items():
            node.emit = self.recording(name, node.emit)
        sim = self

        class Radio:
            def send(self, raw, cad=False):
                for h in sim.hals.values():
                    h.inbox.append((sim.t, raw))
                return True

            def receive(self):
                got, sim.to_gateway = sim.to_gateway, []
                return got

        keys = {n: sim_key(n) for n in ("GATEWAY", *tree.devices)}
        self.gateway = Gateway(GatewayBoard(Radio(), keys, None, clock=lambda: sim.t), tree)
        self.seen_events = 0

    def recording(self, name, emit):
        def wrapped(kind, **payload):
            if kind == "ALERT" and payload["a"] not in NOT_ALERTS:
                self.alerts.append((name, payload["a"], self.t))
            emit(kind, **payload)
        return wrapped

    def minute(self, learning):
        for node in self.nodes.values():
            if learning:
                node.prime(node.hal.read())     # as the node program learns its first day
            else:
                node.step()
        if not learning:
            self.gateway.step()
            events, self.seen_events = self.gateway.events_since(self.seen_events, consumer="experiment")
            self.gateway_alerts += [(e["node"], e["alert"]) for e in events
                                    if e["event"] in ("NEW_ALERT", "REPEAT_ALERT") and "alert" in e]
            for node in self.nodes.values():
                node.radio_poll()


def flat_background(rng, days):
    """Minute-by-minute flow (L/s) and pressure (m) of one ordinary flat."""
    nrng = np.random.default_rng(rng.randrange(2**32))
    pattern = np.array(make_bursty_pattern(nrng, days=days, step_min=5))
    flow = np.repeat(pattern, 5) * LITRES_PER_FLAT_DAY / 86400
    flow = np.where(flow > 0, np.clip(flow + nrng.normal(0, 0.0003, len(flow)), 0, None), 0.0)
    pressure = 10.5 + nrng.normal(0, 0.05, len(flow))
    return flow, pressure


def flat_event(scenario, rng):
    """(onset minute of the whole run, length in minutes, extra flow L/s or None, pressure drop m, days)."""
    day2, u = 1440, rng.uniform
    daytime = day2 + rng.randrange(6 * 60, 18 * 60)
    night = day2 + rng.randrange(60, 120)
    if scenario in ("leak_normal", "use_normal"):
        return None, 0, 0.0, 0.0, 2
    table = {
        "leak_night_flow": (night, rng.randrange(60, 151), u(0.025, 0.08), 0.0),
        "leak_continuous_flow": (daytime, rng.randrange(240, 301), u(0.008, 0.018), 0.0),
        "leak_flow_pressure_drop": (daytime, rng.randrange(30, 61), u(0.25, 0.6), u(0.5, 1.5)),
        "edge_night_flow_20min": (night, 20, u(0.025, 0.05), 0.0),
        "edge_flow_150min": (daytime, 150, u(0.008, 0.018), 0.0),
        "edge_high_flow_no_drop": (daytime, 45, u(0.25, 0.6), 0.0),
        "edge_drop_no_flow": (daytime, 45, 0.0, u(0.5, 1.5)),
        "edge_long_legitimate_use": (daytime, 240, u(0.05, 0.15), 0.0),
        "use_sudden_spike": (daytime, rng.randrange(75, 151), u(0.15, 0.4), 0.0),
        "use_unusual_night": (day2 + rng.randrange(60, 230), rng.randrange(20, 41), u(0.07, 0.2), 0.0),
    }
    if scenario == "use_prolonged_zero":
        return day2 + rng.randrange(6 * 60, 12 * 60), 30 * 60, None, 0.0, 3     # None: no water at all
    onset, length, extra, drop = table[scenario]
    return onset, length, extra, drop, 2


def run_flat_trial(args):
    scenario, trial = args
    rng = random.Random(f"{scenario}:{trial}")
    target = FLATS[trial % len(FLATS)]
    onset, length, extra, drop, days = flat_event(scenario, rng)
    signals = {n: flat_background(rng, days) for n in FLATS}
    if onset is not None:
        flow, pressure = signals[target]
        window = slice(onset, min(onset + length, days * 1440))
        if extra is None:
            flow[window] = 0.0
        else:
            flow[window] += extra
            pressure[window] -= drop
    sim = Sim(FLATS)
    for minute in range(days * 1440):
        sim.t = float(T0 + minute * 60)
        for n, h in sim.hals.items():
            h.readings = {"flow_lps": float(signals[n][0][minute]), "pressure_m": float(signals[n][1][minute])}
        sim.minute(learning=minute < 1440)
    return summarise(scenario, trial, sim, target, onset, length,
                     {"extra_lps": extra, "drop_m": drop}, bystanders=len(FLATS) - 1, days=days)


def water_event(scenario, rng):
    """The values an event replaces: {sensor: (value)}."""
    u = rng.uniform
    return {
        "wq_normal": {}, "wq_ph_low": {"ph": u(4.5, 6.2)}, "wq_ph_high": {"ph": u(8.8, 10.0)},
        "wq_tds_high": {"tds": u(600, 2000)}, "wq_turbidity_high": {"turb": u(6, 50)},
        "wq_temperature_high": {"temp": u(38, 48)},
        "wq_runoff_like": {"ph": u(5.8, 6.35), "tds": u(40, 120), "turb": u(10, 60)},
        "wq_chemical_like": {"ph": u(8.8, 10.0), "tds": u(700, 2000)},
    }[scenario]


def run_water_trial(args):
    scenario, trial, noise_name = args
    rng = random.Random(f"{scenario}:{trial}:{noise_name}")
    noise = QUALITY_NOISE[noise_name]
    event = water_event(scenario, rng)
    onset = None if scenario == "wq_normal" else 1440 + rng.randrange(2 * 60, 20 * 60)
    length = 45
    sim = Sim(("B-TANK",))
    classes = []
    model, window = QualityModel(), []
    for minute in range(2 * 1440):
        sim.t = float(T0 + minute * 60)
        hour = minute % 1440 / 60
        base = {"ph": 7.2, "tds": 250.0, "turb": 0.5, "temp": 26 + 2 * math.sin((hour - 9) / 24 * 2 * math.pi)}
        inside = onset is not None and onset <= minute < onset + length
        if inside:
            base.update(event)
        v = {k: base[k] + rng.gauss(0, noise[k]) for k in base}
        v["turb"] = max(v["turb"], 0.0)
        sim.hals["B-TANK"].readings = {"level_m": 1.5 + rng.gauss(0, 0.01), "inflow_lps": 0.0, "outflow_lps": 0.1,
                                       "ph": v["ph"], "tds_ppm": v["tds"], "turbidity_ntu": v["turb"],
                                       "temp_c": v["temp"]}
        sim.minute(learning=minute < 1440)
        if minute >= 1440:                      # reading by reading: the classifier and the limits alone
            window = (window + [(v["temp"], v["ph"], v["tds"], v["turb"])])[-3:]
            avg = [sum(x) / len(window) for x in zip(*window)]
            if inside and minute >= onset + 2:      # the 3-reading window holds only event readings
                classes.append((model.classify(*avg), check_standards(*avg[1:])[0]))
    row = summarise(scenario, trial, sim, "B-TANK", onset, length, event, bystanders=0, days=2)
    row.update(noise=noise_name, sample_classes=[c for c, _ in classes],
               sample_violations=[not ok for _, ok in classes])
    return row


def summarise(scenario, trial, sim, target, onset, length, size, bystanders, days):
    """What was raised, where, and how long after the event began."""
    start = None if onset is None else T0 + onset * 60
    grace = 15 * 60
    mine = []
    for node, code, t in sim.alerts:
        if node != target:
            continue
        inside = start is not None and start <= t <= start + length * 60 + grace
        mine.append({"code": code, "inside": inside,
                     "delay_min": round((t - start) / 60) if inside else None,
                     "at_min": round((t - T0) / 60)})
    others = [(n, c) for n, c, _ in sim.alerts if n != target]
    return {"scenario": scenario, "trial": trial, "node": target, "onset_min": onset, "length_min": length,
            "size": size, "alerts": mine, "other_node_alerts": others, "bystander_days": bystanders * (days - 1),
            "gateway_alerts": sorted(set(sim.gateway_alerts))}


def first_delay(row, codes):
    """Minutes from onset to the first of `codes` raised during the event, or None."""
    delays = [a["delay_min"] for a in row["alerts"] if a["inside"] and a["code"] in codes]
    return min(delays) if delays else None


def raised(row, codes, during_event_only=False):
    return any(a["code"] in codes and (a["inside"] or not during_event_only) for a in row["alerts"])


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--trials", type=int, default=30)
    ap.add_argument("--out", default=os.path.join(ROOT, "experiments", "results"))
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    water = [(s, i, n) for s, v in SCENARIOS.items() if v[0] == "water" for n in QUALITY_NOISE for i in range(a.trials)]
    flats = [(s, i) for s, v in SCENARIOS.items() if v[0] != "water" for i in range(a.trials)]
    with Pool(a.jobs) as pool:
        rows = pool.map(run_water_trial, water, chunksize=1) + pool.map(run_flat_trial, flats, chunksize=1)
    with open(os.path.join(a.out, "detection_trials.json"), "w") as f:
        json.dump(rows, f, separators=(",", ":"))
    with open(os.path.join(a.out, "detection_trials.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scenario", "water_noise", "trial", "node", "onset_minute", "length_min", "expected",
                    "actual_on_node", "detected", "delay_min", "alert_before_or_outside_event",
                    "gateway_named_node", "other_nodes_alerting"])
        for r in rows:
            expected = SCENARIOS[r["scenario"]][1]
            delay = first_delay(r, expected) if expected else None
            named = ""
            if delay is not None:
                named = any(n == r["node"] and c in expected for n, c in r["gateway_alerts"])
            w.writerow([r["scenario"], r.get("noise", ""), r["trial"], r["node"], r["onset_min"], r["length_min"],
                        "|".join(expected) or "none",
                        "|".join(f"{x['code']}@{x['delay_min'] if x['inside'] else 'outside'}" for x in r["alerts"]) or "none",
                        "" if not expected else delay is not None, "" if delay is None else delay,
                        "|".join(sorted({x["code"] for x in r["alerts"] if not x["inside"]})), named,
                        "|".join(sorted({f"{n}:{c}" for n, c in r["other_node_alerts"]}))])
    print(f"{len(rows)} trials written to {a.out}")


if __name__ == "__main__":
    main()
