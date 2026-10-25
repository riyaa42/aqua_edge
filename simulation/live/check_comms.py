"""Did communication work? Scores a finished live run (live/run/) against pass marks.

    python3 live/check_comms.py

Reads tx_log.csv (every radio attempt), gateway_log.jsonl (what arrived), events_applied.jsonl
(ground truth from the control panel or scenario) and the nodes' SD queues. Exits with an error
if a pass mark is missed. Pass marks are placeholders until the team agrees on them.
"""
import csv
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = os.environ.get("AQUA_RUN", os.path.join(HERE, "run"))
REPLAY = os.path.join(HERE, "..", "ns3", "replay_result.csv")
GATEWAY = "GATEWAY"

MIN_ATTEMPT_OK = 0.95        # share of uplink attempts that get through while the radio is up
MAX_ALERT_DELAY_MIN = 5      # alert created -> at gateway, when the radio is up
MAX_CATCHUP_MIN = 5          # radio back -> stored alerts at gateway
MAX_LOST = 0                 # messages never delivered by the end
MAX_REPLAY_GAP = 0.05        # Python radio model vs ns-3 replay, delivery share per node

EXPECTED = {                 # event -> (node that should alert, alert kinds); downtake leaks handled below
    "runoff": ("B-TANK", {"QUALITY_RUNOFF"}),
    "chemical": ("B-TANK", {"QUALITY_CHEMICAL"}),
}
FLAT_LEAK_ALERTS = {"LEAK_NIGHT_FLOW", "ABNORMAL_CONSUMPTION", "LEAK_PRESSURE_DROP"}


def jsonl(name):
    path = os.path.join(RUN, name)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def hhmm(t):
    return f"day{int(t // 86400) + 1} {int(t % 86400 // 3600):02d}:{int(t % 3600 // 60):02d}"


def main():
    with open(os.path.join(RUN, "tx_log.csv")) as f:
        tx = list(csv.DictReader(f))
    got = jsonl("gateway_log.jsonl")
    gw_events = jsonl("gateway_events.jsonl")
    events = jsonl("events_applied.jsonl")
    cuts = [(e["who"], e["t"], e["t"] + 60 * e["minutes"]) for e in events if e["do"] == "radio_cut"]
    cut_at = lambda node, t: any(s <= t < e and w in ("all", node) for w, s, e in cuts)
    lines, fails = [], []

    def mark(ok, text):
        lines.append(("PASS  " if ok else "FAIL  ") + text)
        if not ok:
            fails.append(text)

    # 1. Radio attempts (uplinks, nodes -> gateway)
    up = [r for r in tx if r["dst"] == GATEWAY]
    live = [r for r in up if r["result"] != "radio_cut"]
    ok = sum(r["result"] == "ok" for r in live)
    by = lambda res: sum(r["result"] == res for r in up)
    lines.append(f"Uplink attempts: {len(up)} (ok {by('ok')}, collision {by('collision')}, "
                 f"weak link {by('weak_link')}, radio cut {by('radio_cut')})")
    waited = [r for r in up if int(r.get("backoffs") or 0) > 0]
    lines.append(f"Listen-before-talk: {len(waited)} packets found the channel busy and waited "
                 f"({sum(int(r['backoffs']) for r in waited)} back-offs, {sum(r['kind'] == 'ALERT' for r in waited)} of them alerts)")
    per_node = {}
    for r in live:
        n, k = per_node.get(r["src"], (0, 0))
        per_node[r["src"]] = (n + (r["result"] == "ok"), k + 1)
    worst_node = min(per_node, key=lambda s: per_node[s][0] / per_node[s][1])
    w_ok, w_n = per_node[worst_node]
    mark(ok / max(len(live), 1) >= MIN_ATTEMPT_OK,
         f"attempts that got through while radio up: {100 * ok / max(len(live), 1):.1f}% "
         f"(worst node {worst_node} {100 * w_ok / w_n:.1f}%), pass mark {100 * MIN_ATTEMPT_OK:.0f}%")
    down = [r for r in tx if r["src"] == GATEWAY and r["result"] != "radio_cut"]
    lines.append(f"Downlinks (ACK, supply) while radio up: {sum(r['result'] == 'ok' for r in down)} of {len(down)} delivered")

    # 2. Messages: created vs delivered (store-and-forward)
    sent = {(r["src"], int(r["seq"])) for r in up}
    waiting = {(m["n"], m["s"]) for n in os.listdir(os.path.join(RUN, "sd")) for m in jsonl(os.path.join("sd", n))}
    received = {(m["n"], m["s"]) for m in got}
    lost = (sent | waiting) - received
    resends = len(up) - len(sent)
    lines.append(f"Messages created: {len(sent | waiting)}, delivered: {len(received)}, re-sends: {resends}, "
                 f"still on SD cards at the end: {len(waiting)}")
    mark(len(lost) <= MAX_LOST, f"messages never delivered: {len(lost)} (pass mark {MAX_LOST})")

    # 3. Delay, split by whether the radio was up when the message was created
    normal = [m["rx"] - m["t"] for m in got if not cut_at(m["n"], m["t"])]
    held = [m["rx"] - m["t"] for m in got if cut_at(m["n"], m["t"])]
    if normal:
        lines.append(f"Delay, radio up: median {statistics.median(normal) / 60:.1f} min, worst {max(normal) / 60:.1f} min "
                     f"({len(normal)} messages)")
    if held:
        lines.append(f"Delay, created during a radio cut: worst {max(held) / 60:.0f} min ({len(held)} messages held on SD)")
    for who, s, e in cuts:
        back = [m["rx"] for m in got if cut_at(m["n"], m["t"]) and s <= m["t"] < e and m["k"] == "ALERT"]
        if back:
            mark((max(back) - e) / 60 <= MAX_CATCHUP_MIN, f"radio cut {who} {hhmm(s)}-{hhmm(e)}: stored alerts "
                 f"delivered {(max(back) - e) / 60:.0f} min after the radio came back (pass mark {MAX_CATCHUP_MIN})")

    # 4. Events: detected, and how long until the gateway knew
    alerts = sorted((m for m in got if m["k"] == "ALERT"), key=lambda m: m["t"])
    for ev in (e for e in events if e["do"] in ("leak", "quality")):
        if ev["do"] == "quality":
            node, kinds = EXPECTED[ev["kind"]]
            label = f"quality {ev['kind']}"
        elif ev["where"].startswith("B-D"):
            node, kinds, label = "B-TANK", {"ABNORMAL_SUPPLY"}, f"{ev['size']} leak on downtake {ev['where']}"
        else:
            node, kinds, label = ev["where"], FLAT_LEAK_ALERTS, f"{ev['size']} leak in flat {ev['where']}"
        a = next((m for m in alerts if m["n"] == node and m["a"] in kinds and m["t"] >= ev["t"]), None)
        pipe = next((g for g in gw_events if g["event"] == "PIPE_LEAK" and g["t"] >= ev["t"]), None) \
            if ev["do"] == "leak" and ev["where"].startswith("B-D") else None
        if a is None and pipe is None:
            mark(False, f"{label} at {hhmm(ev['t'])}: NOT DETECTED")
            continue
        parts = []
        if a:
            parts.append(f"{node} {a['a']} after {(a['t'] - ev['t']) / 60:.0f} min, at gateway +{(a['rx'] - a['t']) / 60:.0f} min")
        if pipe:
            parts.append(f"gateway mass balance after {(pipe['t'] - ev['t']) / 60:.0f} min")
        held_ok = a is None or cut_at(a["n"], a["t"]) or (a["rx"] - a["t"]) / 60 <= MAX_ALERT_DELAY_MIN
        mark(held_ok, f"{label} at {hhmm(ev['t'])}: " + "; ".join(parts))
    for g in (g for g in gw_events if g["event"] in ("NEW_ALERT", "REPEAT_ALERT")):
        lines.append(f"Gateway: {g['event']:<12} {g['node']} {g['alert']} {hhmm(g['t'])}"
                     + (f" (#{g['count']}, {g['since_last_min']} min after the last one)" if g["event"] == "REPEAT_ALERT" else ""))
    expected_nodes = {ev.get("where") for ev in events} | {"B-TANK"}
    stray = [m for m in alerts if m["n"] not in expected_nodes]
    mark(not stray, f"alerts from nodes with no event: {len(stray)}"
         + "".join(f"\n        {m['n']} {m['a']} {hhmm(m['t'])}" for m in stray[:5]))

    # 5. Python radio model vs ns-3 replay of the same transmissions
    if os.path.exists(REPLAY):
        with open(REPLAY) as f:
            rep = list(csv.DictReader(f))
        share = lambda rows, key: {n: sum(r[key] == "ok" for r in rows if r["src"] == n) /
                                   max(sum(r["src"] == n for r in rows), 1) for n in per_node}
        gap = max(abs(a - b) for a, b in zip(share(live, "result").values(), share(rep, "result").values()))
        mark(gap <= MAX_REPLAY_GAP, f"Python radio vs ns-3 replay: largest per-node gap {100 * gap:.1f} points "
             f"(pass mark {100 * MAX_REPLAY_GAP:.0f})")
    else:
        lines.append("ns-3 replay: not run yet (ns3/replay_result.csv missing)")

    print("\n".join(lines))
    with open(os.path.join(RUN, "comms_summary.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    if fails:
        sys.exit(f"{len(fails)} check(s) failed")


if __name__ == "__main__":
    main()
