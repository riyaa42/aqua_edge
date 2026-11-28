"""The building's water pipes as a tree, read from a JSON file (building_B.json).

A parent is upstream of its children: water passes the parent first. "valve": true means a valve
sits on the pipe just before that entry, so it controls the supply of the entry and of everything
under it. The root is the tank. "junction" entries are points in the pipes with no monitoring
device; "flat" entries are leaves. "unmetered_homes": true on the tank means it also feeds homes
that are not in the tree, so its outflow cannot be compared with the flats. A building with a single monitored node is just that entry: a
tank, or a flat. Schema and rules: README.md, "Building tree".

    python3 -m gateway.topology      # self-check
"""
import json
import os

_GW_DIR = os.path.dirname(os.path.abspath(__file__))

from hal.packet import NAMES, check_custom_name

SCHEMA_VERSION = 1
BUILDING_B = os.path.join(_GW_DIR, "building_B.json")
KEYS = {"tank": {"id", "type", "valve", "children", "radio_id", "unmetered_homes"},
        "junction": {"id", "type", "valve", "children", "label"},
        "flat": {"id", "type", "valve", "children", "radio_id"}}
RADIO_NAMES = set(NAMES) - {"GATEWAY"}


class Topology:
    def __init__(self, data):
        """data: the parsed JSON file. Raises ValueError saying what is wrong."""
        if not isinstance(data, dict) or set(data) != {"schema_version", "building", "root"}:
            raise ValueError("topology needs exactly: schema_version, building, root")
        if data["schema_version"] != SCHEMA_VERSION:
            raise ValueError(f"unsupported topology schema_version {data['schema_version']!r}")
        self.data = data
        self.nodes = {}             # id -> the entry as written
        self.parent = {}            # id -> parent id (None for the tank)
        self.labels = {}            # junction label -> junction id
        self.radio_ids = {}         # custom node -> its radio number (hal.packet.add_names)
        self._add(data["root"], None)
        root = data["root"]
        self.tank = root["id"] if root["type"] == "tank" else None     # None: a lone flat
        self.flats = {i for i, n in self.nodes.items() if n["type"] == "flat"}
        self.devices = self.flats | ({self.tank} if self.tank else set())   # the monitoring nodes
        self.unmetered_homes = root.get("unmetered_homes", False)
        if not isinstance(self.unmetered_homes, bool):
            raise ValueError(f'{root["id"]}: "unmetered_homes" must be true or false')

    def _add(self, node, parent):
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not node["id"]:
            raise ValueError(f"every entry needs a text id (under {parent})")
        name, kind = node["id"], node.get("type")
        if kind not in KEYS:
            raise ValueError(f"{name}: type must be tank, junction or flat, not {kind!r}")
        lone_flat = kind == "flat" and parent is None and not node.get("children")
        if (kind == "tank") != (parent is None) and not lone_flat:
            raise ValueError(f"{name}: the root, and only the root, is the tank (or the root is a lone flat)")
        unknown = set(node) - KEYS[kind]
        if unknown:
            raise ValueError(f"{name}: unknown keys {sorted(unknown)}")
        if name in self.nodes:
            raise ValueError(f"{name}: id used twice")
        if not isinstance(node.get("valve"), bool):
            raise ValueError(f'{name}: "valve" must be true or false')
        children = node.get("children", [])
        if not isinstance(children, list):
            raise ValueError(f"{name}: children must be a list")
        if kind == "junction":
            if name in NAMES:
                raise ValueError(f"{name}: a junction has no device, so it cannot use a radio name")
            if not children:
                raise ValueError(f"{name}: a junction needs children")
            label = node.get("label", name)
            if not isinstance(label, str) or not label or label in self.labels:
                raise ValueError(f"{name}: label must be unique text")
            self.labels[label] = name
        else:
            if "radio_id" in node:              # a custom name: this is its number on air
                check_custom_name(name, node["radio_id"])
                if node["radio_id"] in self.radio_ids.values():
                    raise ValueError(f"{name}: radio_id {node['radio_id']} used twice")
                self.radio_ids[name] = node["radio_id"]
            elif name not in RADIO_NAMES:
                raise ValueError(f'{name}: not a built-in radio name, so it needs a "radio_id"')
            if kind == "flat" and children:     # its meter would count its children's water twice
                raise ValueError(f"{name}: a flat cannot have children")
        self.nodes[name], self.parent[name] = node, parent
        for child in children:
            self._add(child, name)

    @classmethod
    def load(cls, path=BUILDING_B):
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))

    def label(self, junction):
        return self.nodes[junction].get("label", junction)

    def path_up(self, name):
        """The entry itself, then everything upstream of it, ending with the root."""
        while name is not None:
            yield name
            name = self.parent[name]

    def section_of(self, flat):
        """Label of the junction feeding this flat; None if it hangs straight off the tank, or
        is not in the tree."""
        parent = self.parent.get(flat)
        return None if parent in (None, self.tank) else self.label(parent)

    def upstream_sections(self, label):
        """Labels of the junctions above this one, nearest first."""
        return [self.label(i) for i in self.path_up(self.parent[self.labels[label]]) if i != self.tank]

    def flats_below(self, name):
        """Flats whose water passes through this entry (the entry itself if it is a flat)."""
        return {f for f in self.flats if name in self.path_up(f)}

    def valve_for(self, name):
        """The nearest valve that cuts this entry's supply: the entry or the closest one upstream
        with a valve; None if there is none."""
        return next((i for i in self.path_up(name) if self.nodes[i]["valve"]), None)

    def only(self, flats):
        """A copy with just these flats (for tests that run part of a building)."""
        keep = set(flats)
        if keep - self.flats or (self.tank is None and not keep):
            raise ValueError(f"not in the topology: {sorted(keep - self.flats)}")

        def prune(node):
            if node["type"] == "flat":
                return node if node["id"] in keep else None
            kids = [c for c in map(prune, node.get("children", [])) if c]
            return {**node, "children": kids} if kids or node["type"] == "tank" else None

        return Topology(self.data if self.tank is None else {**self.data, "root": prune(self.data["root"])})


if __name__ == "__main__":
    import copy

    b = Topology.load()
    assert b.tank == "B-TANK" and b.flats == RADIO_NAMES - {"B-TANK"}
    assert list(b.labels) == ["4", "3", "2", "1", "G"] and b.labels["G"] == "B-D0"
    assert b.section_of("B-203") == "2" and b.section_of("B-G01") == "G" and b.section_of("B-999") is None
    assert b.upstream_sections("2") == ["3", "4"]
    assert b.flats_below("B-D1") == {f for f in b.flats if f[2] in "1G"}
    assert b.flats_below("B-D4") == b.flats and b.flats_below("B-203") == {"B-203"}
    assert b.valve_for("B-203") == "B-203" and b.valve_for("B-D1") == "B-TANK"   # no junction valves

    # The flats are the ones in the building's water model
    inp = os.path.join(_GW_DIR, "..", "simulation", "webots", "data", "building_B_scenario.inp")
    if os.path.exists(inp):
        with open(inp) as f:
            pipes = [line.split() for line in f.read().split("[PIPES]")[1].split("[PUMPS]")[0].splitlines()]
        feeds = {p[2]: p[1] for p in pipes if len(p) > 3 and p[2] in RADIO_NAMES and p[1].startswith("B-D")}
        assert feeds == {n: b.parent[n] for n in b.flats}, "tree differs from the water model's pipes"

    assert not b.unmetered_homes
    one, three = (Topology.load(os.path.join(_GW_DIR, f"virtual_{n}.json")) for n in ("1node", "3node"))
    assert one.tank is None and one.devices == {"B-203"} and not one.unmetered_homes
    assert three.devices == {"B-TANK", "B-203", "B-101"} and three.unmetered_homes

    some = b.only(["B-401", "B-101"])
    assert some.flats == {"B-401", "B-101"} and list(some.labels) == ["4", "3", "2", "1"]
    assert some.upstream_sections("1") == ["2", "3", "4"] and some.section_of("B-101") == "1"

    # With a junction valve, that is the one that isolates
    data = copy.deepcopy(b.data)
    d4 = data["root"]["children"][0]
    d4["valve"] = True
    assert Topology(data).valve_for("B-D1") == "B-D4"
    data["root"]["valve"] = d4["valve"] = False
    assert Topology(data).valve_for("B-D1") is None

    def rejected(change, why):
        bad = copy.deepcopy(b.data)
        change(bad, bad["root"], bad["root"]["children"][0])
        try:
            Topology(bad)
        except ValueError as e:
            assert why in str(e), (why, str(e))
        else:
            raise AssertionError(f"accepted: {why}")

    flat = lambda d4: d4["children"][0]
    rejected(lambda d, r, j: d.update(schema_version=2), "schema_version")
    rejected(lambda d, r, j: d.update(extra=1), "needs exactly")
    rejected(lambda d, r, j: r.update(type="junction"), "only the root")
    rejected(lambda d, r, j: j.update(type="tank"), "only the root")
    rejected(lambda d, r, j: j.update(type="pump"), "type must be")
    rejected(lambda d, r, j: flat(j).update(floor=4), "unknown keys")
    rejected(lambda d, r, j: flat(j).update(id="B-402"), "used twice")
    rejected(lambda d, r, j: j["children"][-1].update(label="4"), "label must be unique")
    rejected(lambda d, r, j: flat(j).pop("valve"), '"valve" must be')
    rejected(lambda d, r, j: flat(j).update(valve="yes"), '"valve" must be')
    rejected(lambda d, r, j: flat(j).update(id="B-501"), 'needs a "radio_id"')
    rejected(lambda d, r, j: flat(j).update(radio_id=200), "built in")
    rejected(lambda d, r, j: flat(j).update(id="X-1", radio_id=127), "from 128 to 255")
    rejected(lambda d, r, j: flat(j).update(id="X-1", radio_id=256), "from 128 to 255")
    rejected(lambda d, r, j: flat(j).update(id="X-1", radio_id="200"), "whole number")
    rejected(lambda d, r, j: flat(j).update(id="X-1", radio_id=True), "whole number")
    for name in ("X/1", "X+1", "X#", "../x", "gateway", "x" * 33):
        rejected(lambda d, r, j: flat(j).update(id=name, radio_id=200), "a node name is")
    rejected(lambda d, r, j: [e.update(id=f"X-{i}", radio_id=200) for i, e in enumerate(j["children"][:2])],
             "radio_id 200 used twice")
    rejected(lambda d, r, j: j.update(radio_id=200), "unknown keys")
    rejected(lambda d, r, j: j.update(id="B-TANK"), "used twice")
    rejected(lambda d, r, j: j.update(id="GATEWAY"), "cannot use a radio name")
    rejected(lambda d, r, j: flat(j).update(children=[{"id": "B-402", "type": "flat", "valve": True}]), "cannot have children")
    rejected(lambda d, r, j: j.update(children=[]), "needs children")
    rejected(lambda d, r, j: r.update(children={}), "must be a list")
    # One monitored node: a tank or a flat, with a built-in name or a custom one
    def lone(**root):
        return Topology({"schema_version": 1, "building": "demo", "root": {"valve": True, **root}})

    tank = lone(id="DEMO-TANK", type="tank", radio_id=128)
    assert (tank.tank, tank.flats, tank.devices, tank.radio_ids) == ("DEMO-TANK", set(), {"DEMO-TANK"}, {"DEMO-TANK": 128})
    one = lone(id="B-101", type="flat")
    assert (one.tank, one.flats, one.devices, one.radio_ids) == (None, {"B-101"}, {"B-101"}, {})
    assert one.section_of("B-101") is None and one.flats_below("B-101") == {"B-101"} and one.valve_for("B-101") == "B-101"
    assert one.only(["B-101"]).devices == {"B-101"} and tank.only([]).devices == {"DEMO-TANK"}
    for bad in (dict(id="J", type="junction"),
                dict(id="B-101", type="flat", children=[{"id": "B-102", "type": "flat", "valve": True}])):
        try:
            lone(**bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted as a root: {bad}")
    print("topology self-check passed")
