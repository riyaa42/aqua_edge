"""comms/ with real nodes and a real gateway over a fake radio, and a fake MQTT transport and
paho library. No broker or network.

    python3 -m tests.test_comms
"""
import json
import os
import re
import sys
import types

from comms.client import MqttLink
from comms.common import (CHANNELS, KIND_CHANNEL, SENSOR_FIELDS, alert_filters, decode, dispatcher, encode, parse,
                          payload_message)
from comms.edge import EdgeComms
from comms.gateway import GatewayComms
from edge.aqua_edge import DAY, FlatNode, TankNode
from gateway.core import Gateway
from gateway.topology import Topology
from hal.packet import pack, peek, sim_key, unpack

F1, F2, T = "B-101", "B-102", "B-TANK"
START = DAY + 3600          # 01:00 on day 2: night, learning over


class Air:
    def __init__(self):
        self.t, self.inbox, self.sent, self.up = START, {}, [], True

    def deliver(self, to, raw):
        self.sent.append((self.t, to, bytes(raw)))
        if self.up:
            self.inbox.setdefault(to, []).append(raw)

    def take(self, name):
        return self.inbox.pop(name, [])


class NodeRadio:
    storage_path, slot = None, None
    key = staticmethod(sim_key)

    def __init__(self, air, name):
        self.air, self.name, self.reading = air, name, {}

    def now(self):
        return self.air.t

    def read(self):
        return dict(self.reading)

    def send(self, raw, cad=False):
        self.air.deliver("GW", raw)

    def receive(self):
        return self.air.take(self.name)

    def led(self, colour):
        pass


class GatewayRadio:
    storage_path = None
    key = staticmethod(sim_key)

    def __init__(self, air):
        self.air = air

    def now(self):
        return self.air.t

    def send(self, raw, cad=False):
        m = unpack(raw, sim_key)
        for to in ([F1, F2, T] if m["n"] == "GATEWAY" else [m["n"]]):
            self.air.deliver(to, raw)

    def receive(self):
        return self.air.take("GW")


class Broker:
    """A transport that keeps what was published."""

    def __init__(self):
        self.published = []

    def publish(self, where, data, qos, retain, expiry_s):
        self.published.append((where, decode(data), qos, retain, expiry_s))

    def on(self, where):
        return [p for w, p, *_ in self.published if w == f"aqua/v1/B/{where}"]


class BrokenBroker:
    def publish(self, *args):
        raise OSError("broker gone")


def run(transport_for=None):
    """Two flats and the tank: 40 minutes of night flow in F1, then a pipe-leak command to F2
    and its end. Returns the air, the nodes, the gateway and the transports."""
    air = Air()
    nodes = [FlatNode(F1, NodeRadio(air, F1)), FlatNode(F2, NodeRadio(air, F2)), TankNode(T, NodeRadio(air, T))]
    gw = Gateway(GatewayRadio(air), Topology.load().only([F1, F2]))
    transports = {}
    if transport_for:
        for node in nodes:
            transports[node.id] = transport_for()
            EdgeComms(node, transports[node.id], log=lambda line: None).attach()
        transports["GW"] = transport_for()
        GatewayComms(gw, transports["GW"], log=lambda line: None).attach()
    for node in nodes:
        node.learn_until = 0
    nodes[0].hal.reading = {"flow_lps": 0.05, "pressure_m": 10.0}
    nodes[1].hal.reading = {"flow_lps": 0.0, "pressure_m": float("nan")}
    nodes[2].hal.reading = {"level_m": 1.5, "outflow_lps": 0.05, "inflow_lps": 0.05, "ph": 7.2, "tds_ppm": 300.0,
                            "turbidity_ntu": 0.5, "temp_c": 25.0}

    def exchange():
        for _ in range(20):
            gw.step()
            for node in nodes:
                node.radio_poll()
            air.t += 0.25

    for minute in range(40):
        air.t = START + 60 * minute
        for node in nodes:
            node.step()
        exchange()
    gw.tell(air.t, F2, "PIPE_LEAK", air.t)
    exchange()
    gw.tell_ended(air.t, F2, "PIPE_LEAK_CLEARED", ("PIPE_LEAK",), air.t)
    exchange()
    return air, nodes, gw, transports


def check_does_not_change_anything():
    plain, nodes, gw, _ = run()
    watched, nodes_w, gw_w, _ = run(Broker)
    broken, nodes_b, gw_b, _ = run(BrokenBroker)
    assert len(plain.sent) > 100 and {peek(r)["k"] for _, _, r in plain.sent} >= {"BEACON", "ALERT", "SUM", "ACK", "CMD", "GOT"}
    assert plain.sent == watched.sent == broken.sent, "MQTT must not change what goes on air"
    assert gw.log == gw_w.log == gw_b.log and gw.events == gw_w.events == gw_b.events
    assert [n.open for n in nodes] == [n.open for n in nodes_w] == [n.open for n in nodes_b]


def check_what_is_published():
    air, nodes, gw, transports = run(Broker)
    node, gateway = transports[F1], transports["GW"]

    # every message the gateway accepted is on its broker once, and on the node's own broker
    from_gateway = [(parse(w).node, parse(w).channel, p["s"]) for w, p, *_ in gateway.published
                    if parse(w).channel in ("alert", "flag", "summary")]
    assert from_gateway == [(m["n"], KIND_CHANNEL[m["k"]], m["s"]) for m in gw.log]
    from_nodes = {(parse(w).node, parse(w).channel, p["s"]) for name in (F1, F2, T)
                  for w, p, *_ in transports[name].published if parse(w).channel in ("alert", "flag", "summary")}
    assert set(from_gateway) <= from_nodes
    alert = node.on(f"{F1}/alert")[0]
    assert alert == {"v": 1, "s": alert["s"], "t": alert["t"], "a": "LEAK_NIGHT_FLOW", "flow": 0.05}
    assert gateway.on(f"{F1}/alert")[0] == {**alert, "rx": gateway.on(f"{F1}/alert")[0]["rx"]}
    # and it is exactly what crossed the radio
    wire = pack(payload_message("alert", F1, alert), sim_key(F1))
    assert wire in [raw for _, _, raw in air.sent]

    # readings: one per step, kept on that node's broker, never on the gateway's
    sensors = node.on(f"{F1}/sensor")
    assert len(sensors) == 40 and sensors[-1] == {"v": 1, "t": START + 60 * 39, "flow": 0.05, "pressure": 10.0}
    assert transports[F2].on(f"{F2}/sensor")[-1].keys() == {"v", "t", "flow"}, "a missing reading is left out"
    assert set(transports[T].on(f"{T}/sensor")[-1]) == {"v", "t", *SENSOR_FIELDS.values()} - {"flow", "pressure"}
    assert not [w for w, *_ in gateway.published if w.endswith("/sensor")]
    assert not [w for w, *_ in node.published if f"/{F2}/" in w or f"/{T}/" in w], "a node has only its own"

    # a command: once on each side, although the gateway repeats it until confirmed
    for broker in (transports[F2], gateway):
        assert [(p["a"], p["over"]) for p in broker.on(f"{F2}/cmd")] == [("PIPE_LEAK", 0), ("PIPE_LEAK_CLEARED", 0)]
    states = [p["gateway_alerts"] for p in transports[F2].on(f"{F2}/state")]
    assert any("PIPE_LEAK" in s for s in states) and states[-1] == {}
    waiting = gateway.on("gateway/state/unconfirmed")
    assert any(p["commands"] for p in waiting) and waiting[-1]["commands"] == []

    # state is sent only when it changes, and retained
    assert node.on(f"{F1}/state")[-1]["problems"] == ["LEAK_NIGHT_FLOW"] and len(node.on(f"{F1}/state")) == 2
    assert node.on(f"{F1}/link")[-1]["gateway_heard"] is True and node.on(f"{F1}/link")[-1]["waiting"] == 0
    assert gateway.on(f"{F1}/link")[-1]["silent"] is False
    events = [w for w, *_ in gateway.published if "/gateway/event/" in w]
    assert events == [f"aqua/v1/B/gateway/event/{e['event']}" for e in gw.events] and events
    building = gateway.on("gateway/state/building")
    assert building[-1]["building_status"] and len(building) < 100

    # delivery settings follow the table
    for where, payload, qos, retain, expiry in node.published + gateway.published:
        how = CHANNELS[parse(where).channel]
        assert (qos, retain, expiry) == (how.qos, how.retain, how.expiry_s), where


def check_snapshot_has_no_side_effects():
    """gateway/state/building calls Gateway.snapshot() while the gateway runs."""
    _, _, gw, _ = run()
    saved = lambda: json.dumps([gw.analytics.to_state(), gw.silent, gw.events, gw.outbox], sort_keys=True, default=sorted)
    before = saved()
    gw.snapshot(now=gw.hal.now() + 3600)
    assert saved() == before


def check_outage_and_restart():
    air = Air()
    node = FlatNode(F1, NodeRadio(air, F1))
    broker = Broker()
    EdgeComms(node, broker).attach()
    node.learn_until = 0
    node.hal.reading = {"flow_lps": 0.05, "pressure_m": 10.0}
    air.up = False
    for minute in range(40):
        air.t = START + 60 * minute
        node.step()
        node.radio_poll()
    assert [p["a"] for p in broker.on(f"{F1}/alert")] == ["LEAK_NIGHT_FLOW"], "local subscribers need no gateway"
    assert broker.on(f"{F1}/link")[-1]["gateway_heard"] is False and broker.on(f"{F1}/link")[-1]["waiting"] >= 3
    # the program restarts with messages still queued: they are published again, with the same numbers
    again = Broker()
    EdgeComms(node, again).update()
    assert [p["s"] for p in again.on(f"{F1}/alert")] == [p["s"] for p in broker.on(f"{F1}/alert")]
    # one message comms cannot translate does not stop the ones after it
    node.store.add({"n": F1, "t": air.t, "k": "ALERT", "a": "NOT_IN_THE_LIST"})
    node.store.add({"n": F1, "t": air.t, "k": "ALERT", "a": "FLAT_LEAK_CLEARED"})
    lines = []
    late = Broker()
    EdgeComms(node, late, log=lines.append).update()
    assert [p["a"] for p in late.on(f"{F1}/alert")][-1] == "FLAT_LEAK_CLEARED" and len(lines) == 1


# --- client.py against a fake paho ---
def fake_paho():
    class Client:
        last = None

        def __init__(self, api, client_id, protocol):
            Client.last, self.args, self.calls, self.online = self, (api, client_id, protocol), [], False

        def __getattr__(self, name):                    # record every other call
            return lambda *a, **k: self.calls.append((name, a, k))

        def publish(self, *args, **kwargs):
            self.calls.append(("publish", args, kwargs))
            return types.SimpleNamespace(wait_for_publish=lambda s: self.calls.append(("waited", (s,), {})))

        def is_connected(self):
            return self.online

        def named(self, name):
            return [(a, k) for n, a, k in self.calls if n == name]

    def matches(filter_, where):
        pattern = re.escape(filter_).replace(r"\+", "[^/]+").replace(r"/\#", "(/.*)?")
        return re.fullmatch(pattern, where) is not None

    class Properties:
        def __init__(self, kind):
            self.kind = kind

    mods = {name: types.ModuleType(name) for name in
            ("paho", "paho.mqtt", "paho.mqtt.client", "paho.mqtt.packettypes", "paho.mqtt.properties")}
    client = mods["paho.mqtt.client"]
    client.Client, client.topic_matches_sub, client.MQTTv5 = Client, matches, 5
    client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    mods["paho.mqtt.packettypes"].PacketTypes = types.SimpleNamespace(CONNECT=1, PUBLISH=3)
    mods["paho.mqtt.properties"].Properties = Properties
    mods["paho"].mqtt, mods["paho.mqtt"].client = mods["paho.mqtt"], client
    sys.modules.update(mods)
    return Client


def check_client():
    saved = {n: m for n, m in sys.modules.items() if n == "paho" or n.startswith("paho.")}
    try:
        Client = fake_paho()
        got, lines, connected = [], [], []
        handler = dispatcher(lambda where, payload: got.append((where.node, where.channel)))

        def boom(where, data):
            raise RuntimeError("bad handler")

        link = MqttLink("screen", username="u", password="p", status_topic="aqua/v1/B/gateway/status",
                        subscriptions={**{f: handler for f in alert_filters()}, "aqua/v1/B/+/sensor": boom},
                        log=lines.append)
        paho = Client.last
        assert paho.args == (2, "screen", 5) and paho.named("username_pw_set") == [(("u", "p"), {})]
        (will, options), = paho.named("will_set")
        assert decode(will[1]) == {"v": 1, "online": False} and options == {"qos": 1, "retain": True}
        (_, connect), = paho.named("connect_async")      # never the blocking connect()
        assert connect["clean_start"] is False and connect["properties"].SessionExpiryInterval == 7 * 86400
        assert paho.named("loop_start") and not paho.named("connect") and paho.named("max_queued_messages_set")
        assert not paho.named("subscribe")

        link.on_connected(lambda: connected.append(1))
        link._connected(paho, None, None, 5)             # refused
        assert not connected and "refused" in lines[-1]
        for _ in range(2):                               # connect, then reconnect
            link._connected(paho, None, None, 0)
        assert len(connected) == 2 and len(paho.named("subscribe")) == 8
        assert decode(paho.named("publish")[-1][0][1]) == {"v": 1, "online": True}

        link.publish("t", b"x", 1, False, 60)
        assert paho.named("publish")[-1][1]["properties"].MessageExpiryInterval == 60
        link.publish("t", b"x", 1, True, None)
        assert paho.named("publish")[-1][1] == {"qos": 1, "retain": True, "properties": None}

        message = lambda where: types.SimpleNamespace(topic=where, payload=encode({"s": 1}))
        for where in ("aqua/v1/B/B-101/alert", "aqua/v1/B/B-101/cmd", "aqua/v1/B/B-101/sensor", "aqua/v1/B/B-101/link"):
            link._message(paho, None, message(where))
        assert got == [("B-101", "alert"), ("B-101", "cmd")] and "bad handler" in lines[-1]

        paho.online = True
        link.close()
        assert decode(paho.named("publish")[-1][0][1]) == {"v": 1, "online": False}
        assert paho.named("waited") and paho.named("disconnect") and paho.named("loop_stop")
    finally:
        for name in [n for n in sys.modules if n == "paho" or n.startswith("paho.")]:
            del sys.modules[name]
        sys.modules.update(saved)


def check_republish_after_reconnect():
    class Reconnecting(Broker):
        def on_connected(self, callback):
            self.again = callback

    air = Air()
    node = FlatNode(F1, NodeRadio(air, F1))
    broker = Reconnecting()
    EdgeComms(node, broker).attach()
    node.hal.reading = {"flow_lps": 0.0}
    node.step()
    broker.published.clear()
    broker.again()
    assert sorted(w.rsplit("/", 1)[1] for w, *_ in broker.published) == ["link", "state"], "readings may be stale"


def check_readme():
    """The README's tables agree with the code, and its JSON examples are valid."""
    text = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "comms", "README.md")).read()
    rows = re.findall(r"^\| `\S+/(\w+)(?:/\{\w+\})?` \|.*?\| (\d) \| (yes|no) \| (\S+(?: \S+)?) \|", text, re.M)
    words = {"3 min": 180, "7 days": 7 * 86400, "never": None}
    seen = set()
    for channel, qos, retain, expiry in rows:
        how = CHANNELS[channel]
        assert (int(qos), retain == "yes", words[expiry]) == tuple(how), (channel, qos, retain, expiry)
        seen.add(channel)
    assert seen == set(CHANNELS), set(CHANNELS) - seen
    examples = re.findall(r"^```json\n(.*?)^```", text, re.M | re.S)
    assert examples
    for block in examples:
        for line in block.strip().splitlines():
            where, payload = line.split(" ", 1) if line.startswith("aqua/") else (None, line)
            payload = json.loads(payload)
            assert payload["v"] == 1
            if where:
                parsed = parse(where)
                assert parsed, where
                if parsed.channel in KIND_CHANNEL.values():
                    m = payload_message(parsed.channel, parsed.node, payload)
                    assert unpack(pack(m, sim_key(parsed.node)), sim_key) == m, (where, m)


if __name__ == "__main__":
    for check in (check_does_not_change_anything, check_what_is_published, check_snapshot_has_no_side_effects,
                  check_outage_and_restart, check_republish_after_reconnect, check_client, check_readme):
        check()
        print(f"{check.__name__}: ok")
    print("comms tests passed")
