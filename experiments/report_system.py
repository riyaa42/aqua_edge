"""Tables and figures for experiments 1, 2, 3 and 5, from the logs of system_run.py.

    uv run python -m experiments.report_system
"""
import collections
import csv
import json
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from experiments.style import INK, MUTED, NODE_COLOR, SERIES, finish, new_figure, write_table
from experiments.system_run import DAY, NODES, read_jsonl

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS, OUT = os.path.join(HERE, "runs"), os.path.join(HERE, "results")
DETECTION_KINDS = ("ALERT", "FLAG")
STATUS = ("VALVE_CLOSED", "VALVE_OPENED", "VALVE_FAULT", "QUALITY_CLEARED", "QUALITY_TEMPORAL_CLEARED",
          "FLAT_LEAK_CLEARED", "CONSUMPTION_CLEARED", "ZERO_FLOW_CLEARED", "SUPPLY_FLOW_CLEARED", "SUPPLY_RECOVERED")
BLOCKS = range(96, 191)         # the 95 complete 15-minute blocks of day 2


def clock(sample):
    s = int(sample - DAY)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}"


def verdict(node, code, sample):
    """Whether a detection matches an event planted in sensors.csv (simulation/webots/data/events.csv)."""
    h = (sample - DAY) / 3600
    if node == "B-203" and code.startswith("LEAK_") and h >= 2:
        return "matches planted leak (from 02:00)"
    if node == "B-TANK" and code.startswith("QUALITY_"):
        if 6 <= h < 7.25 or 20 <= h < 21:
            return "matches planted water-quality event"
        return "false alarm"
    if node == "B-TANK" and code == "SUPPLY_LOW" and 9 <= h < 17.5:
        return "matches planted pump power cut (09:00-17:00)"
    return "false alarm"


class Data:
    def __init__(self, name):
        self.dir = os.path.join(RUNS, name)
        with open(os.path.join(self.dir, "run.json")) as f:
            self.run = json.load(f)
        with open(os.path.join(self.dir, "samples.json")) as f:
            self.samples = json.load(f)
        with open(os.path.join(self.dir, "frames.json")) as f:
            self.frames = json.load(f)
        self.node = {n: read_jsonl(os.path.join(self.dir, f"probe-{n}.jsonl")) for n in NODES}
        self.gw = read_jsonl(os.path.join(self.dir, "probe-gateway.jsonl"))
        self.relay = read_jsonl(os.path.join(self.dir, "relay.jsonl"))
        self.first_rx, self.repeats = {}, collections.Counter()
        for e in self.gw:
            if e["e"] == "message":
                key = (e["n"], e["s"])
                if e["repeat"]:
                    self.repeats[e["n"]] += 1
                else:
                    assert key not in self.first_rx, f"accepted twice: {key}"
                    self.first_rx[key] = e["w"]
        self.queued = {n: [e for e in self.node[n] if e["e"] == "queued"] for n in NODES}
        self.start = self.mark("started")

    def mark(self, what):
        return next((m["w"] for m in self.run["marks"] if m["what"] == what), None)

    def detections(self, node):
        return [(q["code"], q["sample"]) for q in self.queued[node]
                if q["k"] in DETECTION_KINDS and q["code"] not in STATUS]

    def blocks_at_gateway(self, node):
        return {q["block"] for q in self.queued[node] if q["k"] == "SUM" and (node, q["s"]) in self.first_rx
                and q["block"] in BLOCKS}

    def blocks_on_dashboard(self, node):
        return {f["sums"].get(node) for f in self.frames} & set(BLOCKS)


# --- Experiment 1 ---
def experiment1(d):
    rows, alerts = [], []
    for n in NODES:
        q = d.queued[n]
        kinds = collections.Counter(m["k"] for m in q)
        got = sum((n, m["s"]) in d.first_rx for m in q)
        det = d.detections(n)
        false = sum(verdict(n, c, t) == "false alarm" for c, t in det)
        up = [r for r in d.relay if r["dir"] == "up" and r["n"] == n]
        rows.append([n, len(q), kinds["SUM"], kinds["ALERT"] + kinds["FLAG"], got, f"{100 * got / len(q):.1f}%",
                     len(q) - got, len(up), d.repeats[n], len(det), len(det) - false, false,
                     f"{len(d.blocks_at_gateway(n))}/{len(BLOCKS)}", f"{len(d.blocks_on_dashboard(n))}/{len(BLOCKS)}"])
        for c, t in det:
            alerts.append([n, clock(t), c, verdict(n, c, t)])
        for e in d.node[n]:
            if e["e"] == "valve" and e["closed"] and e["sample"] is not None:
                alerts.append([n, clock(e["sample"]), "(valve closed)", ""])
    write_table(OUT, "exp1_node_summary", ["Node", "Messages generated", "15-min summaries", "Alerts, flags and status",
                "Unique at gateway", "Delivery ratio", "Missing", "Packets sent (incl. retries)",
                "Repeats the gateway rejected", "Detections", "Matching a planted event", "False alarms",
                "Summaries at gateway", "Summary blocks visible on dashboard"], rows)
    alerts.sort(key=lambda r: (NODES.index(r[0]), r[1]))
    write_table(OUT, "exp1_alerts", ["Node", "Data time (day 2)", "Alert", "Against the planted events"], alerts)

    fig, ax = new_figure(13, 4.6)
    bands = [(2, 24, "B-203", "planted leak"), (6, 7, "B-TANK", "runoff"), (20, 20.75, "B-TANK", "chemical"),
             (9, 17, "B-TANK", "power cut")]
    ypos = {n: len(NODES) - 1 - i for i, n in enumerate(NODES)}
    for a, b, n, label in bands:
        ax.barh(ypos[n], b - a, left=a, height=0.62, color="#e7e6e2", zorder=1)
        ax.text(a + 0.1, ypos[n] + 0.33, label, fontsize=8.5, color=MUTED, va="bottom")
    seen = collections.Counter()
    for n in NODES:
        for c, t in sorted(d.detections(n), key=lambda x: x[1]):
            h = (t - DAY) / 3600
            bad = verdict(n, c, t) == "false alarm"
            ax.scatter(h, ypos[n], marker="X" if bad else "o", s=95, color=NODE_COLOR[n], edgecolors=INK,
                       linewidths=1.1 if bad else 0.4, zorder=3)
            key = (n, round(h))
            short = c.replace("QUALITY_", "quality: ").replace("LEAK_", "leak: ").replace("_", " ").lower()
            ax.annotate(short, (h, ypos[n]), xytext=(0, -13 - 10 * seen[key]), textcoords="offset points",
                        ha="center", fontsize=7.5, color=INK, zorder=4)
            seen[key] += 1
    ax.set_yticks([ypos[n] for n in NODES], NODES)
    ax.set_ylim(-0.75, len(NODES) - 0.35)
    ax.set_xlim(-0.6, 24.4)
    ax.set_xticks(range(0, 25, 2), [f"{h:02d}:00" for h in range(0, 25, 2)])
    ax.set_xlabel("data time, day 2 of sensors.csv")
    ax.scatter([], [], marker="o", color=MUTED, s=70, label="matches a planted event")
    ax.scatter([], [], marker="X", color=MUTED, edgecolors=INK, s=70, label="false alarm")
    ax.legend(loc="upper right", frameon=False, bbox_to_anchor=(1, 1.13), ncols=2)
    finish(fig, ax, OUT, "exp1_alert_timeline", "Experiment 1: alerts on the four-node replay")
    return d


# --- Experiment 2 ---
def queue_series(d, node):
    """(minutes since the start of day 2, messages waiting) at every change."""
    t0 = d.mark("all nodes on day 2")
    pts = [((e["w"] - t0) / 60, e["queue"]) for e in d.node[node] if e["e"] in ("queued", "acked")]
    return [p[0] for p in pts], [p[1] for p in pts]


def experiment2(d, base):
    start, end = d.mark("outage starts"), d.mark("outage ends")
    t0 = d.mark("all nodes on day 2")
    rows = []
    for n in NODES:
        q = d.queued[n]
        during = [m for m in q if start <= m["w"] < end]
        waiting_at_end = [m for m in q if m["w"] < end and d.first_rx.get((n, m["s"]), 1e18) >= end]
        later = sum((n, m["s"]) in d.first_rx for m in waiting_at_end)
        missing = sum((n, m["s"]) not in d.first_rx for m in q)
        acks = {e["s"]: e["w"] for e in d.node[n] if e["e"] == "acked"}
        drain = max((acks.get(m["s"], float("nan")) for m in waiting_at_end), default=end) - end
        first_in = min((d.first_rx[(n, m["s"])] for m in waiting_at_end if (n, m["s"]) in d.first_rx), default=end) - end
        det_during = [m for m in during if m["k"] in DETECTION_KINDS and m["code"] not in STATUS]
        valves = [clock(e["sample"]) for e in d.node[n] if e["e"] == "valve" and e["closed"] and start <= e["w"] < end]
        same = d.detections(n) == base.detections(n)
        readings = sum(e["e"] == "step" and start <= e["w"] < end for e in d.node[n])
        rows.append([n, readings, len(during), max(e["queue"] for e in d.node[n] if e["e"] in ("queued", "acked")),
                     len(waiting_at_end), later, missing, d.repeats[n], f"{first_in:.1f}", f"{drain:.1f}",
                     "; ".join(f"{m['code']} {clock(m['sample'])}" for m in det_during) or "none",
                     ", ".join(valves) or "none", "yes" if same else "NO"])
    write_table(OUT, "exp2_outage_recovery", ["Node", "Readings processed during outage",
                "Messages generated during outage", "Peak messages buffered", "Waiting when the link returned",
                "Of those, delivered after reconnection", "Missing at end", "Repeats the gateway rejected (whole run)",
                "First backlog message after reconnection (s)", "Backlog drain time (s)",
                "Detections raised during outage (data time)", "Valve closed during outage (data time)",
                "Detections identical to the baseline run"], rows)
    dropped = [r for r in d.relay if r["dropped"]]
    info = {"outage_s": round(end - start, 1), "data_start": clock(DAY + next(
        m["data_s"] for m in d.run["marks"] if m["what"] == "outage starts")), "data_end": clock(
        DAY + next(m["data_s"] for m in d.run["marks"] if m["what"] == "outage ends")),
        "packets_dropped_up": sum(r["dir"] == "up" for r in dropped),
        "packets_dropped_down": sum(r["dir"] == "down" for r in dropped),
        "generated_total": sum(len(d.queued[n]) for n in NODES), "received_unique": len(d.first_rx)}

    fig, ax = new_figure(13, 4.6)
    o0, o1 = (start - t0) / 60, (end - t0) / 60
    ax.axvspan(o0, o1, color="#e7e6e2", zorder=0)
    ax.text((o0 + o1) / 2, 0.97, "node-gateway link cut (5 min)", transform=ax.get_xaxis_transform(), ha="center",
            va="top", fontsize=9.5, color=MUTED)
    for n in NODES:
        x, y = queue_series(d, n)
        ax.step(x, y, where="post", lw=2, color=NODE_COLOR[n], label=n)
    ax.set_xlabel(f"minutes since the replay of day 2 started ({d.run['step_s']:g} s per data minute)")
    ax.set_ylabel("messages waiting on the node")
    ax.legend(frameon=False, ncols=4, loc="upper right", bbox_to_anchor=(1, 1.11))
    finish(fig, ax, OUT, "exp2_buffer_vs_time", "Experiment 2: node buffers through a gateway-link outage")
    return info


# --- Experiment 3 ---
def experiment3(d):
    stop, restart, healthy = d.mark("outage starts"), d.mark("outage ends"), d.mark("dashboard healthy again")
    t0, drained = d.mark("all nodes on day 2"), d.mark("backlog drained")
    made = {e["seq"]: e for e in d.gw if e["e"] == "snapshot"}
    stored = {f["seq"]: f for f in d.frames}
    posts = [e for e in d.gw if e["e"] == "post"]
    ok = [p for p in posts if p["ok"]]
    dup = sum(p["reply"].get("duplicates", 0) for p in ok)
    during = {s: e for s, e in made.items() if stop <= e["w"] < healthy}
    last_stored = max(stored)
    left_at_stop = sorted(s for s in made if s not in stored and s > last_stored)
    missing = sorted(s for s in made if s not in stored and s < last_stored)
    backlog_clear = min((e["w"] for e in d.gw if e["e"] == "post" and e["ok"] and e["w"] >= healthy), default=None)
    last_during = max(stored[s]["received"] for s in during if s in stored)
    msgs = sum(1 for k, w in d.first_rx.items() if stop <= w < healthy)
    decisions = sum(1 for e in d.gw if e["e"] == "decision" and stop <= e["w"] < healthy)
    info = [["Dashboard backend down (s)", f"{restart - stop:.0f} stopped, healthy again {healthy - restart:.1f} s after restart"],
            ["Snapshots generated (whole run)", len(made)], ["Snapshots generated while down", len(during)],
            ["Peak snapshots queued in the gateway outbox", max(e["pending"] for e in made.values())],
            ["Snapshots stored by the dashboard", len(stored)],
            ["Of those generated while down, stored", sum(s in stored for s in during)],
            ["Missing (gaps in the stored sequence)", len(missing)],
            ["Still in the outbox when the gateway was stopped (kept for its next start)", len(left_at_stop)],
            ["Duplicate frames stored", len(d.frames) - len(stored)], ["Duplicate frames the server rejected", dup],
            ["Failed delivery attempts", len(posts) - len(ok)],
            ["Healthy to first successful delivery (s)", f"{backlog_clear - healthy:.1f}"],
            ["Healthy to last outage snapshot stored (recovery time, s)", f"{last_during - healthy:.1f}"],
            ["Node messages the gateway accepted while the dashboard was down", msgs],
            ["Gateway decisions made while the dashboard was down", decisions],
            ["Node messages generated / accepted (whole run)",
             f"{sum(len(d.queued[n]) for n in NODES)} / {len(d.first_rx)}"]]
    write_table(OUT, "exp3_dashboard_outage", ["Measure", "Value"], info)

    fig, ax = new_figure(13, 4.6)
    x = [(s["w"] - t0) / 60 for s in d.samples if s["w"] >= t0]
    y = [s["outbox"] for s in d.samples if s["w"] >= t0]
    ax.axvspan((stop - t0) / 60, (healthy - t0) / 60, color="#e7e6e2", zorder=0)
    ax.text(((stop + healthy) / 2 - t0) / 60, 0.97, "dashboard backend stopped (5 min)",
            transform=ax.get_xaxis_transform(), ha="center", va="top", fontsize=9.5, color=MUTED)
    ax.step(x, y, where="post", lw=2, color=SERIES[0])
    peak = int(np.argmax(y))
    ax.annotate(f"peak {y[peak]}", (x[peak], y[peak]), xytext=(4, 4), textcoords="offset points", fontsize=9.5)
    ax.set_xlabel(f"minutes since the replay of day 2 started ({d.run['step_s']:g} s per data minute)")
    ax.set_ylabel("snapshots in the gateway outbox")
    finish(fig, ax, OUT, "exp3_outbox_vs_time", "Experiment 3: gateway outbox through a dashboard outage")


# --- Experiment 5 ---
def pct(values, q):
    return float(np.percentile(values, q)) if len(values) else float("nan")


def usage(d, name, since, until):
    pts = [(s["w"], s["usage"][name]) for s in d.samples if name in s["usage"] and since <= s["w"] <= until]
    wall = pts[-1][0] - pts[0][0]
    cpu = pts[-1][1]["cpu_s"] - pts[0][1]["cpu_s"]
    return {"cpu_s": cpu, "wall_s": wall, "cpu_pct": 100 * cpu / wall, "rss": np.mean([p["rss_mb"] for _, p in pts]),
            "rss_peak": max(p["rss_mb"] for _, p in pts), "pss": np.mean([p["pss_mb"] for _, p in pts])}


def experiment5(base, real):
    procs = [*NODES, "gateway", "dashboard"]
    res, rows = {}, []
    for label, d, until in (("fast replay", base, base.mark("replay finished")),
                            ("real time", real, real.samples[-1]["w"] - 1)):
        for p in procs:
            u = res[label, p] = usage(d, p, d.mark("all nodes on day 2") + 2, until)
            steps = sum(e["e"] == "step" and e["w"] >= d.mark("all nodes on day 2") + 2 and e["w"] <= until
                        for e in d.node.get(p, []))
            rows.append([label, p, f"{u['wall_s']:.0f}", f"{u['cpu_s']:.2f}", f"{u['cpu_pct']:.2f}", steps or "",
                         f"{u['rss']:.1f}", f"{u['rss_peak']:.1f}", f"{u['pss']:.1f}"])
    write_table(OUT, "exp5_cpu_memory", ["Run", "Process", "Measured seconds", "CPU seconds",
                "CPU, % of one core", "Readings processed", "RSS mean (MB)", "RSS peak (MB)", "PSS mean (MB)"], rows)

    series = {}
    for n in NODES:
        steps = [e for e in base.node[n] if e["e"] == "step"]
        series[f"{n}: one sensor reading,\ndetection only"] = [e["detect_us"] / 1000 for e in steps]
        series[f"{n}: whole step\n(detect, save, send)"] = [e["wall_us"] / 1000 for e in steps]
    handled = [e for e in base.gw if e["e"] == "handled"]
    series["gateway: per received packet\n(incl. saving state)"] = [e["wall_us"] / e["packets"] / 1000 for e in handled]
    series["gateway CPU only,\nper received packet"] = [e["cpu_us"] / e["packets"] / 1000 for e in handled]
    series["node message queued to\naccepted by gateway"] = [
        1000 * (base.first_rx[n, q["s"]] - q["w"]) for n in NODES for q in base.queued[n] if (n, q["s"]) in base.first_rx]
    series["dashboard ingest request\n(HTTP round trip)"] = [e["ms"] for e in base.gw if e["e"] == "post" and e["ok"]]
    series["snapshot taken to stored\nin the dashboard"] = [1000 * (f["received"] - f["created"]) for f in base.frames]
    rows = [[k.replace("\n", " "), len(v), f"{pct(v, 50):.3f}", f"{pct(v, 95):.3f}", f"{max(v):.3f}"]
            for k, v in series.items()]
    write_table(OUT, "exp5_latency_ms", ["Measure (fast replay, ms)", "Samples", "Median", "95th percentile", "Max"], rows)

    plt.rcParams.update({"figure.facecolor": "#fcfcfb"})
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.6), gridspec_kw={"width_ratios": [1, 1, 1]})
    labels, colors = procs, [NODE_COLOR.get(p, "#8a8984") for p in procs]
    panels = [("CPU, real time (1 reading/min)\n% of one core", [res["real time", p]["cpu_pct"] for p in procs], "{:.2f}"),
              (f"CPU, fast replay ({1 / base.run['step_s']:g} readings/s)\n% of one core", [res["fast replay", p]["cpu_pct"] for p in procs], "{:.2f}"),
              ("Memory, real time\nmean resident MB", [res["real time", p]["rss"] for p in procs], "{:.0f}")]
    for ax, (title, values, fmt) in zip(axes, panels):
        bars = ax.bar(labels, values, color=colors, width=0.66, zorder=2)
        ax.bar_label(bars, [fmt.format(v) for v in values], fontsize=9, padding=2)
        ax.set_title(title, fontsize=10.5, color=INK, loc="left")
        ax.tick_params(axis="x", rotation=35, labelsize=9)
        ax.set_ylim(0, max(values) * 1.18)
    finish(fig, axes, OUT, "exp5_cpu_memory", "Experiment 5: processor and memory per process (laptop, i9-13900H)")

    keys = ["B-TANK: one sensor reading,\ndetection only", "B-203: one sensor reading,\ndetection only",
            "B-TANK: whole step\n(detect, save, send)", "B-203: whole step\n(detect, save, send)",
            "gateway CPU only,\nper received packet", "gateway: per received packet\n(incl. saving state)",
            "node message queued to\naccepted by gateway", "dashboard ingest request\n(HTTP round trip)",
            "snapshot taken to stored\nin the dashboard"]
    fig, ax = new_figure(13, 5.8)
    for i, k in enumerate(keys):
        y = len(keys) - 1 - i
        lo, hi = pct(series[k], 50), pct(series[k], 95)
        ax.plot([lo, hi], [y, y], color="#b9b8b2", lw=2, zorder=2)
        ax.scatter(lo, y, s=80, color=SERIES[0], zorder=3, label="median" if i == 0 else None)
        ax.scatter(hi, y, s=80, color=SERIES[1], marker="D", zorder=3, label="95th percentile" if i == 0 else None)
        ax.annotate(f"{lo:.2g} / {hi:.2g} ms", (hi, y), xytext=(9, -3), textcoords="offset points", fontsize=9)
    ax.set_yticks(range(len(keys)), [k.replace("\n", " ") for k in keys][::-1], fontsize=9.5)
    ax.set_xscale("log")
    ax.set_xlim(right=ax.get_xlim()[1] * 4)
    ax.set_xlabel("milliseconds (log scale)")
    ax.legend(frameon=False, ncols=2, loc="upper right", bbox_to_anchor=(1, 1.09))
    finish(fig, ax, OUT, "exp5_processing_latency",
           "Experiment 5: processing and delivery times (laptop; network on localhost, no radio)")


def main():
    os.makedirs(OUT, exist_ok=True)
    base = experiment1(Data("baseline"))
    info2 = experiment2(Data("node-outage"), base)
    experiment3(Data("dashboard-outage"))
    experiment5(base, Data("realtime"))
    with open(os.path.join(OUT, "exp2_totals.json"), "w") as f:
        json.dump(info2, f, indent=1)
    print(json.dumps(info2))


if __name__ == "__main__":
    main()
