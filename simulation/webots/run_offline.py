"""Run the real firmware for all nodes + gateway without Webots, then score it against the planted events.

    python run_offline.py              # with the network outage from data/outages.csv
    python run_offline.py --no-outage  # same scenario, perfect network

Fast (seconds), so use it to test firmware changes before opening Webots.
Results go to results/offline[_no_outage]/.
"""
import csv
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, "firmware")]
from aqua_edge import FlatNode, TankNode, DAY  # noqa: E402
from gateway import Gateway  # noqa: E402
from simlink import SensorFeed, load_outages, link_up, STEP_S  # noqa: E402

GATEWAY = "GATEWAY"


class Radio:
    """Every message is heard by everyone whose link is up; delivered at the next step."""

    def __init__(self, outages):
        self.outages, self.inbox = outages, {}

    def send(self, src, msg, t):
        if not link_up(src, t, self.outages):
            return
        for dst in self.inbox:
            if dst != src and link_up(dst, t, self.outages):
                self.inbox[dst].append(dict(msg))


class OfflineHAL:
    def __init__(self, name, clock, feed, radio, valve_log, storage_dir):
        self.name, self.clock, self.feed, self.radio, self.valve_log = name, clock, feed, radio, valve_log
        self.storage_path = os.path.join(storage_dir, f"{name}_sd.jsonl")
        radio.inbox[name] = []
        self.colour = "green"

    def now(self):
        return self.clock[0]

    def read(self):
        return self.feed.read(self.name, self.now())

    def send(self, msg):
        self.radio.send(self.name, msg, self.now())

    def receive(self):
        msgs, self.radio.inbox[self.name] = self.radio.inbox[self.name], []
        return msgs

    def set_valve(self, closed, reason):
        self.valve_log.append({"t": self.now(), "node": self.name, "closed": closed, "reason": reason})

    def led(self, colour):
        self.colour = colour


def run(outages, out_dir):
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir)
    feed, clock, valve_log = SensorFeed(), [0.0], []
    radio = Radio(outages)
    names = feed.nodes()
    flats = [n for n in names if n != "B-TANK"]
    hal = lambda n: OfflineHAL(n, clock, feed, radio, valve_log, out_dir)
    nodes = [TankNode(n, hal(n)) if n == "B-TANK" else FlatNode(n, hal(n)) for n in names]
    gw = Gateway(hal(GATEWAY), flats, "B-TANK")
    while clock[0] < feed.duration_s:
        for node in nodes:
            node.step()
        gw.step()
        clock[0] += STEP_S
    return nodes, gw, valve_log


def write_csv(path, rows):
    keys = sorted({k for r in rows for k in r}, key=lambda k: (k not in ("t", "rx", "n", "k", "a"), k))
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, keys)
        w.writeheader()
        w.writerows(rows)


def hhmm(t):
    d, s = divmod(int(t), DAY)
    return f"day{d + 1} {s // 3600:02d}:{s % 3600 // 60:02d}"


def score(gw, valve_log, nodes):
    with open(os.path.join(HERE, "data", "events.csv")) as f:
        events = {r["event"]: (r["kind"], float(r["start_s"]), float(r["end_s"]), r["where"]) for r in csv.DictReader(f)}
    alerts = [m for m in gw.log if m["k"] == "ALERT"]
    first = lambda node, kinds, after: next((m for m in sorted(alerts, key=lambda m: m["t"])
                                              if m["n"] == node and m["a"] in kinds and m["t"] >= after), None)
    gw_first = lambda kind, after: next((e for e in gw.events if e["event"] == kind and e["t"] >= after), None)
    lines, checks = [], {}

    def report(label, ev, found, created_key="t"):
        start = events[ev][1]
        if found is None:
            lines.append(f"{ev} {label}: NOT DETECTED")
            return False
        det = found[created_key] - start
        rx = found.get("rx", found[created_key])
        lines.append(f"{ev} {label}: detected {hhmm(found[created_key])} ({det / 60:.0f} min after start), "
                     f"reached gateway {hhmm(rx)} (+{(rx - found[created_key]) / 60:.0f} min)")
        return True

    checks["E1"] = report("flat leak B-203 (night flow)", "E1", first("B-203", {"LEAK_NIGHT_FLOW"}, events["E1"][1]))
    checks["E3a"] = report("pipe burst (tank node, abnormal supply)", "E3", first("B-TANK", {"ABNORMAL_SUPPLY"}, events["E3"][1]))
    checks["E3b"] = report("pipe burst (gateway mass balance, Ali [6])", "E3", gw_first("PIPE_LEAK", events["E3"][1]))
    checks["E4"] = report("rainwater-like water", "E4", first("B-TANK", {"QUALITY_RUNOFF"}, events["E4"][1]))
    checks["E5"] = report("chemical contamination", "E5", first("B-TANK", {"QUALITY_CHEMICAL"}, events["E5"][1]))
    _, cut_start, cut_end, _ = events["E2"]
    sup = gw_first("SUPPLY_EVENT", cut_start)
    lines.append(f"E2 power cut: gateway supply event {'at ' + hhmm(sup['t']) if sup else 'NOT raised'}")
    planned = {("B-203", "continuous night flow"), ("B-203", "pressure drop with high flow"), ("B-TANK", "water quality runoff"),
               ("B-TANK", "water quality chemical"), ("B-TANK", "water quality back to normal")}
    false_closures = [v for v in valve_log if v["closed"] and (v["node"], v["reason"]) not in planned]
    lines.append(f"Valve actions: {len(valve_log)}; closures not explained by a planted event: {len(false_closures)}")
    for v in false_closures:
        lines.append(f"   false closure: {v['node']} at {hhmm(v['t'])} ({v['reason']})")
    unexplained = [m for m in alerts if (m["n"], m["a"]) not in {
        ("B-203", "LEAK_NIGHT_FLOW"), ("B-203", "ABNORMAL_CONSUMPTION"), ("B-203", "LEAK_PRESSURE_DROP"), ("B-TANK", "ABNORMAL_SUPPLY"),
        ("B-TANK", "QUALITY_RUNOFF"), ("B-TANK", "QUALITY_CHEMICAL"), ("B-TANK", "QUALITY_CLEARED")}]
    lines.append(f"Alerts not explained by a planted event: {len(unexplained)}")
    for m in unexplained:
        lines.append(f"   {m['n']} {m['a']} at {hhmm(m['t'])}")
    lines.append(f"Messages received by gateway: {len(gw.log)}"
                 + (f"; still waiting in node SD queues at the end: {sum(len(n.store.queue) for n in nodes)}" if nodes else ""))
    return lines, checks, false_closures


if __name__ == "__main__":
    no_outage = "--no-outage" in sys.argv
    out_dir = os.path.join(HERE, "results", "offline_no_outage" if no_outage else "offline")
    nodes, gw, valve_log = run([] if no_outage else load_outages(), out_dir)
    write_csv(os.path.join(out_dir, "gateway_log.csv"), gw.log)
    write_csv(os.path.join(out_dir, "gateway_events.csv"), gw.events or [{"t": "", "event": ""}])
    write_csv(os.path.join(out_dir, "valve_log.csv"), valve_log or [{"t": "", "node": ""}])
    lines, checks, false_closures = score(gw, valve_log, nodes)
    lines.insert(0, f"Network outage: {'none' if no_outage else 'day2 01:30-05:00, all nodes'}")
    print("\n".join(lines))
    with open(os.path.join(out_dir, "summary.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    # Self-check: every planted leak and quality event must be caught, with no false valve closures
    assert all(checks.values()), f"missed: {[k for k, v in checks.items() if not v]}"
    assert not false_closures, "false valve closures"
