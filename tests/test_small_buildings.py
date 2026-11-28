"""Demo-sized buildings: one node (a tank, or a flat), and four nodes with custom names. Real
nodes and a real gateway with encrypted packets over a fake radio; the node and gateway programs
started with --tree.

    python3 -m tests.test_small_buildings
"""
import json
import os
import signal
import tempfile

import edge.edge_based as node_program
import gateway.edge_based as server
from comms.gateway import GatewayComms
from edge.aqua_edge import DAY, FlatNode, TankNode
from gateway.analytics import BASE_WEIGHTS
from gateway.core import SILENT_S, Gateway
from gateway.topology import Topology
from hal.packet import add_names, is_node, load_keys, node_number, pack, peek, sim_key, unpack
from tests.test_comms import Air, Broker, GatewayRadio, NodeRadio

START = DAY + 3600          # 01:00 on day 2: night, learning over


def tree(root, building="demo"):
    return {"schema_version": 1, "building": building, "root": root}


def entry(name, kind, *children, **more):
    return {"id": name, "type": kind, "valve": kind != "junction",
            **({"children": list(children)} if children else {}), **more}


LONE_TANK = tree(entry("DEMO-TANK", "tank", radio_id=128))
LONE_FLAT = tree(entry("DEMO-PI", "flat", radio_id=129))
FOUR = tree(entry("LAB-TANK", "tank",
                  entry("J-top", "junction", entry("pi-flat", "flat", radio_id=141), label="top"),
                  entry("virtual.2", "flat", radio_id=142), entry("virtual_3", "flat", radio_id=143), radio_id=140))


class Radios(GatewayRadio):
    def __init__(self, air, names):
        super().__init__(air)
        self.names = names

    def send(self, raw, cad=False):
        m = unpack(raw, sim_key)
        for to in (self.names if m["n"] == "GATEWAY" else [m["n"]]):
            self.air.deliver(to, raw)


def building(data, state_dir=None):
    topology = Topology(data)
    air = Air()
    radio = Radios(air, sorted(topology.devices))
    radio.storage_path = state_dir and os.path.join(state_dir, "gateway.jsonl")
    gw = Gateway(radio, topology)           # registers the custom numbers
    nodes = {}
    for name in sorted(topology.devices):
        nodes[name] = node_program.make_node(name, NodeRadio(air, name), topology)
        nodes[name].learn_until = 0
    return air, gw, nodes


def live(air, gw, nodes, minutes, readings, first=0):
    for minute in range(first, first + minutes):
        air.t = START + 60 * minute
        for name, node in nodes.items():
            node.hal.reading = readings(name, minute)
            node.step()
        for _ in range(12):
            gw.step()
            for node in nodes.values():
                node.radio_poll()
            air.t += 0.25


TANK = {"level_m": 1.5, "outflow_lps": 0.3, "inflow_lps": 0.3, "ph": 7.2, "tds_ppm": 300.0,
        "turbidity_ntu": 0.5, "temp_c": 25.0}


def events(gw):
    return [e["event"] for e in gw.events]


def check_lone_tank():
    folder = tempfile.mkdtemp()
    air, gw, nodes = building(LONE_TANK, folder)
    node = nodes["DEMO-TANK"]
    assert isinstance(node, TankNode), "a tank by its place in the tree, whatever its name"
    for hour in range(24):                          # the normal level, as its learning day would give
        node.base["level_m"].add(hour, 1.5)
        node.base["outflow_lps"].add(hour, 0.3)
    broker = Broker()
    GatewayComms(gw, broker, building="demo").attach()
    # three hours of normal use: the tank's whole outflow must not look like a pipe leak
    live(air, gw, nodes, 180, lambda n, m: TANK)
    assert len([m for m in gw.log if m["k"] == "SUM"]) >= 10 and not node.store.queue, "summaries acknowledged"
    assert not gw.pipe_alerted and not set(events(gw)) & {"PIPE_LEAK", "BALANCE_PAUSED"}, events(gw)
    assert len(gw.sums) > 0
    # bad water: reported, ranked, and published
    live(air, gw, nodes, 12, lambda n, m: {**TANK, "turbidity_ntu": 40.0}, first=180)
    snapshot = gw.snapshot()
    item = snapshot["maintenance_queue"][0]
    assert item["target"] == "DEMO-TANK" and item["category"].startswith("QUALITY") and item["affected_flats"] == 0
    assert "QUALITY_LIMIT_VIOLATION" in item["symptoms"] and item["risk_score"] >= BASE_WEIGHTS[item["category"]] + 30, \
        ("a problem with the only tank keeps the whole building's 20 points, besides 10 for the broken limit", item)
    assert list(snapshot["nodes"]) == ["DEMO-TANK"] and json.dumps(snapshot, allow_nan=False)
    assert any(w == "aqua/v1/demo/DEMO-TANK/alert" for w, *_ in broker.published)
    # low tank: the gateway decides, and tells the tank
    live(air, gw, nodes, 30, lambda n, m: {**TANK, "level_m": 0.2, "inflow_lps": 0.0}, first=192)
    assert "SUPPLY_EVENT" in events(gw) and "SUPPLY_EVENT" in node.remote and not gw.outbox, (events(gw), node.remote)
    # silence, then a restart from the saved state
    air.t += SILENT_S + 60
    gw.step()
    assert "NODE_SILENT" in events(gw) and "BALANCE_PAUSED" not in events(gw)
    again = Gateway(gw.hal, Topology(LONE_TANK))
    again.step()
    assert set(again.silent) == {"DEMO-TANK"} and again.seen_floor == gw.seen_floor


def check_lone_flat():
    folder = tempfile.mkdtemp()
    air, gw, nodes = building(LONE_FLAT, folder)
    node = nodes["DEMO-PI"]
    assert isinstance(node, FlatNode) and gw.tank is None
    live(air, gw, nodes, 75, lambda n, m: {"flow_lps": 0.05, "pressure_m": 10.0})
    assert "NEW_ALERT" in events(gw) and not node.store.queue and gw.sums, "alert and summaries acknowledged"
    snapshot = gw.snapshot()
    assert [(i["target"], i["category"], i["affected_flats"]) for i in snapshot["maintenance_queue"]][:1] \
        == [("DEMO-PI", "FLAT_LEAK", 1)] and list(snapshot["nodes"]) == ["DEMO-PI"]
    assert not set(events(gw)) & {"PIPE_LEAK", "BALANCE_PAUSED", "SUPPLY_EVENT"} and not gw.outbox
    gw.sums = {i: {"DEMO-PI": 1.0} for i in range(2000)}       # old blocks are still forgotten
    gw.step()
    assert len(gw.sums) < 700
    air.t += SILENT_S + 60
    gw.step()
    Gateway(gw.hal, Topology(LONE_FLAT)).step()
    assert "NODE_SILENT" in events(gw) and "BALANCE_PAUSED" not in events(gw)
    # building B's own names work alone too, with no radio_id
    air, gw, nodes = building(tree(entry("B-203", "flat")))
    live(air, gw, nodes, 20, lambda n, m: {"flow_lps": 0.0, "pressure_m": 10.0})
    assert gw.log and not nodes["B-203"].store.queue


def check_four_custom_nodes():
    air, gw, nodes = building(FOUR)
    assert {n: type(x).__name__ for n, x in nodes.items()} == \
        {"LAB-TANK": "TankNode", "pi-flat": "FlatNode", "virtual.2": "FlatNode", "virtual_3": "FlatNode"}
    assert [nodes[n].index for n in sorted(nodes)] == [12, 13, 14, 15], "turns stay short without a --slot"
    flats = {"flow_lps": 0.1, "pressure_m": 10.0}
    live(air, gw, nodes, 60, lambda n, m: TANK if n == "LAB-TANK" else flats)
    assert not gw.pipe_alerted and {m["n"] for m in gw.log} == set(nodes), events(gw)
    # water leaves the tank that no flat receives: a pipe leak, and the tank is told
    live(air, gw, nodes, 75, lambda n, m: {**TANK, "outflow_lps": 0.6} if n == "LAB-TANK" else flats, first=60)
    assert "PIPE_LEAK" in events(gw) and "PIPE_LEAK" in nodes["LAB-TANK"].remote, events(gw)
    assert gw.snapshot()["maintenance_queue"][0]["affected_flats"] >= 1
    numbers = {peek(raw)["n"] for _, _, raw in air.sent}
    assert numbers == set(nodes) | {"GATEWAY"}
    assert {raw[1] for _, to, raw in air.sent if to == "GW"} == {140, 141, 142, 143}, "the numbers on air"


def check_names():
    add_names({"names-a": 200})
    add_names({"names-a": 200})                     # again: fine
    assert node_number("names-a") == 200 and is_node("names-a") and node_number("B-TANK") == 1
    for clash in ({"names-a": 201}, {"names-b": 200}, {"names-c": 202, "names-d": 202}, {"names-e": 203, "x": 5},
                  {"B-101": 204}):
        try:
            add_names(clash)
        except ValueError:
            continue
        raise AssertionError(f"accepted {clash}")
    assert not any(is_node(n) for n in ("names-b", "names-c", "names-e")), "all or nothing"
    alert = {"k": "ALERT", "n": "names-a", "s": 1, "t": 0, "a": "LEAK_NIGHT_FLOW"}
    raw = pack(alert, sim_key("names-a"))
    unknown = raw[:1] + bytes([250]) + raw[2:]
    assert unpack(raw, sim_key) == alert and unpack(unknown, sim_key) is None, "a number nobody has here"
    # one end numbering a node differently cannot be read as another node: the check fails
    add_names({"names-f": 205})
    assert unpack(raw[:1] + bytes([205]) + raw[2:], sim_key) is None
    try:
        pack({**alert, "n": "never-added"}, b"k" * 16)
    except ValueError as e:
        assert "add_names" in str(e)
    else:
        raise AssertionError("packed an unknown node")


def check_programs():
    """Both programs take the tree: custom names in key files, node type, building name."""
    folder = tempfile.mkdtemp()

    def write(name, data):
        path = os.path.join(folder, name)
        with open(path, "w") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        return path

    data = tree(entry("prog-tank", "tank", entry("prog-flat", "flat", radio_id=221), radio_id=220), "lab")
    tree_path = write("tree.json", data)
    keys = write("demo_keys.json", {n: sim_key(n).hex() for n in ("GATEWAY", "prog-tank", "prog-flat")})
    short = write("short_keys.json", {n: sim_key(n).hex() for n in ("GATEWAY", "prog-tank")})
    header = "t_s,prog-tank:level_m,prog-tank:outflow_lps,prog-flat:flow_lps\n"
    csv = write("sensors.csv", header + "".join(f"{60 * i},1.5,0.2,0.1\n" for i in range(3)))

    class Radio:
        sent, closed = [], False

        def send(self, raw, cad=False):
            self.sent.append(raw)
            return True

        def receive(self):
            signal.raise_signal(signal.SIGTERM)
            return []

        def close(self):
            Radio.closed = True

    saved = node_program.open_radio, node_program.NodeBoard, node_program.make_node, server.open_radio
    saved_env = os.environ.pop("AQUA_TOPOLOGY_PATH", None), os.environ.pop("MQTT_HOST", None)
    handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    made = []
    try:
        import comms.gateway
        real_make, real_connect = saved[2], comms.gateway.connect
        node_program.open_radio = lambda: type("Quiet", (), {"send": lambda self, r, c=False: True,
                                                              "receive": lambda self: []})()
        node_program.make_node = lambda *a: made.append(real_make(*a)) or made[-1]
        import sys
        for name, kind in (("prog-tank", TankNode), ("prog-flat", FlatNode)):
            sys.argv = ["edge_based", name, "--keys", keys, "--tree", tree_path, "--csv", csv,
                        "--sd", os.path.join(folder, "sd"), "--step-s", "0", "--actuators", "sim", "--exit-at-end"]
            node_program.main()
            assert isinstance(made[-1], kind) and made[-1].id == name
        sys.argv = ["edge_based", "B-101", "--keys", keys, "--tree", tree_path, "--step-s", "0"]
        try:
            node_program.main()
        except SystemExit as e:
            assert e.code == 2
        else:
            raise AssertionError("ran a node that is not in the tree")

        server.open_radio = lambda port: Radio()
        buildings = []
        broker = Broker()
        broker.close = lambda: None
        comms.gateway.connect = lambda building, **options: buildings.append(building) or broker
        os.environ["AQUA_TOPOLOGY_PATH"] = tree_path            # as compose.yaml passes it
        server.main(["--keys", keys, "--data", os.path.join(folder, "data"), "--mqtt-host", "broker"])
        assert Radio.closed and buildings == ["lab"] and any(w.startswith("aqua/v1/lab/") for w, *_ in broker.published)
        for args, words in ((["--keys", short], "no key for: prog-flat"),
                            (["--keys", keys, "--tree", write("bad.json", "{")], "")):
            try:
                server.main([*args, "--data", folder])
            except SystemExit as e:
                assert words in str(e), e
            else:
                raise AssertionError(f"started with {args}")
        assert set(load_keys(keys)) == {"GATEWAY", "prog-tank", "prog-flat"}
    finally:
        node_program.open_radio, node_program.NodeBoard, node_program.make_node, server.open_radio = saved
        comms.gateway.connect = real_connect
        for variable, value in zip(("AQUA_TOPOLOGY_PATH", "MQTT_HOST"), saved_env):
            os.environ.pop(variable, None)
            if value is not None:
                os.environ[variable] = value
        for s, handler in handlers.items():
            signal.signal(s, handler)


if __name__ == "__main__":
    for check in (check_lone_tank, check_lone_flat, check_four_custom_nodes, check_names, check_programs):
        check()
        print(f"{check.__name__}: ok")
    print("small building tests passed")
