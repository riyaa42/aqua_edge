"""gateway/dashboard_link.py with a real gateway, the real shipper and the real dashboard server on
this computer, and the gateway program started with --dashboard-url.

    python3 -m tests.test_dashboard_link
"""
import json
import os
import signal
import socket
import tempfile
import threading
import time
import urllib.request

import uvicorn

import gateway.edge_based as server
from dashboard.server import create_app
from dashboard.shipper import DashboardShipper, OutboxStorageError
from gateway.core import Gateway
from gateway.dashboard_link import EVENT_GAP_S, WAITING_FRAMES, DashboardFeed
from gateway.topology import Topology
from hal.packet import NAMES, pack, sim_key

F1 = "B-101"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Dashboard:
    def __init__(self, folder, port):
        app = create_app(db_path=os.path.join(folder, "dashboard.db"), auth_token="secret")
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        wait(lambda: self.server.started, "dashboard start")
        self.base = f"http://127.0.0.1:{port}/api/v1"

    def get(self, path):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(self.base + path, timeout=5) as r:
            return json.load(r)

    def latest(self):
        return self.get("/snapshots/latest?gateway_id=test")["frame"]

    def close(self):
        self.server.should_exit = True
        self.thread.join(5)


def wait(test, what, seconds=10):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if test():
            return
        time.sleep(0.02)
    raise AssertionError(f"waited too long for: {what}")


class Hal:
    storage_path = None
    key = staticmethod(sim_key)

    def __init__(self):
        self.box = []

    def now(self):
        return time.time()

    def receive(self):
        got, self.box = self.box, []
        return got

    def send(self, raw, cad=False):
        pass


def alert(gw, seq, name="LEAK_NIGHT_FLOW"):
    gw.hal.box.append(pack({"k": "ALERT", "n": F1, "s": seq, "t": time.time(), "a": name}, sim_key(F1)))


def shipper(folder, port, **options):
    return DashboardShipper(f"http://127.0.0.1:{port}/api/v1/ingest", folder, gateway_id="test", auth_token="secret",
                            min_free_bytes=0, base_backoff_s=0.2, max_backoff_s=0.2, **options)


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def check_delivery_outage_and_restart():
    folder, port, lines = tempfile.mkdtemp(), free_port(), []
    gw, clock = Gateway(Hal(), Topology.load()), Clock()
    ship = shipper(os.path.join(folder, "outbox"), port)
    feed = DashboardFeed(gw, ship, log=lines.append, clock=clock).start()
    # the dashboard is not there yet: frames wait on disk, said once
    alert(gw, 1)
    gw.step()
    wait(lambda: ship.queue and ship.connected is False, "first frame queued")
    for _ in range(3):
        clock.t += 61
        gw.step()
    wait(lambda: len(ship.queue) == 4, "frames kept")
    assert len([line for line in lines if "not reachable" in line]) == 1, lines
    # the program restarts while it is still away: what was waiting is still there, plus a last frame
    alert(gw, 2, "FLAT_LEAK_CLEARED")
    gw.step()
    feed.stop()
    ship = shipper(os.path.join(folder, "outbox"), port)
    assert len(ship.queue) >= 5 and any(r["new_events"] for r in ship.queue)
    waiting = len(ship.queue)
    dashboard = Dashboard(folder, port)
    gw = Gateway(Hal(), Topology.load())
    clock = Clock()
    feed = DashboardFeed(gw, ship, log=lines.append, clock=clock).start()
    try:
        gw.step()
        wait(lambda: not ship.queue and ship.delivered_frames >= waiting + 1, "backlog delivered")
        assert dashboard.get("/gateways")["gateways"]
        alert(gw, 3, "LEAK_CONTINUOUS_FLOW")
        clock.t += EVENT_GAP_S + 0.1                        # a decision does not wait a whole minute
        gw.step()
        wait(lambda: any(i["target"] == F1 for i in dashboard.get("/incidents?gateway_id=test")["incidents"]),
             "incident on the dashboard")
        frame = dashboard.latest()
        assert frame["mode"] == "edge" and set(frame["snapshot"]["nodes"]) == set(NAMES[1:])
    finally:
        feed.stop()
        dashboard.close()
    assert not feed.thread.is_alive()


def check_a_stuck_dashboard_never_delays_the_gateway():
    with socket.socket() as stuck:                  # takes the connection and never answers
        stuck.bind(("127.0.0.1", 0))
        stuck.listen(5)
        gw = Gateway(Hal(), Topology.load())
        ship = shipper(tempfile.mkdtemp(), stuck.getsockname()[1], timeout_s=2.0)
        feed = DashboardFeed(gw, ship, log=lambda line: None, interval_s=0).start()
        gw.step()
        time.sleep(0.5)                             # the sender is now waiting for an answer
        slowest = 0.0
        for seq in range(1, 60):
            alert(gw, seq)
            start = time.monotonic()
            gw.step()
            slowest = max(slowest, time.monotonic() - start)
        assert slowest < 0.25, f"a step took {slowest:.2f} s"
        feed.stop()
        assert not feed.thread.is_alive() and feed.inbox.empty() and len(ship.queue) >= 50


def check_pacing_copies_and_storage_errors():
    class Shipper:
        queue, connected, last_error, timeout_s = [], None, None, 1

        def __init__(self):
            self.frames, self.full = [], False

        def enqueue(self, snapshot, new_events=None, created_at_utc=None):
            if self.full:
                raise OutboxStorageError("outbox limit reached")
            self.frames.append((snapshot, new_events))

        def flush_once(self):
            raise ValueError("single frame exceeds the limit")       # must not end the thread

    gw, clock, ship, lines = Gateway(Hal(), Topology.load()), Clock(), Shipper(), []
    feed = DashboardFeed(gw, ship, log=lines.append, clock=clock)       # thread not started yet
    take = lambda: (feed.update(), feed.inbox.qsize())[1]
    gw.step()
    assert take() == 1 and take() == 1, "one at the start, then not before something is due"
    clock.t += EVENT_GAP_S + 1
    assert take() == 1, "no news"
    alert(gw, 1)
    gw.step()
    assert take() == 2
    alert(gw, 2, "LEAK_PRESSURE_DROP")
    gw.step()
    assert take() == 2, "too soon after the last"
    clock.t += EVENT_GAP_S
    assert take() == 3
    clock.t += 60
    assert take() == 4
    # the copy is the thread's own
    copied = feed.inbox.queue[-1]["snapshot"]
    gw.analytics.last_supply["state"] = "CHANGED"
    assert copied["supply"]["state"] != "CHANGED"
    # a full disk: the snapshot is skipped, its decisions go with the next one
    first, second, third = (feed.inbox.get_nowait() for _ in range(3))
    ship.full = True
    feed._store(second)
    feed._store(third)
    ship.full = False
    feed._store(feed.inbox.get_nowait())
    names = [e["alert"] for e in ship.frames[-1][1]]
    assert names == ["LEAK_NIGHT_FLOW", "LEAK_PRESSURE_DROP"] and len([x for x in lines if "not stored" in x]) == 1
    # never more than a limit waiting, and decisions are not lost while it is full
    for _ in range(WAITING_FRAMES + 5):
        clock.t += 61
        feed.update()
    taken = feed.taken
    alert(gw, 3, "LEAK_CONTINUOUS_FLOW")
    gw.step()
    clock.t += 61
    feed.update()
    assert feed.inbox.qsize() == WAITING_FRAMES and feed.taken == taken < len(gw.events)
    feed.thread.start()
    wait(lambda: feed.inbox.empty(), "thread drains")
    assert feed.thread.is_alive() and any("could not send" in x for x in lines)
    feed.stop()
    assert any(e["alert"] == "LEAK_CONTINUOUS_FLOW" for _, events in ship.frames for e in events)


def check_gateway_program():
    folder, port = tempfile.mkdtemp(), free_port()
    keys = os.path.join(folder, "gateway_keys.json")
    with open(keys, "w") as f:
        json.dump({n: sim_key(n).hex() for n in NAMES}, f)

    class Radio:
        polls = 0

        def send(self, raw, cad=False):
            return True

        def receive(self):
            Radio.polls += 1
            if Radio.polls == 60:
                signal.raise_signal(signal.SIGTERM)
            return []

        def close(self):
            pass

    saved = server.open_radio, os.environ.get("AQUA_DASHBOARD_TOKEN")
    variables = {n: os.environ.pop(n, None) for n in ("MQTT_HOST", "DASHBOARD_URL", "DASHBOARD_GATEWAY_ID",
                                                      "AQUA_TOPOLOGY_PATH")}
    handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    dashboard = Dashboard(folder, port)
    try:
        server.open_radio = lambda p: Radio()
        os.environ["AQUA_DASHBOARD_TOKEN"] = "secret"
        os.environ["DASHBOARD_URL"] = dashboard.base + "/ingest"        # as compose.yaml passes it
        server.main(["--keys", keys, "--data", os.path.join(folder, "data")])
        assert [g["gateway_id"] for g in dashboard.get("/gateways")["gateways"]] == ["building-B"]
        assert os.path.isdir(os.path.join(folder, "data", "dashboard-outbox"))
    finally:
        dashboard.close()
        server.open_radio = saved[0]
        for name, value in {**variables, "AQUA_DASHBOARD_TOKEN": saved[1]}.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value
        for s, handler in handlers.items():
            signal.signal(s, handler)


if __name__ == "__main__":
    for check in (check_delivery_outage_and_restart, check_a_stuck_dashboard_never_delays_the_gateway,
                  check_pacing_copies_and_storage_errors, check_gateway_program):
        check()
        print(f"{check.__name__}: ok")
    print("dashboard link tests passed")
