"""A building on a simulated clock, run either way round, for tests/test_cloud_baseline.py: the real
node programs' classes, boards, packets and Gateway, over a radio in memory that can be cut or can
lose packets. Mode "edge": edge-based nodes. Mode "cloud": thin nodes and CloudHost.
"""
import os

from edge.board import NodeBoard
from edge.cloud_based import ThinNode, prime as stream_first_day
from edge.edge_based import make_node, prime as prime_on_node
from edge.sensors import DATASET, sensor_reader
from gateway.cloud_host import CloudHost
from gateway.core import Gateway
from gateway.edge_based import GatewayBoard
from gateway.topology import Topology
from hal.packet import NAMES, peek, sim_key

START = 1_800_000_000 // 86400 * 86400
DAY = 86400
PUMP_S = 5                      # the gateway and radios are looked at this often between readings


class Air:
    """The radio channel. up: nothing arrives while False. lose(direction, header) -> True drops a packet."""

    def __init__(self, world):
        self.world, self.to_gateway, self.nodes = world, [], {}
        self.cut, self.lose = set(), None
        self.traffic = {}               # (sender, kind) -> [packets, bytes]

    def count(self, sender, raw):
        entry = self.traffic.setdefault((sender, peek(raw)["k"]), [0, 0])
        entry[0] += 1
        entry[1] += len(raw)

    def delivers(self, name, direction, raw):
        return name not in self.cut and not (self.lose and self.lose(direction, peek(raw)))


class NodeRadio:
    def __init__(self, air, name):
        self.air, self.name, self.inbox = air, name, []

    def send(self, raw, cad=False):
        self.air.count(self.name, raw)
        if self.air.delivers(self.name, "up", raw):
            self.air.to_gateway.append((self.air.world.t, raw))
        return True

    def receive(self):
        got, self.inbox = self.inbox, []
        return got


class GatewayRadio:
    def __init__(self, air):
        self.air = air

    def send(self, raw, cad=False):
        self.air.count("GATEWAY", raw)
        for name, radio in self.air.nodes.items():
            if self.air.delivers(name, "down", raw):
                radio.inbox.append((self.air.world.t, raw))
        return True

    def receive(self):
        got, self.air.to_gateway = self.air.to_gateway, []
        return got


class World:
    def __init__(self, mode, folder, names=None, csv=DATASET, prime_s=DAY, host_options=None):
        self.mode, self.folder, self.csv, self.prime_s = mode, folder, csv, prime_s
        full = Topology.load()
        # names: the flats to keep (the tank always stays)
        self.tree = full.only([n for n in names if n != full.tank]) if names else full
        self.names = sorted(self.tree.devices)
        self.keys = {n: sim_key(n) for n in NAMES}
        self.t = float(START)
        self.air = Air(self)
        self.host_options = host_options or {}
        self.log, self.events, self.valves, self.lights, self.said = [], [], [], {}, []
        self.cursors = [0, 0]
        self.nodes, self.boards, self.waiting = {}, {}, {}
        self.start_gateway()
        for name in self.names:
            self.start_node(name, first=True)
        if self.host:               # a first day streamed at once is more than a radio's hourly limit
            self.host.limit_air = False
        self.settle()
        if self.host:
            self.host.limit_air = True

    # --- the programs ---
    def start_gateway(self):
        radio = GatewayRadio(self.air)
        self.host = None
        if self.mode == "cloud":
            self.host = radio = CloudHost(radio, self.keys, self.tree, os.path.join(self.folder, "brains"),
                                          clock=lambda: self.t, monotonic=lambda: self.t,
                                          log=self.said.append, **self.host_options)
        self.gateway = Gateway(GatewayBoard(radio, self.keys, os.path.join(self.folder, "gateway.jsonl"),
                                            clock=lambda: self.t), self.tree)
        self.cursors = [0, 0]

    def start_node(self, name, first=False):
        radio = self.air.nodes[name] = NodeRadio(self.air, name)
        storage = os.path.join(self.folder, f"{name}.jsonl")
        after = None
        if not first:
            from edge.store import saved_sample_time
            after = saved_sample_time(storage)
        board = NodeBoard(name, radio, sensor_reader(name, self.csv, after=after), storage, self.keys, gpio=False)
        board.now = lambda: self.t
        board.log = lambda text: self.said.append(text)
        move = board.set_valve

        def set_valve(closed, board=board, name=name, move=move):
            if board.actuators.valve_closed != closed:
                self.valves.append((int(self.t - START), name, "closed" if closed else "open"))
            return move(closed)

        board.set_valve = set_valve
        node = ThinNode(name, board) if self.mode == "cloud" else make_node(name, board, self.tree)
        self.nodes[name], self.boards[name] = node, board
        if first and self.prime_s:
            if self.mode == "edge":
                self.waiting[name] = prime_on_node(node, board, self.prime_s)
            elif board.advance():
                node.saved["prime_until"] = board.sample_time() + self.prime_s
                node.step(send=False)
                self.waiting[name] = stream_first_day(node, board)

    def settle(self, limit=20000):
        """Pass messages with the clock stopped until no node has anything left to send."""
        for _ in range(limit):
            self.pump()
            if not any(n.store.queue for n in self.nodes.values()):
                return
        raise AssertionError("messages never got through")

    # --- time ---
    def pump(self):
        self.gateway.step()
        new, self.cursors[0] = self.gateway.log_since(self.cursors[0], consumer="world")
        self.log += new
        new, self.cursors[1] = self.gateway.events_since(self.cursors[1], consumer="world")
        self.events += new
        for node in self.nodes.values():
            node.radio_poll()

    def minute(self, read=True):
        """One minute: every node takes its next reading, then messages move. False at the end of the data."""
        more = False
        if read:
            for name, node in self.nodes.items():
                if self.waiting.pop(name, False) or self.boards[name].advance():
                    node.step()
                    more = True
        for _ in range(60 // PUMP_S):
            self.pump()
            self.t += PUMP_S
        for name, board in self.boards.items():
            shown = (board.colour, board.actuators.blinks)
            history = self.lights.setdefault(name, [])
            if not history or history[-1][1] != shown:
                history.append((int(self.t - START) // 60, shown))
        return more

    def run(self, minutes=None):
        done = 0
        while (minutes is None or done < minutes) and self.minute():
            done += 1
        return done

    # --- what came out ---
    def messages(self, *kinds):
        return sorted((m["n"], int(m["t"] - START), m["k"], m.get("a") or m.get("f") or "") for m in self.log
                      if m["k"] in (kinds or ("ALERT", "FLAG")))

    def sums(self):
        return {(m["n"], m["i"]): {k: m[k] for k in ("vol_l", "p_min", "p_mean", "in_l", "level_m") if k in m}
                for m in self.log if m["k"] == "SUM"}

    def moves(self, name):
        """[(second of the live day, "closed" or "open")] of one node's valve."""
        return [(t, state) for t, node, state in self.valves if node == name]

    def decisions(self):
        return sorted((int(e["t"] - START), e["event"], str(e.get("node", "")), str(e.get("alert", "")))
                      for e in self.events)
