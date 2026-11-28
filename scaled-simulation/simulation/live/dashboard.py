"""Aqua Edge dashboard: watch building B and run the leak test in a browser.

    python3 live/dashboard.py              # then open http://localhost:8080
    python3 live/dashboard.py --check      # self-check, no browser or simulation needed

Standard library only. Serves dashboard.html and two small JSON endpoints. It reads the live
run's files in live/run/ (what the gateway logged, what the timekeeper applied) and appends the
buttons' commands to run/commands.jsonl for the timekeeper. "Start simulation" launches
live/run_local.sh with the demo scenario: the nodes race through their learning day and the run
pauses at the start of day 2, ready for a leak test. "Start again" stops the run, waits for it to
end and starts a fresh one (a new run wipes live/run/). Start and Start again are for the laptop
run only; under Docker, restart the containers instead.
"""
import argparse
import csv
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = os.environ.get("AQUA_RUN", os.path.join(HERE, "run"))
DEMO = ["sh", "live/run_local.sh", "--scenario", "live/scenarios/demo.jsonl", "--hours", "336"]
PROC, START_LOCK = None, threading.Lock()   # the run this dashboard started
ALIVE_S = 5                     # clock.json touched this recently = a simulation is running
SIZES = ("small", "medium", "large")
STATIC = {"/": ("dashboard.html", "text/html; charset=utf-8"),
          "/fonts/fredoka.woff2": ("fonts/fredoka.woff2", "font/woff2"),
          "/fonts/nunito.woff2": ("fonts/nunito.woff2", "font/woff2")}


def jsonl(run, name):
    try:
        with open(os.path.join(run, name)) as f:
            return [json.loads(x) for x in f if x.strip()]
    except (OSError, ValueError):
        return []


def lines(run, name):
    try:
        with open(os.path.join(run, name)) as f:
            return f.read().split()
    except OSError:
        return []


def node_of(where):
    return "B-TANK" if where.startswith("B-D") else where       # downtake leaks are seen by the tank


FLAT_LEAKS = {"LEAK_NIGHT_FLOW", "LEAK_CONTINUOUS_FLOW", "LEAK_PRESSURE_DROP"}
QUALITY_KINDS = {"QUALITY_LIMIT_VIOLATION", "QUALITY_RUNOFF", "QUALITY_CHEMICAL", "QUALITY_HOT"}
# A node's message that a problem is over -> (the problems it closes, why they ended)
CLEARS = {"FLAT_LEAK_CLEARED": (FLAT_LEAKS, "water seen off for 10 min"),
          "CONSUMPTION_CLEARED": ({"ABNORMAL_CONSUMPTION"}, "use back to normal"),
          "SUPPLY_FLOW_CLEARED": ({"ABNORMAL_SUPPLY"}, "tank outflow back to normal"),
          "QUALITY_CLEARED": (QUALITY_KINDS, "water clean again"),
          "VALVE_OPENED": ({"VALVE_CLOSED"}, "valve reopened after a water test")}
# Fix first: the team's risk score idea (gateway/analytics.py on their communication branch), our weights.
# score = weight + 20 x share of flats affected + size (litres or flow) + how long it has lasted, at most 100
WEIGHTS = {"QUALITY_CHEMICAL": 55, "QUALITY_LIMIT_VIOLATION": 52, "QUALITY_RUNOFF": 45, "PIPE_LEAK": 42,
           "QUALITY_HOT": 40, "VALVE_CLOSED": 38, "LEAK_PRESSURE_DROP": 34, "LEAK_NIGHT_FLOW": 32,
           "LEAK_CONTINUOUS_FLOW": 32, "ABNORMAL_SUPPLY": 30, "ABNORMAL_CONSUMPTION": 16, "NODE_SILENT": 10}
LEVELS = ((70, "Critical"), (45, "High"), (25, "Medium"), (0, "Low"))
IS_10500 = {"ph": (6.5, 8.5), "tds": (0, 500), "turb": (0, 5), "temp": (0, 35)}   # temp: our 35 C limit
LIVE_S = 300                    # an alert at the gateway within 5 min of being made counts as live


def problems(log, gw, applied, nodes):
    """Every problem in the run, from what the gateway knows: opened by an alert (or the gateway's own
    pipe check, or a silent node), closed by the node's recovery message, a clean night, the valve
    reopening, or a fix in the simulation. Returns (open, closed)."""
    timeline = [(m["rx"], 0, "alert", m) for m in log if m["k"] == "ALERT"] + \
               [(e["t"], 1, "gw", e) for e in gw] + [(e["t"], 2, "applied", e) for e in applied]
    opened, closed = {}, []

    def close(key, t, why):
        if key in opened:
            closed.append({**opened.pop(key), "end": t, "why": why})

    for t, _, src, e in sorted(timeline, key=lambda x: x[:2]):
        if src == "alert":
            if e["a"] in CLEARS:
                kinds, why = CLEARS[e["a"]]
                for k in kinds:
                    close((e["n"], k), t, why)
            elif (e["n"], e["a"]) not in opened:
                opened[e["n"], e["a"]] = {"n": e["n"], "a": e["a"], "t": e["t"], "rx": t,
                                         **{k: e[k] for k in ("flow", "ph", "tds", "turb", "temp", "viol") if k in e}}
        elif src == "gw" and e["event"] == "PIPE_LEAK":
            where = e.get("where", "PIPE")
            opened.setdefault((where, "PIPE_LEAK"), {"n": where, "a": "PIPE_LEAK", "t": e["t"], "rx": e["t"],
                                                     "missing_l": e.get("missing_l"), "check": e.get("check", "night")})
        elif src == "gw" and e["event"] == "PIPE_LEAK_CLEARED":
            close((e.get("where", "PIPE"), "PIPE_LEAK"), t, "clean night check")
        elif src == "gw" and e["event"] == "NODE_SILENT" and e["node"] in nodes:
            opened[e["node"], "NODE_SILENT"] = {"n": e["node"], "a": "NODE_SILENT", "t": t, "rx": t}
        elif src == "gw" and e["event"] == "NODE_BACK":
            close((e["node"], "NODE_SILENT"), t, "heard again")
        elif src == "applied" and e["do"] == "fix":
            w = e["where"]
            keys = [(w, k) for k in FLAT_LEAKS | {"ABNORMAL_CONSUMPTION"}] if not w.startswith("B-D") else \
                [(w, "PIPE_LEAK"), ("PIPE", "PIPE_LEAK"), ("B-TANK", "ABNORMAL_SUPPLY")]
            for key in keys:
                close(key, t, "fixed")
    return sorted(opened.values(), key=lambda m: -m["rx"]), closed[::-1]


def score(p, now, flats):
    """Fix first: how urgent one open problem is, 0-100, and its level."""
    n = p["n"]
    if n == "B-TANK" or n == "PIPE":
        share = 1.0                                   # the tank feeds every flat
    elif n.startswith("B-D"):                         # this stretch and every floor below it
        f = "G" if n[3] == "0" else n[3]
        order = ["G", "1", "2", "3", "4"]
        share = sum(x[2] in order[:order.index(f) + 1] for x in flats) / max(len(flats), 1)
    else:
        share = 1 / max(len(flats), 1)
    size = min(20, (p.get("missing_l") or 0) / 10) + min(15, 100 * (p.get("flow") or 0))
    lasted = min(15, 1.5 * max(now - p["rx"], 0) / 3600)
    value = round(min(100, WEIGHTS.get(p["a"], 10) + 20 * share + size + lasted))
    return value, next(name for lo, name in LEVELS if value >= lo), round(share * len(flats))


def health(log, gw, open_p, closed_p, clock, nodes, running):
    """Numbers for the Building health tab."""
    now = clock["t"] if clock else 0
    flats = [n for n in nodes if n != "B-TANK"]
    ranked = sorted(({**p, "score": v, "level": lv, "flats": k} for p in open_p for v, lv, k in [score(p, now, flats)]),
                    key=lambda p: -p["score"])
    tank = [m for m in log if m["n"] == "B-TANK" and m["k"] == "SUM"]
    last = tank[-1] if tank else {}
    dp = {"ph": 2, "tds": 0, "turb": 1, "temp": 1}             # packets carry 4-byte decimals: 6.3984...
    water = {k: round(last[k], dp[k]) for k in dp if k in last}
    broken = [k for k, v in water.items() if not IS_10500[k][0] <= v <= IS_10500[k][1]]
    cause = next((p["a"] for p in open_p if p["a"] in ("QUALITY_RUNOFF", "QUALITY_CHEMICAL")), None)
    valve = [m for m in log if m["k"] == "ALERT" and m["a"] in ("VALVE_CLOSED", "VALVE_OPENED")]
    sums_by_flat = {}
    for m in log:
        if m["k"] == "SUM" and m["n"] in flats and m["i"] * 900 > now - 86400:
            sums_by_flat[m["n"]] = sums_by_flat.get(m["n"], 0) + m["vol_l"]
    delays = [m["rx"] - m["t"] for m in log]
    alert_delays = [m["rx"] - m["t"] for m in log if m["k"] == "ALERT"]
    balance = [[e["i"], e["lost"]] for e in gw if e["event"] == "BALANCE"][-96:]
    return {
        "status": ranked[0]["level"] if ranked else "Normal",
        "freshness": "Live" if running and not clock.get("paused") else "Paused" if running else "Stale" if clock else "No run",
        "fix_first": ranked,
        "water": water, "broken": broken, "cause": cause,
        "valve_open": valve[-1]["a"] == "VALVE_OPENED" if valve else True,
        "valve_since": valve[-1]["rx"] if valve else None,
        "water_series": [[m["t"], *(m.get(k) and round(m[k], 2) for k in dp)] for m in tank if "ph" in m][-96:],
        "tank_series": [[m["t"], m.get("level_m"), round(m.get("in_l", 0) / 900, 3)] for m in tank][-96:],
        "balance": balance,
        "usage": sorted(([n, round(v)] for n, v in sums_by_flat.items()), key=lambda x: -x[1]),
        "flat_problems": [p for p in open_p if p["n"] in flats and p["a"] != "NODE_SILENT"],
        "network": {"alerts_live": sum(d <= LIVE_S for d in alert_delays), "alerts_late": sum(d > LIVE_S for d in alert_delays),
                    "saved_sent_later": sum(d > LIVE_S for d in delays), "messages": len(delays),
                    "mean_delay_s": round(statistics.mean(delays)) if delays else None,
                    "max_delay_s": round(max(delays)) if delays else None},
        "past": closed_p[:30],
    }


def state(run=RUN, radio=False):
    """Everything the page shows, from the run's files. Status is what the gateway knows."""
    path = os.path.join(run, "clock.json")
    try:
        with open(path) as f:
            clock = json.load(f)
        running = time.time() - os.path.getmtime(path) < ALIVE_S
    except (OSError, ValueError):
        clock, running = None, False
    nodes, log = lines(run, "nodes.txt"), jsonl(run, "gateway_log.jsonl")
    gw, applied = jsonl(run, "gateway_events.jsonl"), jsonl(run, "events_applied.jsonl")

    # Status per node: "alert" while it has an open problem the gateway knows about (closed by the
    # node's recovery message or a fix), "silent" if not heard. The alarm banner lists the open problems.
    alerts = [m for m in log if m["k"] == "ALERT" and m["a"] not in CLEARS]
    open_p, closed_p = problems(log, gw, applied, nodes)
    status = {n: "ok" for n in nodes}
    for p in open_p[::-1]:
        if p["n"] in status:
            status[p["n"]] = "silent" if p["a"] == "NODE_SILENT" else "alert"
    for p in open_p:                          # an alert outranks silence
        if p["n"] in status and p["a"] != "NODE_SILENT":
            status[p["n"]] = "alert"
    pipe = [{"n": e.get("where", "PIPE"), "a": "PIPE_LEAK", "t": e["t"], "rx": e["t"], "missing_l": e.get("missing_l"),
             "check": e.get("check", "night")} for e in gw if e["event"] == "PIPE_LEAK"]
    active = [{k: p[k] for k in ("n", "a", "t", "rx", "missing_l", "check") if k in p} for p in open_p if p["a"] != "NODE_SILENT"]
    led = {e["n"]: e["led"] for e in jsonl(run, "leds.jsonl")}   # each node's own LED (red = buzzer on)

    # Every leak in this run, newest first: when it started, which check caught it, when the
    # gateway knew. A leak ends when it is fixed or its flat gets a new leak size ("replaced").
    leaks = []
    for i, e in enumerate(applied):
        if e["do"] != "leak":
            continue
        node = node_of(e["where"])
        end = next((f for f in applied[i + 1:] if f["do"] in ("leak", "fix") and f["where"] == e["where"]), None)
        until = end["t"] if end else float("inf")
        if e["where"].startswith("B-D"):   # down pipe: the tank node finds bursts, the night check smaller leaks
            found_by = [m for m in alerts if m["n"] == node and m["a"] == "ABNORMAL_SUPPLY"] + \
                       [m for m in pipe if m["n"] in ("PIPE", e["where"])]
        else:
            found_by = [m for m in alerts if m["n"] == node]
        hit = min((m for m in found_by if e["t"] <= m["t"] <= until), key=lambda m: m["t"], default=None)
        leaks.append({"where": e["where"], "node": node, "size": e["size"], "start": e["t"],
                      "fixed": end["t"] if end and end["do"] == "fix" else None,
                      "replaced": end["t"] if end and end["do"] == "leak" else None,
                      "alert": hit and {k: hit[k] for k in ("a", "t", "rx")}})
    now = clock["t"] if clock else 0
    later = [c for c in jsonl(run, "commands.jsonl") if c.get("do") == "leak" and c.get("at_s", 0) > now]

    tank = [m for m in log if m["n"] == "B-TANK"]
    levels = [[m["t"], m["level_m"]] for m in tank if m["k"] == "SUM" and "level_m" in m]
    qalerts = [m for m in tank if m["k"] == "ALERT" and m["a"].startswith("QUALITY")]
    # the worst open water problem (the limit check outranks the cause guess), else the latest word
    qopen = [p["a"] for p in open_p if p["a"] in QUALITY_KINDS]
    quality = min(qopen, key=lambda a: -WEIGHTS[a]) if qopen else "QUALITY_CLEARED"
    # Water quality card: the tank node only sends readings with a bad-water alert (it judges the
    # water itself), so show the last bad reading, and the latest runoff/chemical test with when it was caught
    bad = [m for m in qalerts if m["a"] != "QUALITY_CLEARED"]
    qtests = [e for e in applied if e["do"] == "quality"]
    qtest = qtests[-1] if qtests else None
    if qtest:
        hit = next((m for m in bad if m["a"] in ("QUALITY_" + qtest["kind"].upper(), "QUALITY_LIMIT_VIOLATION")
                    and m["t"] >= qtest["t"]), None)
        clean = hit and next((m for m in qalerts if m["a"] == "QUALITY_CLEARED" and m["t"] > hit["t"]), None)
        qtest = {"kind": qtest["kind"], "start": qtest["t"], "end": qtest["t"] + 60 * qtest["minutes"],
                 "alert": hit and {k: hit[k] for k in ("a", "t", "rx")}, "clean": clean and clean["rx"]}
    feed = [e for e in gw if e["event"] not in ("BALANCE_PAUSED", "BALANCE")][-40:][::-1]

    out = {"running": running, "clock": clock, "nodes": nodes, "sites": lines(run, "leak_sites.txt"),
           "status": status, "active": active, "led": led, "leaks": leaks[::-1], "later": later, "feed": feed, "levels": levels[-96:], "quality": quality,
           "quality_since": qalerts[-1]["rx"] if qalerts else None,
           "quality_bad": bad and {k: bad[-1].get(k) for k in ("a", "ph", "tds", "temp", "t", "rx")}, "quality_test": qtest,
           "messages": len(log), "health": health(log, gw, open_p, closed_p, clock, nodes, running)}
    out["valve_open"] = out["health"]["valve_open"]
    if radio:
        out["radio"] = radio_stats(run, log)
    return out


def radio_stats(run, log):
    """Radio numbers for the second tab. ponytail: re-reads the whole tx_log.csv each poll (about
    0.1 s for two weeks of traffic); keep a read offset if runs get much longer."""
    try:
        with open(os.path.join(run, "tx_log.csv")) as f:
            tx = list(csv.DictReader(f))
    except OSError:
        tx = []
    up = [r for r in tx if r["dst"] == "GATEWAY"]
    down = [r for r in tx if r["src"] == "GATEWAY"]
    count = lambda rows: {k: sum(r["result"].strip() == k for r in rows)
                          for k in ("ok", "collision", "half_duplex", "weak_link", "radio_cut")}
    per = {}
    for r in up:
        if r["result"].strip() != "radio_cut":
            ok, n = per.get(r["src"], (0, 0))
            per[r["src"]] = (ok + (r["result"].strip() == "ok"), n + 1)
    delays = [m["rx"] - m["t"] for m in log]
    return {"up": count(up), "down": count(down), "waits": sum(int(r["backoffs"] or 0) > 0 for r in up),
            "per_node": {n: round(ok / k, 4) for n, (ok, k) in sorted(per.items())},
            "median_delay_s": statistics.median(delays) if delays else None,
            "resends": sum(r["kind"] != "GOT" for r in up) - len({(r["src"], r["seq"]) for r in up if r["kind"] != "GOT"}),
            # last time each node got something from the gateway (a beacon or an ACK): "gateway heard"
            "heard": {r["dst"]: float(r["t_s"]) for r in down if r["result"].strip() == "ok" and r["kind"] in ("BEACON", "ACK")}}


def valid(c, sites, nodes):
    """Commands from the page: only known kinds, places and sensible numbers reach the timekeeper."""
    num = lambda k, lo, hi: type(c.get(k)) in (int, float) and lo <= c[k] <= hi
    when = lambda: "at_s" not in c or num("at_s", 0, 1e8)
    rules = {"leak": lambda: c.get("where") in sites and (c.get("size") in SIZES or num("size", 0.001, 5)),
             "fix": lambda: c.get("where") in sites,
             "quality": lambda: c.get("kind") in ("runoff", "chemical", "hot") and num("minutes", 1, 600),
             "radio_cut": lambda: (c.get("who") == "all" or c.get("who") in nodes) and num("minutes", 1, 600),
             "speed": lambda: num("delay_s", 0, 60),
             "until_alert": lambda: c.get("node") in nodes and num("from_s", 0, 1e8),
             "valve_reopen": lambda: True,
             "pause": lambda: True, "resume": lambda: True, "stop": lambda: True}
    return isinstance(c, dict) and c.get("do") in rules and rules[c["do"]]() and when()


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body, kind="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/api/state":
            return self.reply(200, state(radio="radio=1" in query))
        if path in STATIC:
            name, kind = STATIC[path]
            with open(os.path.join(HERE, name), "rb") as f:
                return self.reply(200, f.read(), kind)
        self.reply(404, {"error": "not found"})

    def start(self):
        """Start a run unless the one we started is still alive. clock.json only appears a few
        seconds into a run, so without this a double click started two runs, and the second wiped
        the first one's files while it kept writing to them."""
        global PROC
        with START_LOCK:
            if PROC and PROC.poll() is None:
                return False
            PROC = subprocess.Popen(DEMO, cwd=os.path.dirname(HERE), stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
            return True

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 10_000)) or b"{}")
        except ValueError:
            return self.reply(400, {"error": "not JSON"})
        s = state()
        if self.path == "/api/start":
            if s["running"]:
                return self.reply(409, {"error": "a simulation is already running"})
            if not self.start():
                return self.reply(409, {"error": "a simulation is already starting"})
            return self.reply(200, {"started": True})
        if self.path == "/api/reset":
            if s["running"]:
                with open(os.path.join(RUN, "commands.jsonl"), "a") as f:
                    f.write(json.dumps({"do": "stop"}) + "\n")
                deadline = time.time() + 20
                while state()["running"] and time.time() < deadline:   # clock.json goes quiet once it stops
                    time.sleep(0.5)
                if state()["running"]:
                    return self.reply(500, {"error": "the old run did not stop; stop it in its terminal"})
                time.sleep(1)                    # nodes exit just after the timekeeper
            if PROC and PROC.poll() is None:
                try:
                    PROC.wait(20)                # the run we started: wait for all of it to end
                except subprocess.TimeoutExpired:
                    return self.reply(500, {"error": "the old run did not stop"})
            if not self.start():
                return self.reply(409, {"error": "a simulation is already starting"})
            return self.reply(200, {"started": True})
        if self.path == "/api/command":
            if not s["running"]:
                return self.reply(409, {"error": "no simulation running"})
            if not valid(body, s["sites"], s["nodes"]):
                return self.reply(400, {"error": f"command not allowed: {body}"})
            with open(os.path.join(RUN, "commands.jsonl"), "a") as f:
                f.write(json.dumps(body) + "\n")
            return self.reply(200, {"sent": body})
        self.reply(404, {"error": "not found"})

    def log_message(self, *args):
        pass                                     # one line per poll would flood the terminal


def check():
    run = tempfile.mkdtemp()
    w = lambda name, text: open(os.path.join(run, name), "w").write(text)
    w("nodes.txt", "B-203\nB-204\nB-TANK\n")
    w("leak_sites.txt", "B-203\nB-204\nB-D1\n")
    w("clock.json", json.dumps({"t": 97200, "paused": True, "delay_s": 0, "leaks": {"B-203": 0.05},
                                "quality": "normal", "watching": None}))
    w("events_applied.jsonl", json.dumps({"t": 93600, "do": "leak", "where": "B-203", "size": 0.05}) + "\n")
    w("gateway_log.jsonl", "".join(json.dumps(m) + "\n" for m in [
        {"k": "SUM", "n": "B-TANK", "s": 1, "t": 93600, "i": 104, "vol_l": 300, "in_l": 0, "level_m": 1.42, "rx": 93660},
        {"k": "ALERT", "n": "B-203", "s": 9, "t": 95400, "a": "LEAK_NIGHT_FLOW", "flow": 0.05, "rx": 95460}]))
    w("gateway_events.jsonl", json.dumps({"t": 96000, "event": "NODE_SILENT", "node": "B-204", "last_heard_min": 45}) + "\n")
    s = state(run)
    assert s["running"] and s["status"] == {"B-203": "alert", "B-204": "silent", "B-TANK": "ok"}, s["status"]
    assert s["leaks"][0]["alert"] == {"a": "LEAK_NIGHT_FLOW", "t": 95400, "rx": 95460}, s["leaks"]
    assert s["levels"] == [[93600, 1.42]] and s["quality"] == "QUALITY_CLEARED"
    assert s["active"] == [{"n": "B-203", "a": "LEAK_NIGHT_FLOW", "t": 95400, "rx": 95460}]
    w("leds.jsonl", '{"t": 95400, "n": "B-203", "led": "red"}\n{"t": 95400, "n": "B-204", "led": "yellow"}\n')
    assert state(run)["led"] == {"B-203": "red", "B-204": "yellow"}
    # Fixing the leak turns the flat calm again and closes the test
    with open(os.path.join(run, "events_applied.jsonl"), "a") as f:
        f.write(json.dumps({"t": 97000, "do": "fix", "where": "B-203"}) + "\n")
    s = state(run)
    assert s["status"]["B-203"] == "ok" and s["leaks"][0]["fixed"] == 97000 and s["active"] == []
    # A second leak elsewhere, one scheduled for later, and a new size for an open leak
    with open(os.path.join(run, "events_applied.jsonl"), "a") as f:
        f.write(json.dumps({"t": 97100, "do": "leak", "where": "B-204", "size": "small"}) + "\n")
        f.write(json.dumps({"t": 97150, "do": "leak", "where": "B-204", "size": 0.8}) + "\n")
    w("commands.jsonl", json.dumps({"do": "leak", "where": "B-203", "size": 0.3, "at_s": 99000}) + "\n")
    s = state(run)
    assert [(x["where"], x["size"], x["replaced"]) for x in s["leaks"]] == [("B-204", 0.8, None), ("B-204", "small", 97150), ("B-203", 0.05, None)], s["leaks"]
    assert s["leaks"][2]["alert"]["a"] == "LEAK_NIGHT_FLOW" and [c["at_s"] for c in s["later"]] == [99000]
    # A small leak on the down pipe: a tank quality alert does not count, the gateway's night check does
    with open(os.path.join(run, "events_applied.jsonl"), "a") as f:
        f.write(json.dumps({"t": 97200, "do": "leak", "where": "B-D1", "size": "small"}) + "\n")
    with open(os.path.join(run, "gateway_log.jsonl"), "a") as f:
        f.write(json.dumps({"k": "ALERT", "n": "B-TANK", "s": 9, "t": 98000, "a": "QUALITY_RUNOFF", "rx": 98060}) + "\n")
    with open(os.path.join(run, "gateway_events.jsonl"), "a") as f:
        f.write(json.dumps({"t": 99900, "event": "PIPE_LEAK", "where": "B-D3", "missing_l": 45, "lps": 0.05, "check": "fast"}) + "\n")
        f.write(json.dumps({"t": 100860, "event": "PIPE_LEAK", "where": "B-D1", "missing_l": 360, "lps": 0.05, "check": "night"}) + "\n")
    s = state(run)          # the floor 3 alarm is not credited to the floor 1 leak, but shows in the banner
    assert s["leaks"][0]["where"] == "B-D1" and s["leaks"][0]["alert"] == {"a": "PIPE_LEAK", "t": 100860, "rx": 100860}, s["leaks"][0]
    assert s["active"][0] == {"n": "B-D1", "a": "PIPE_LEAK", "t": 100860, "rx": 100860, "missing_l": 360, "check": "night"}, s["active"]
    assert [m["n"] for m in s["active"]].count("B-D3") == 1
    with open(os.path.join(run, "events_applied.jsonl"), "a") as f:
        f.write(json.dumps({"t": 101000, "do": "fix", "where": "B-D1"}) + "\n")
    s = state(run)
    assert [m["n"] for m in s["active"] if m["n"] in ("B-D1", "B-D3", "B-TANK")] == ["B-D3", "B-TANK"], s["active"]   # only floor 1 fixed
    # Water quality card: a runoff test, caught by the tank node, then reported clean again
    with open(os.path.join(run, "events_applied.jsonl"), "a") as f:
        f.write(json.dumps({"t": 102000, "do": "quality", "kind": "runoff", "minutes": 60}) + "\n")
    with open(os.path.join(run, "gateway_log.jsonl"), "a") as f:
        f.write(json.dumps({"k": "ALERT", "n": "B-TANK", "s": 10, "t": 102180, "a": "QUALITY_RUNOFF", "ph": 6.4, "tds": 52, "rx": 102240}) + "\n")
    s = state(run)
    assert s["quality_test"] == {"kind": "runoff", "start": 102000, "end": 105600, "alert": {"a": "QUALITY_RUNOFF", "t": 102180, "rx": 102240}, "clean": None}, s["quality_test"]
    assert s["quality_bad"] == {"a": "QUALITY_RUNOFF", "ph": 6.4, "tds": 52, "temp": None, "t": 102180, "rx": 102240} and s["quality_since"] == 102240
    with open(os.path.join(run, "gateway_log.jsonl"), "a") as f:
        f.write(json.dumps({"k": "ALERT", "n": "B-TANK", "s": 11, "t": 106200, "a": "QUALITY_CLEARED", "rx": 106260}) + "\n")
    s = state(run)
    assert s["quality"] == "QUALITY_CLEARED" and s["quality_test"]["clean"] == 106260 and not any(m["n"] == "B-TANK" for m in s["active"])
    # Building health: tank valve shut by runoff (limit + cause), a pipe leak and a flat leak open:
    # Fix first ranks the water first; the valve reopens, the flat reports its leak over
    add = lambda name, rows: open(os.path.join(run, name), "a").write("".join(json.dumps(r) + "\n" for r in rows))
    add("gateway_log.jsonl", [
        {"k": "ALERT", "n": "B-TANK", "s": 12, "t": 110000, "a": "QUALITY_LIMIT_VIOLATION", "ph": 6.2, "tds": 60, "turb": 20, "viol": 5, "rx": 110060},
        {"k": "ALERT", "n": "B-TANK", "s": 13, "t": 110000, "a": "QUALITY_RUNOFF", "rx": 110060},
        {"k": "ALERT", "n": "B-TANK", "s": 14, "t": 110000, "a": "VALVE_CLOSED", "valve": 0, "rx": 110060},
        {"k": "SUM", "n": "B-TANK", "s": 15, "t": 110700, "i": 123, "vol_l": 0, "in_l": 900, "level_m": 1.6, "valve": 0,
         "ph": 6.3, "tds": 70, "turb": 18, "temp": 26, "rx": 110760},
        {"k": "SUM", "n": "B-203", "s": 16, "t": 110700, "i": 123, "vol_l": 40, "p_min": 9, "rx": 110760},
        {"k": "ALERT", "n": "B-203", "s": 17, "t": 110000, "a": "LEAK_NIGHT_FLOW", "flow": 0.05, "rx": 110100}])
    add("gateway_events.jsonl", [{"t": 110100, "event": "BALANCE", "i": 122, "lost": {"2": 1.0, "1": 0.5}}])
    s = state(run)
    h = s["health"]
    order = [p["a"] for p in h["fix_first"]]
    assert order[0] == "QUALITY_LIMIT_VIOLATION" and h["status"] == "Critical", order
    assert order.index("QUALITY_RUNOFF") < order.index("LEAK_NIGHT_FLOW") < order.index("NODE_SILENT"), order
    assert not h["valve_open"] and not s["valve_open"] and h["cause"] == "QUALITY_RUNOFF" and h["broken"] == ["ph", "turb"], h
    assert s["quality"] == "QUALITY_LIMIT_VIOLATION" and s["status"]["B-TANK"] == "alert"
    assert h["balance"] == [[122, {"2": 1.0, "1": 0.5}]] and not any(e["event"] == "BALANCE" for e in s["feed"])
    assert h["usage"] == [["B-203", 40]] and h["tank_series"][-1] == [110700, 1.6, 1.0]
    add("gateway_log.jsonl", [
        {"k": "ALERT", "n": "B-TANK", "s": 18, "t": 111000, "a": "QUALITY_CLEARED", "rx": 111060},
        {"k": "ALERT", "n": "B-TANK", "s": 19, "t": 111500, "a": "VALVE_OPENED", "valve": 1, "rx": 111560},
        {"k": "ALERT", "n": "B-203", "s": 20, "t": 111600, "a": "FLAT_LEAK_CLEARED", "rx": 111660}])
    s = state(run)
    h = s["health"]
    assert h["valve_open"] and s["status"]["B-203"] == "ok" and s["quality"] == "QUALITY_CLEARED"
    assert [(p["a"], p["why"]) for p in h["past"][:2]] == [("LEAK_NIGHT_FLOW", "water seen off for 10 min"),
                                                            ("VALVE_CLOSED", "valve reopened after a water test")], h["past"][:2]
    assert [p["a"] for p in h["fix_first"]] == ["PIPE_LEAK", "NODE_SILENT"], h["fix_first"]   # floor 3 pipe, silent B-204 left
    assert valid({"do": "valve_reopen"}, s["sites"], s["nodes"])
    # Only sensible commands get through
    sites, nodes = s["sites"], s["nodes"]
    good = [{"do": "leak", "where": "B-203", "size": 0.3, "at_s": 100000}, {"do": "leak", "where": "B-D1", "size": "large"},
            {"do": "until_alert", "node": "B-203", "from_s": 93600}, {"do": "quality", "kind": "runoff", "minutes": 60},
            {"do": "pause"}]
    bad = [{"do": "leak", "where": "B-999", "size": 0.3}, {"do": "leak", "where": "B-203", "size": 50},
           {"do": "leak", "where": "B-203", "size": True}, {"do": "format_disk"}, ["leak"],
           {"do": "radio_cut", "who": "everyone", "minutes": 60}]
    assert all(valid(c, sites, nodes) for c in good), [c for c in good if not valid(c, sites, nodes)]
    assert not any(valid(c, sites, nodes) for c in bad), [c for c in bad if valid(c, sites, nodes)]
    print("dashboard self-check passed")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--host", default="127.0.0.1", help="0.0.0.0 inside Docker")
    p.add_argument("--check", action="store_true")
    a = p.parse_args()
    if a.check:
        sys.exit(check())
    print(f"Aqua Edge dashboard on http://localhost:{a.port}  (Ctrl+C to stop)")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
