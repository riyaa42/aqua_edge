"""The edge-based gateway node: checking nodes' readings, flags by planning/valve_spec.md, and the
dashboard's valve reopen requests, with the real dashboard server on this computer and, at the end,
the real gateway and node programs.

    python3 -m tests.test_gateway_edge
"""
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from dashboard.shipper import DashboardShipper
from dashboard.store import REQUEST_TTL_S
from gateway import flags, readings
from gateway.core import REOPEN_ORDER_S, Gateway
from gateway.dashboard_link import MAX_AGE_S, DashboardCommands, DashboardFeed
from gateway.topology import BUILDING_B, Topology
from hal.packet import NAMES, pack, sim_key, unpack
from tasks import prod
from tests.test_dashboard_link import Dashboard as _Dashboard, free_port, wait
from tests.test_edge_node import free_udp_port, make

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
F1, F2, T = "B-101", "B-102", "B-TANK"
GATEWAY_TOKEN, OPERATOR = "secret", "operator-secret"


class Hal:
    key = staticmethod(sim_key)

    def __init__(self, folder=None, t=10_000.0):
        self.storage_path = folder and os.path.join(folder, "gateway.jsonl")
        self.t, self.box, self.sent, self.seq = t, [], [], 0

    def now(self):
        return self.t

    def receive(self):
        got, self.box = self.box, []
        return got

    def send(self, raw, cad=False):
        self.sent.append(raw)


def gateway(folder=None, tree=None):
    return Gateway(Hal(folder), tree or Topology.load())


def hear(gw, node, kind, **fields):
    gw.hal.seq += 1
    gw.hal.box.append(pack({"k": kind, "n": node, "s": gw.hal.seq, "t": gw.hal.t, **fields}, sim_key(node)))
    gw.step()


def kinds(gw):
    return [e["event"] for e in gw.events]


# --- deploying ---
def check_deploy_guards():
    folder = tempfile.mkdtemp()
    keys = os.path.join(folder, "keys.json")
    with open(keys, "w") as f:
        json.dump({n: sim_key(n).hex() for n in NAMES}, f)
    env_file = os.path.join(folder, ".env")
    with open(env_file, "w") as f:
        f.write(f'MQTT_GATEWAY_PASSWORD="a b"\nMQTT_VIEWER_PASSWORD=b  # comment\n# AQUA_OPERATOR_TOKEN=x\n'
                f"AQUA_DASHBOARD_TOKEN=gw\nGATEWAY_KEYS={keys}\n")
    good = prod.settings(env_file, {})
    assert good["MQTT_GATEWAY_PASSWORD"] == "a b" and good["MQTT_VIEWER_PASSWORD"] == "b"
    assert "AQUA_OPERATOR_TOKEN" not in good and prod.settings(env_file, {"AQUA_DASHBOARD_TOKEN": "z"})["AQUA_DASHBOARD_TOKEN"] == "z"
    assert prod.problems(good, modem=False) == []
    assert any("ESP8266" in x for x in prod.problems({**good, "GATEWAY_SERIAL": "/no/such/port"}))
    for wrong in ({"AQUA_DASHBOARD_TOKEN": "dev-token"}, {"AQUA_DASHBOARD_TOKEN": ""}, {"AQUA_OPERATOR_TOKEN": "gw"},
                  {"AQUA_ENABLE_OUTAGE_SIM": "1"}, {"COMPOSE_PROFILES": "virtual-nodes"}, {"GATEWAY_KEYS": "keys.json"},
                  {"GATEWAY_KEYS": ""}, {"GATEWAY_TREE": "./no-such-tree.json"}):
        found = prod.problems({**good, **wrong}, modem=False)
        assert len(found) == 1 and "gw" not in found[0].split(), (wrong, found)
    with open(keys, "w") as f:
        json.dump({"GATEWAY": sim_key("GATEWAY").hex()}, f)
    assert "no key for" in prod.problems(good, modem=False)[0]
    service = prod.unit("B-203", "/k y/keys.json", "/data/s.csv", "/var/lib/a%b", (5, 20), user="aqua", root="/opt/aqua")
    assert 'ExecStart="/opt/aqua/.venv/bin/python" "-m" "edge.edge_based" "B-203" "--keys" "/k y/keys.json"' in service
    assert '"/var/lib/a%%b"' in service and '"--actuators" "gpio"' in service and "User=aqua" in service
    assert "Restart=on-failure" in service and "--exit-at-end" not in service


# --- readings ---
def check_bad_readings_are_left_out():
    good = {"k": "SUM", "n": F1, "s": 1, "t": 5, "i": 3, "vol_l": 0.0, "p_min": -0.2}
    assert readings.check(good) == (good, [])
    bad = {**good, "vol_l": -1.0, "p_min": math.nan, "level_m": math.inf, "inflow": -0.1}
    cleaned, names = readings.check(bad)
    assert names == ["inflow", "level_m", "p_min", "vol_l"] and set(cleaned) == {"k", "n", "s", "t", "i"}

    gw = gateway(tempfile.mkdtemp(), Topology.load().only([F1, F2]))
    block = int(gw.hal.t // 900)
    hear(gw, T, "SUM", i=block, vol_l=100.0, in_l=90.0, level_m=1.4)
    hear(gw, F1, "SUM", i=block, vol_l=50.0, p_min=10.0)
    hear(gw, F2, "SUM", i=block, vol_l=float("nan"), p_min=float("inf"))
    event = next(e for e in gw.events if e["event"] == "BAD_READING")
    assert event["node"] == F2 and event["fields"] == ["p_min", "vol_l"] and event["level"] == "WARNING"
    assert F2 not in gw.sums[block] and block not in gw.balance_done, "left out, like a dead sensor"
    hear(gw, F1, "ALERT", a="LEAK_NIGHT_FLOW", flow=float("nan"))
    snapshot = gw.snapshot()
    json.dumps(snapshot, allow_nan=False)           # a browser could not read NaN
    assert snapshot["nodes"][F1]["last_sum"]["vol_l"] == 50.0
    assert len([m for m in (unpack(r, sim_key) for r in gw.hal.sent) if m["k"] == "ACK"]) == 4, "all confirmed"


# --- flags by the spec ---
def check_levels_and_lights():
    assert [flags.level(c) for c in ("FLAT_LEAK", "QUALITY_CHEMICAL", "PIPE_LEAK", "ABNORMAL_SUPPLY")] == ["ALERT"] * 4
    assert [flags.level(c) for c in ("NODE_SILENT", "CLOCK_SKEW", "VALVE_FAULT")] == ["WARNING"] * 3
    assert flags.event_level({"event": "NEW_ALERT", "alert": "LEAK_NIGHT_FLOW"}) == "ALERT"
    assert flags.event_level({"event": "PIPE_LEAK_PAST"}) == flags.event_level({"event": "NODE_BACK"}) == "INFO"

    gw = gateway()
    light = lambda n: gw.snapshot()["nodes"][n]["light"] and gw.snapshot()["nodes"][n]["light"]["pattern"]
    gw.step()
    assert light(F1) == light(T) == "G" and not gw.outbox
    hear(gw, F1, "ALERT", a="LEAK_NIGHT_FLOW", flow=0.1)
    assert light(F1) == "R2" and light(F2) == "G"
    hear(gw, T, "ALERT", a="QUALITY_CHEMICAL", ph=4.0, tds=900.0)
    assert light(T) == "R1" and light(F2) == "G", "a flat does not show the tank's water unless told"
    snapshot = gw.snapshot()
    assert {i["level"] for i in snapshot["active_incidents"]} == {"ALERT"} == {q["level"] for q in snapshot["maintenance_queue"]}
    assert all(e["level"] in ("ALERT", "WARNING", "INFO") for e in gw.events)
    assert not gw.outbox, "a node's own alert needs no command"
    hear(gw, F1, "ALERT", a="FLAT_LEAK_CLEARED")
    assert light(F1) == "G" and gw.snapshot()["incident_history"][-1]["level"] == "ALERT"

    hear(gw, T, "ALERT", a="QUALITY_CLEARED")
    hear(gw, T, "FLAG", f="SUPPLY_LOW", level=0.3, inflow=0.0)
    # the flat's leak alert just before is evidence: the tank is told it is a leak draining it
    assert light(T) == "R2" and any(c["a"] == "LEAK_TANK_DRAIN" for c in gw.outbox.values())
    hear(gw, T, "FLAG", f="SUPPLY_RECOVERED", level=1.5, inflow=0.3)
    assert light(T) == "G"

    gw.order(F2, "VALVE_CLOSE")                     # an operator's orders show as red until over
    assert light(F2) == "R2"
    hear(gw, F2, "ALERT", a="VALVE_OPENED")
    assert light(F2) == "G"
    gw.order(F2, "ALERT_LED_ON")
    assert light(F2) == "R2"
    gw.order(F2, "ALERT_LED_OFF")
    assert light(F2) == "G"

    gw.hal.t += 3000                                # silent: its light cannot be known (spec 8)
    gw.step()
    assert light(F1) is None and "NODE_SILENT" in kinds(gw)
    assert all(i["level"] == "WARNING" for i in gw.snapshot()["active_incidents"] if i["category"] == "NODE_SILENT")


# --- orders ---
def check_old_reopen_orders():
    folder = tempfile.mkdtemp()
    gw = gateway(folder)
    gw.step()
    gw.order(F1, "VALVE_OPEN", t=gw.hal.t - 60)
    assert [c["t"] for c in gw.outbox.values()] == [gw.hal.t - 60], "dated when it was asked for"
    gw.order(F1, "VALVE_CLOSE")
    try:
        gw.order(F1, "VALVE_OPEN", t=gw.hal.t - 30)     # asked before that close: must not replace it
    except ValueError:
        pass
    else:
        raise AssertionError("an older click replaced a newer order")
    assert [c["a"] for c in gw.outbox.values()] == ["VALVE_CLOSE"]
    gw.order(F1, "VALVE_OPEN")
    gw.hal.t += REOPEN_ORDER_S
    gw.step()
    assert [c["a"] for c in gw.outbox.values()] == ["VALVE_OPEN"]
    gw = Gateway(gw.hal, Topology.load())               # a restart does not renew it
    gw.hal.t += 2
    gw.step()
    assert not gw.outbox and gw.events[-1]["event"] != "ORDER"
    assert any(e["event"] == "ORDER_EXPIRED" and e["node"] == F1 for e in gw.events)

    # the node's side: a click older than a closing loses to it
    node, hal = make(F1)
    hal.wall = 10_000
    node.alert("LEAK_NIGHT_FLOW")
    gw = gateway()
    gw.hal.t = hal.now() + 5
    gw.order(F1, "VALVE_OPEN", t=node.valve_changed - 30)
    gw.step()
    hal.inbox.extend(r for r in gw.hal.sent if unpack(r, sim_key)["k"] == "CMD")
    node.radio_poll()
    assert node.valve["closed"]


# --- the reopen API ---
class Dashboard(_Dashboard):
    def __init__(self, folder, port, operator=OPERATOR):
        os.environ["AQUA_OPERATOR_TOKEN"] = operator
        try:
            super().__init__(folder, port)
        finally:
            del os.environ["AQUA_OPERATOR_TOKEN"]

    def call(self, path, body=None, token=None, raw=None):
        headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(urllib.request.Request(self.base + path, data, headers), timeout=5) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def reopen(self, node, token=OPERATOR, gateway_id="test"):
        return self.call("/valves/reopen", {"gateway_id": gateway_id, "node": node}, token)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(5)


def send_snapshot(gw, folder, port):
    shipper = DashboardShipper(f"http://127.0.0.1:{port}/api/v1/ingest", folder, gateway_id="test",
                               auth_token=GATEWAY_TOKEN, min_free_bytes=0)
    shipper.enqueue(gw.snapshot())
    assert shipper.flush_once()


def check_reopen_api():
    folder, port = tempfile.mkdtemp(), free_port()
    dash = Dashboard(folder, port)
    try:
        assert dash.reopen(F1)[0] == 404, "a gateway that has sent nothing"
        gw = gateway()
        gw.step()
        hear(gw, F1, "ALERT", a="VALVE_CLOSED")
        send_snapshot(gw, os.path.join(folder, "outbox"), port)
        for token in (None, "wrong", GATEWAY_TOKEN):
            assert dash.reopen(F1, token)[0] == 401, token
        assert dash.reopen("B-999")[0] == 404 and dash.reopen(F1, gateway_id="other")[0] == 404
        for raw in (b"nonsense", b"[1]", b'{"node": "B-101"}', b'{"gateway_id": "test", "node": 3}',
                    b" " * 5000):
            assert dash.call("/valves/reopen", token=OPERATOR, raw=raw)[0] == 400, raw[:20]
        status, reply = dash.reopen(F1)
        first = reply["request"]
        assert status == 202 and first["status"] == "pending" and first["node"] == F1
        assert dash.reopen(F1)[1]["request"]["id"] == first["id"], "a second click: the same request"
        status, reply = dash.call("/valves?gateway_id=test")
        assert status == 200 and reply["reopen_enabled"] and reply["valves"][F1]["state"] == "closed"
        assert reply["valves"][F1]["request"]["id"] == first["id"] and reply["valves"][F2]["request"] is None
        for token in (None, "wrong", OPERATOR):         # only the gateway collects and answers
            assert dash.call("/gateway/commands?gateway_id=test", token=token)[0] == 401
            assert dash.call("/gateway/commands/ack", {"gateway_id": "test", "id": first["id"], "result": "sent"},
                             token)[0] == 401
        waiting = dash.call("/gateway/commands?gateway_id=test", token=GATEWAY_TOKEN)[1]["commands"]
        assert [(c["id"], c["node"], c["command"]) for c in waiting] == [(first["id"], F1, "VALVE_OPEN")]
        assert 0 <= waiting[0]["age_s"] < 30
        assert dash.call("/gateway/commands?gateway_id=other", token=GATEWAY_TOKEN)[1]["commands"] == []
        answer = lambda result, rid=first["id"], g="test": dash.call(
            "/gateway/commands/ack", {"gateway_id": g, "id": rid, "result": result}, GATEWAY_TOKEN)
        assert answer("done")[0] == 400 and answer("sent", "nope")[0] == 404 and answer("sent", g="other")[0] == 404
        assert answer("sent")[1]["request"]["status"] == "sent"
        assert answer("rejected")[1]["request"]["status"] == "sent", "the first answer stays"
        assert dash.call("/gateway/commands?gateway_id=test", token=GATEWAY_TOKEN)[1]["commands"] == []

        # not collected in time; a late answer from the gateway is still the truth
        old = dash.server.config.app.state.store.request_reopen("test", F2, now=time.time() - REQUEST_TTL_S - 1)
        assert dash.call("/gateway/commands?gateway_id=test", token=GATEWAY_TOKEN)[1]["commands"] == []
        assert dash.call("/valves?gateway_id=test")[1]["valves"][F2]["request"]["status"] == "expired"
        assert answer("sent", old["id"])[1]["request"]["status"] == "sent"
        dash.call("/sim/outage", {"enabled": True}, GATEWAY_TOKEN)
        assert dash.call("/gateway/commands?gateway_id=test", token=GATEWAY_TOKEN)[0] == 503
    finally:
        dash.stop()

    for operator in ("", GATEWAY_TOKEN):            # no operator token, or the gateway's: off
        port = free_port()
        dash = Dashboard(tempfile.mkdtemp(), port, operator)
        try:
            assert dash.reopen(F1, operator or None)[0] == 403
        finally:
            dash.stop()


def check_gateway_collects_requests():
    folder = tempfile.mkdtemp()
    memory = os.path.join(folder, "dashboard-commands.json")
    clock = [100.0]
    link = lambda: DashboardCommands("http://127.0.0.1:9/api/v1/ingest", "test", "secret", memory,
                                     clock=lambda: clock[0])
    commands, gw = link(), gateway(folder)
    gw.step()
    ask = lambda rid, **more: {"id": rid, "node": F1, "command": "VALVE_OPEN", "age_s": 20.0,
                               "fetched": clock[0], **more}
    assert commands.carry_out(gw, ask("a"), log=lambda s: None)[1] == "sent"
    assert [(c["a"], c["t"]) for c in gw.outbox.values()] == [("VALVE_OPEN", gw.hal.t - 20)]
    number = list(gw.outbox)
    assert commands.carry_out(gw, ask("a"))[1] == "sent" and list(gw.outbox) == number, "acted on once"
    assert link().carry_out(gw, ask("a"))[1] == "sent" and list(gw.outbox) == number, "also after a restart"
    assert Gateway(gw.hal, Topology.load()).outbox, "the order was saved before the answer"

    clock[0] += 50                                  # waiting to be carried out counts towards its age
    assert commands.carry_out(gw, ask("b", age_s=MAX_AGE_S - 40, fetched=clock[0] - 50))[1] == "rejected"
    for rid, wrong in (("c", {"node": "B-999"}), ("d", {"command": "VALVE_CLOSE"}), ("e", {"age_s": float("nan")}),
                       ("f", {"age_s": -5}), ("g", {"node": 7}), ("h", {"age_s": None})):
        assert commands.carry_out(gw, ask(rid, **wrong))[1] == "rejected", wrong
    assert list(gw.outbox) == number
    commands._remember("half", None)                # stopped half way: not done again
    assert link().carry_out(gw, ask("half"))[1:] == ("rejected", "the gateway restarted while handling it: ask again")
    try:
        DashboardCommands("http://host/other", "test", "", memory)
    except ValueError:
        pass
    else:
        raise AssertionError("an address that is not the ingest one")


def check_requests_through_the_threads():
    """The real feed and server: an unreachable dashboard, a lost answer, then delivery once."""
    folder, port, lines = tempfile.mkdtemp(), free_port(), []
    url = f"http://127.0.0.1:{port}/api/v1/ingest"
    gw = gateway(folder)
    gw.hal.now = time.time
    commands = DashboardCommands(url, "test", GATEWAY_TOKEN, os.path.join(folder, "done.json"))
    real_answer, lost = commands.answer, []

    def answer_lost_once(*args):
        if not lost:
            lost.append(args)
            raise OSError("connection dropped")
        real_answer(*args)

    commands.answer = answer_lost_once
    shipper = DashboardShipper(url, os.path.join(folder, "outbox"), gateway_id="test", auth_token=GATEWAY_TOKEN,
                               min_free_bytes=0, base_backoff_s=0.2, max_backoff_s=0.2)
    feed = DashboardFeed(gw, shipper, log=lines.append, commands=commands, commands_s=0.1).start()
    dash = None

    def run_until(test, what, seconds=15):
        end = time.monotonic() + seconds
        while not test():
            assert time.monotonic() < end, f"waited too long for: {what} {lines}"
            started = time.monotonic()
            gw.step()
            assert time.monotonic() - started < 1, "the radio thread waited for the dashboard"
            time.sleep(0.02)

    try:
        run_until(lambda: any("no reopen requests" in x for x in lines), "the dashboard unreachable")
        dash = Dashboard(folder, port)
        run_until(lambda: dash.call("/valves?gateway_id=test")[0] == 200, "first snapshot")
        asked = dash.reopen(F1)[1]["request"]
        status = lambda: dash.call("/valves?gateway_id=test")[1]["valves"][F1]["request"]["status"]
        run_until(lambda: status() == "sent", "the answer, offered again after it was lost")
        assert lost and [c["a"] for c in gw.outbox.values()] == ["VALVE_OPEN"]
        assert [e for e in kinds(gw) if e == "ORDER"] == ["ORDER"], "carried out once"
        assert abs(next(iter(gw.outbox.values()))["t"] - asked["requested_at_utc"]) < 2
    finally:
        feed.stop()
        if dash:
            dash.stop()


def check_button_to_valve_end_to_end():
    """The real dashboard server, gateway program and virtual node: the node shuts its valve on a
    leak, the reopen endpoint is called, and the node reports its valve open."""
    folder, port, udp = tempfile.mkdtemp(), free_port(), free_udp_port()
    keys = os.path.join(folder, "keys.json")
    with open(keys, "w") as f:
        json.dump({n: sim_key(n).hex() for n in ("GATEWAY", "B-TANK", "B-101", "B-203", "B-301")}, f)
    tree = os.path.join(ROOT, "gateway", "virtual_4node.json")
    data, sd = os.path.join(folder, "gateway"), os.path.join(folder, "node")
    logs = {name: os.path.join(folder, f"{name}.log") for name in ("gateway", "node")}
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "AQUA_DASHBOARD_TOKEN": GATEWAY_TOKEN}

    def start(name, *args):
        return subprocess.Popen([sys.executable, "-u", "-m", *args], cwd=ROOT, env=env,
                                stdout=open(logs[name], "a"), stderr=subprocess.STDOUT)

    dash, running = Dashboard(folder, port), []
    try:
        running.append(start("gateway", "gateway.edge_based", "--keys", keys, "--data", data, "--tree", tree,
                             "--port", "none", "--udp-listen", f"127.0.0.1:{udp}",
                             "--dashboard-url", f"http://127.0.0.1:{port}/api/v1/ingest", "--dashboard-id", "test"))
        running.append(start("node", "edge.edge_based", "B-203", "--tree", tree, "--keys", keys, "--sd", sd,
                             "--radio", "udp", "--gateway", f"127.0.0.1:{udp}", "--actuators", "sim",
                             "--prime-s", "86400", "--step-s", "0.005"))

        def valve():
            status, reply = dash.call("/valves?gateway_id=test")
            return reply["valves"]["B-203"] if status == 200 else {}

        wait(lambda: "end of the sensor data" in open(logs["node"]).read(), "replay of day 2", 120)
        wait(lambda: valve().get("state") == "closed", "the dashboard shows the valve closed", 60)
        assert dash.reopen("B-203")[0] == 202
        wait(lambda: valve()["state"] == "open", "the node reports its valve open", 60)
        assert valve()["request"]["status"] == "sent"
        node_state = json.load(open(os.path.join(sd, "B-203_open.json")))
        assert not node_state["valve"]["closed"]
        light = dash.call("/snapshots/latest?gateway_id=test")[1]["frame"]["snapshot"]["nodes"]["B-203"]["light"]
        assert light["pattern"] == "R2", "reopened by hand while the leak alert stands: still red"
        for process in running:
            process.send_signal(signal.SIGTERM)
        assert [process.wait(15) for process in running] == [0, 0]
    except Exception:
        for name, path in logs.items():
            print(f"--- {name}", "".join(open(path).readlines()[-15:]))
        raise
    finally:
        for process in running:
            if process.poll() is None:
                process.kill()
        dash.stop()
        shutil.rmtree(folder, ignore_errors=True)


def check_prod_runner_setup():
    """prod-edge-edge and prod-edge-gateway fill in what they are not told."""
    from edge.edge_based import parse_args
    with tempfile.TemporaryDirectory() as home:
        def write(name, *nodes):
            path = os.path.join(home, name)
            with open(path, "w") as f:
                json.dump({n: "00" * 16 for n in nodes}, f)
            return path

        def refused(*args):
            try:
                prod.find_node_keys(*args)
            except ValueError as e:
                return str(e)
            raise AssertionError(f"accepted {args}")

        write("gateway_keys.json", "GATEWAY", "B-101", "B-203")
        assert "no node key file" in refused(None, home)
        own = write("B-203_keys.json", "GATEWAY", "B-203")
        assert prod.find_node_keys(None, home) == ("B-203", own)
        write("B-101_keys.json", "GATEWAY", "B-101")
        assert "several" in refused(None, home)
        assert prod.find_node_keys("B-203", home) == ("B-203", own)
        # a name left out is read from the key file; one given is kept
        old = dict(os.environ)
        try:
            os.environ.pop("AQUA_NODE", None)
            os.environ["AQUA_KEYS"] = own
            fill = lambda a: prod.complete_node(a)
            assert parse_args(["--radio", "udp", "--actuators", "sim"], fill)[0].name == "B-203"
            assert parse_args(["B-101", "--radio", "udp", "--actuators", "sim"], fill)[0].name == "B-101"
            os.environ.update(GATEWAY_KEYS="/k/gateway_keys.json", GATEWAY_TREE="./gateway/building_B.json")
            os.environ.pop("AQUA_TOPOLOGY_PATH", None)
            argv = prod.gateway_defaults()
            assert argv[argv.index("--keys") + 1] == "/k/gateway_keys.json"
            assert os.path.isfile(argv[argv.index("--tree") + 1])      # relative to the repository
        finally:
            os.environ.clear()
            os.environ.update(old)


if __name__ == "__main__":
    for check in (check_prod_runner_setup, check_deploy_guards, check_bad_readings_are_left_out, check_levels_and_lights, check_old_reopen_orders,
                  check_reopen_api, check_gateway_collects_requests, check_requests_through_the_threads,
                  check_button_to_valve_end_to_end):
        check()
        print(f"{check.__name__}: ok", flush=True)
    print("edge gateway tests passed")
