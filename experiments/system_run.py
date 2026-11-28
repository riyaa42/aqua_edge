"""Experiments 1, 2, 3 and 5: the four-node setup as real programs on this computer.

    uv run python -m experiments.system_run baseline        experiments/runs/baseline
    uv run python -m experiments.system_run node-outage     experiments/runs/node-outage
    uv run python -m experiments.system_run dashboard-outage experiments/runs/dashboard-outage
    uv run python -m experiments.system_run realtime        experiments/runs/realtime

The dashboard server, the edge-based gateway and the four nodes of gateway/virtual_4node.json run as
separate processes. The nodes replay day 2 of sensors.csv (day 1 is learned first) with valves only
simulated. Their packets go to the gateway through a UDP relay here, which counts them and can drop
all of them, both ways (the node-to-gateway outage). The dashboard outage stops the server's process.
Needs Linux (/proc). Not a radio: no airtime, range, collisions or random loss.
"""
import argparse
import json
import os
import selectors
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

from hal.packet import peek, sim_key

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODES = ("B-TANK", "B-101", "B-203", "B-301")
DAY = 86400
TOKEN = "experiment-gateway-token"      # this computer only, thrown away with the run
TICK = os.sysconf("SC_CLK_TCK")


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Relay(threading.Thread):
    """Nodes send here; each gets its own socket towards the gateway, so replies find their way back."""

    def __init__(self, gateway_port, log_path):
        super().__init__(daemon=True)
        self.gateway = ("127.0.0.1", gateway_port)
        self.front = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.front.bind(("127.0.0.1", 0))
        self.port = self.front.getsockname()[1]
        self.back, self.node_of, self.cut, self.stopping = {}, {}, False, False
        self.log = open(log_path, "w", buffering=1)
        self.select = selectors.DefaultSelector()
        self.select.register(self.front, selectors.EVENT_READ, None)

    def note(self, direction, data, node):
        m = peek(data) if data else None
        if m:       # empty datagrams only say "a node is here": not radio packets
            self.log.write(json.dumps({"w": time.time(), "dir": direction, "k": m["k"], "n": m["n"], "s": m.get("s"),
                                       "node": node, "bytes": len(data), "dropped": self.cut}) + "\n")

    def run(self):
        while not self.stopping:
            for key, _ in self.select.select(0.2):
                try:
                    data, address = key.fileobj.recvfrom(2048)
                except OSError:
                    continue
                if key.data is None:                            # from a node
                    if address not in self.back:
                        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        s.bind(("127.0.0.1", 0))
                        self.back[address] = s
                        self.select.register(s, selectors.EVENT_READ, address)
                    m = peek(data) if data else None
                    if m:
                        self.node_of[address] = m["n"]
                    self.note("up", data, self.node_of.get(address))
                    if not self.cut:
                        self.back[address].sendto(data, self.gateway)
                else:                                           # from the gateway, for one node
                    self.note("down", data, self.node_of.get(key.data))
                    if not self.cut:
                        self.front.sendto(data, key.data)


def proc_usage(pid):
    """(CPU seconds so far, resident MB, proportional MB) of one process."""
    with open(f"/proc/{pid}/stat") as f:
        fields = f.read().rsplit(")", 1)[1].split()
    cpu = (int(fields[11]) + int(fields[12])) / TICK
    sizes = {}
    with open(f"/proc/{pid}/smaps_rollup") as f:
        for line in f:
            parts = line.split()
            if parts and parts[0] in ("Rss:", "Pss:"):
                sizes[parts[0]] = int(parts[1]) / 1024
    return cpu, sizes.get("Rss:", 0.0), sizes.get("Pss:", 0.0)


def lines(path):
    try:
        with open(path, "rb") as f:
            return f.read().count(b"\n")
    except OSError:
        return 0


def read_jsonl(path):
    out = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


class Run:
    def __init__(self, out, step_s):
        self.out, self.step_s = os.path.abspath(out), step_s
        os.makedirs(self.out, exist_ok=True)
        for name in os.listdir(self.out):
            os.remove(os.path.join(self.out, name))
        self.work = tempfile.mkdtemp(prefix="aqua-exp-")
        self.keys = os.path.join(self.work, "keys.json")
        with open(self.keys, "w") as f:
            json.dump({n: sim_key(n).hex() for n in ("GATEWAY", *NODES)}, f)
        self.tree = os.path.join(ROOT, "gateway", "virtual_4node.json")
        self.http, gateway_port = free_port(), free_port(socket.SOCK_DGRAM)
        self.gateway_port = gateway_port
        self.db = os.path.join(self.work, "dashboard.db")
        self.env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
                    "AQUA_DASHBOARD_TOKEN": TOKEN, "AQUA_DASHBOARD_DB": self.db, "AQUA_TOPOLOGY_PATH": self.tree,
                    "AQUA_ENABLE_OUTAGE_SIM": "0"}
        self.relay = Relay(gateway_port, self.path("relay.jsonl"))
        self.procs, self.marks, self.samples = {}, [], []

    def path(self, name):
        return os.path.join(self.out, name)

    def mark(self, what, **fields):
        self.marks.append({"w": time.time(), "what": what, **fields})
        print(f"[{time.strftime('%H:%M:%S')}] {what} {fields or ''}", flush=True)

    def spawn(self, name, *args):
        self.procs[name] = subprocess.Popen([sys.executable, *args], cwd=ROOT, env=self.env,
                                            stdout=open(self.path(f"{name}.log"), "a"), stderr=subprocess.STDOUT)

    def start_dashboard(self):
        self.spawn("dashboard", "-m", "uvicorn", "--factory", "dashboard.server:create_app", "--host", "127.0.0.1",
                   "--port", str(self.http), "--timeout-graceful-shutdown", "3")

    def healthy(self):
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{self.http}/api/v1/health", timeout=1) as r:
                return r.status == 200
        except OSError:
            return False

    def start(self):
        self.relay.start()
        self.start_dashboard()
        end = time.time() + 30
        while not self.healthy():
            assert time.time() < end, "the dashboard did not start"
            time.sleep(0.2)
        self.spawn("gateway", "-m", "experiments.probe", self.path("probe-gateway.jsonl"), "gateway.edge_based",
                   "--keys", self.keys, "--data", os.path.join(self.work, "gateway"), "--tree", self.tree,
                   "--port", "none", "--udp-listen", f"127.0.0.1:{self.gateway_port}",
                   "--dashboard-url", f"http://127.0.0.1:{self.http}/api/v1/ingest", "--dashboard-id", "experiment")
        time.sleep(2)
        for node in NODES:
            self.spawn(node, "-m", "experiments.probe", self.path(f"probe-{node}.jsonl"), "edge.edge_based", node,
                       "--tree", self.tree, "--keys", self.keys, "--sd", os.path.join(self.work, node),
                       "--radio", "udp", "--gateway", f"127.0.0.1:{self.relay.port}", "--actuators", "sim",
                       "--prime-s", str(DAY), "--step-s", str(self.step_s))
        self.mark("started", step_s=self.step_s)

    # --- what can be seen from outside ---
    def node_queue(self, node):
        return max(0, lines(os.path.join(self.work, node, f"{node}.jsonl")) - 1)   # first line: the counter

    def outbox(self):
        return lines(os.path.join(self.work, "gateway", "dashboard-outbox", "outbox.jsonl"))

    def sample(self):
        row = {"w": time.time(), "outbox": self.outbox(), "queues": {n: self.node_queue(n) for n in NODES},
               "usage": {}}
        for name, p in self.procs.items():
            if p.poll() is None:
                try:
                    cpu, rss, pss = proc_usage(p.pid)
                    row["usage"][name] = {"cpu_s": cpu, "rss_mb": rss, "pss_mb": pss, "pid": p.pid}
                except OSError:
                    pass
        self.samples.append(row)

    def data_time(self, node="B-TANK"):
        """Seconds into day 2 of the node's newest reading, or -1."""
        try:
            with open(self.path(f"probe-{node}.jsonl"), "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 20000))
                tail = f.read().decode(errors="ignore").splitlines()
        except OSError:
            return -1
        for line in reversed(tail):
            if '"e": "step"' in line:
                try:
                    return json.loads(line)["sample"] - DAY
                except ValueError:
                    continue
        return -1

    def finished(self, node):
        try:
            with open(self.path(f"{node}.log")) as f:
                return "end of the sensor data" in f.read()
        except OSError:
            return False

    def wait(self, condition, what, limit_s):
        end = time.time() + limit_s
        while not condition():
            self.sample()
            for name, p in self.procs.items():
                if p.poll() is not None and name not in self.stopped:
                    raise AssertionError(f"{name} stopped while waiting for {what}: see {self.path(name + '.log')}")
            if time.time() > end:
                raise AssertionError(f"waited too long for {what}")
            time.sleep(1)

    stopped = frozenset()

    def sleep(self, seconds):
        end = time.time() + seconds
        self.wait(lambda: time.time() >= end, f"{seconds} s", seconds + 60)

    def stop(self, name, sig=signal.SIGTERM):
        p = self.procs[name]
        if p.poll() is None:
            p.send_signal(sig)
            try:
                p.wait(15)
            except subprocess.TimeoutExpired:
                p.kill()
        return p.returncode

    def drain(self, limit_s=420):
        """Wait until no node and no dashboard outbox has anything left."""
        self.wait(lambda: not any(self.node_queue(n) for n in NODES), "the nodes' queues to empty", limit_s)
        self.mark("node queues empty")
        # one more periodic snapshot (every 60 s) so the dashboard has the final state
        self.sleep(65)
        self.wait(lambda: self.outbox() == 0, "the dashboard outbox to empty", 120)
        self.mark("outbox empty")

    def save(self, scenario, extra):
        frames = []
        if os.path.exists(self.db):
            con = sqlite3.connect(self.db)
            for row in con.execute("SELECT seq, created_at_utc, received_at_utc, buffered, snapshot_json, "
                                   "new_events_json FROM frames ORDER BY seq"):
                snap = json.loads(row[4])
                frames.append({"seq": row[0], "created": row[1], "received": row[2], "buffered": row[3],
                               "sums": {n: (d.get("last_sum") or {}).get("i") for n, d in snap.get("nodes", {}).items()},
                               "valves": {n: d.get("valve") for n, d in snap.get("nodes", {}).items()},
                               "events": [e.get("event") for e in json.loads(row[5])]})
            con.close()
        with open(self.path("frames.json"), "w") as f:
            json.dump(frames, f)
        with open(self.path("samples.json"), "w") as f:
            json.dump(self.samples, f)
        with open(self.path("run.json"), "w") as f:
            json.dump({"scenario": scenario, "step_s": self.step_s, "nodes": list(NODES), "marks": self.marks,
                       "cpus": os.cpu_count(), **extra}, f, indent=1)

    def close(self):
        for name in list(self.procs):
            self.stop(name)
        self.relay.stopping = True


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("scenario", choices=["baseline", "node-outage", "dashboard-outage", "realtime"])
    ap.add_argument("out")
    ap.add_argument("--step-s", type=float, help="seconds per data minute (default 0.5; realtime: 60)")
    ap.add_argument("--outage-s", type=float, default=300)
    ap.add_argument("--outage-at-h", type=float, default=1.0, help="data time (day 2) at which the outage starts")
    ap.add_argument("--realtime-s", type=float, default=600)
    a = ap.parse_args()
    run = Run(a.out, a.step_s or (60.0 if a.scenario == "realtime" else 0.5))
    extra = {}
    try:
        run.start()
        run.wait(lambda: all(run.data_time(n) >= 0 for n in NODES), "every node to start on day 2", 180)
        run.mark("all nodes on day 2")
        if a.scenario == "realtime":
            run.sleep(a.realtime_s)
        else:
            if a.scenario != "baseline":
                run.wait(lambda: run.data_time() >= a.outage_at_h * 3600, "the outage time", 3600)
                if a.scenario == "node-outage":
                    run.relay.cut = True
                else:
                    run.stopped = frozenset({"dashboard"})
                    run.stop("dashboard")
                run.mark("outage starts", data_s=run.data_time())
                run.sleep(a.outage_s)
                if a.scenario == "node-outage":
                    run.relay.cut = False
                    run.mark("outage ends", data_s=run.data_time())
                else:
                    run.mark("outage ends", data_s=run.data_time())
                    run.start_dashboard()
                    run.wait(run.healthy, "the dashboard to come back", 60)
                    run.stopped = frozenset()
                    run.mark("dashboard healthy again")
                run.wait(lambda: (run.node_queue(NODES[0]) + sum(map(run.node_queue, NODES[1:])) == 0)
                         if a.scenario == "node-outage" else run.outbox() == 0, "the backlog to drain", 600)
                run.mark("backlog drained")
            run.wait(lambda: all(map(run.finished, NODES)), "the end of the data", 1440 * run.step_s + 900)
            run.mark("replay finished")
            run.drain()
        extra["exit_codes"] = {n: run.stop(n) for n in (*NODES, "gateway", "dashboard")}
    finally:
        run.close()
        run.save(a.scenario, extra)
    print("done", run.out)


if __name__ == "__main__":
    main()
