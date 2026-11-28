"""The edge node's behaviour: clocks, what it saves, notification levels, the valve, the gateway's
orders, and a node and gateway talking over the network radio. No hardware.

    uv run python -m tests.test_edge_node
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

from edge import policy
from edge.actuators import BUZZER_PIN, LED_PINS, PATTERNS, STEP_S, VALVE_PIN, GpioActuators
from edge.aqua_edge import (DAY, HOUR, MAX_SUMMARIES, REOPEN_AFTER_S, STALE_S, STUCK_S, TRIAL_AFTER_S,
                            TRIAL_QUIET, TRIAL_RETRY_S, TRIAL_TRIES, TRIAL_WINDOW, FlatNode, Store, TankNode,
                            saved_sample_time)
from edge.board import NodeBoard
from edge.edge_based import prime, run
from edge.sensors import sensor_reader
from edge.valve import request_open, take_request
from gateway import order
from gateway.core import MAX_GAPS, Gateway
from gateway.edge_based import GatewayBoard, open_radios
from gateway.edge_based import run as run_gateway
from gateway.topology import Topology
from hal.packet import ALERTS, FLAGS, NAMES, pack, sim_key, unpack

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UTC_MIDNIGHT_IST = 1_764_000_000 // DAY * DAY - 19800       # a real clock's value at local midnight


class Hal:
    """A node board in memory. replay: sensor rows from the dataset, one per step()."""

    def __init__(self, node, folder=None, wall=0, valve=True):
        self.t, self.wall, self.r = 0.0, wall, {}
        self.storage_path = folder and os.path.join(folder, f"{node}.jsonl")
        self.moves, self.stuck, self.buzzed, self.colour, self.inbox, self.sent = [], False, [], None, [], []
        self.blinks = None
        self.is_closed = False
        if valve:
            self.set_valve = self._set_valve

    def _set_valve(self, closed):
        self.moves.append(closed)
        if self.stuck:
            return False
        self.is_closed = closed
        return True

    def sample_time(self):
        return self.t

    def now(self):
        return self.wall + self.t

    def read(self):
        return {k: 0.0 if self.is_closed and k in ("flow_lps", "outflow_lps") else v for k, v in self.r.items()}

    def key(self, name):
        return sim_key(name)

    def buzzer(self, code):
        self.buzzed.append(code)

    def led_pattern(self, colour, blinks):
        self.colour, self.blinks = colour, blinks

    def receive(self):
        got, self.inbox = self.inbox, []
        return got

    def send(self, raw, cad=False):
        self.sent.append(unpack(raw, sim_key))


def make(name, folder=None, **options):
    hal = Hal(name, folder, **options)
    return (TankNode if name == "B-TANK" else FlatNode)(name, hal), hal


def events(node):
    """Record (sample time, name) of every alert and flag the node raises from now on."""
    raised, emit = [], node.emit

    def record(kind, **m):
        if kind != "SUM":
            raised.append((node.hal.t, m.get("a") or m.get("f")))
        emit(kind, **m)

    node.emit = record
    return raised


def replay(node, rows, until=None):
    for t, r in rows:
        if until is not None and t >= until:
            break
        node.hal.t, node.hal.r = t, r
        node.step()


def dataset(name):
    get, rows = sensor_reader(name), []
    while (sample := get()) is not None:
        rows.append(sample)
    return rows


# --- clocks and what is saved ---
def check_real_and_zero_clocks_agree():
    for name in ("B-101", "B-203", "B-TANK"):
        results = []
        for wall in (0, UTC_MIDNIGHT_IST):
            node, _ = make(name, wall=wall, valve=False)
            raised = events(node)
            replay(node, dataset(name))
            results.append(raised)
            if name != "B-TANK":
                assert sum(node.cmon.base.n) == 24 * 60, "the consumption detector learned the whole first day"
            else:
                assert node.min_level is not None
        assert results[0] == results[1] and results[0], (name, results)


def check_restarts_change_nothing():
    """Power cuts at saved points: the same alerts as without them. And no raw readings are kept."""
    for name in ("B-203", "B-TANK"):
        rows = dataset(name)
        whole, _ = make(name)
        expected = events(whole)
        replay(whole, rows)
        folder, got = tempfile.mkdtemp(), []
        cuts = [7 * HOUR, DAY - HOUR, DAY + 3 * HOUR, DAY + 12 * HOUR, DAY + 21 * HOUR, 2 * DAY]
        sizes = []
        for cut in cuts:
            node, hal = make(name, folder)
            raised = events(node)
            start = saved_sample_time(hal.storage_path)
            replay(node, [x for x in rows if start is None or x[0] > start], until=cut)
            node.save_state()
            node.store.queue = []                    # as if the gateway had acknowledged it all
            node.store._save()
            got += raised
            sizes.append(os.path.getsize(os.path.join(folder, f"{name}_state.json")))
        assert got == expected and expected, (name, set(got) ^ set(expected))
        settled = sizes[2:]                          # after the first day nothing grows
        assert max(settled) < 20_000 and max(settled) - min(settled) < 3_000, (name, sizes)
        print(f"  {name} saved state: {min(settled)}-{max(settled)} bytes, {sizes[0]} during the first day")


def check_old_baseline_file():
    folder = tempfile.mkdtemp()
    base = {"flow_lps": {"n": [60] * 24, "s": [0.6] * 24, "ss": [0.006] * 24, "today": [0, 0, 0]}}
    for learn_until, kept in ((DAY, True), (1_764_000_000, False)):      # same clock, other clock
        with open(os.path.join(folder, "B-101_baseline.json"), "w") as f:
            json.dump({"learn_until": learn_until, **base}, f)
        node, hal = make("B-101", folder)
        hal.t, hal.r = DAY + HOUR, {"flow_lps": 0.0}
        node.step()
        assert (node.base["flow_lps"].n[3] == 60) == kept, (learn_until, node.learn_until)
        os.remove(os.path.join(folder, "B-101_state.json"))


def check_priming_and_resume():
    rows = dataset("B-203")
    node, hal = make("B-203")
    raised = events(node)

    class Board:
        left = list(rows)

        def advance(self):
            if not self.left:
                return False
            hal.t, hal.r = self.left.pop(0)
            return True

        sample_time = lambda self: hal.t
        read = lambda self: hal.r

    board = Board()
    assert prime(node, board, DAY) is True and hal.t == DAY, "the first row after the day waits to be used"
    assert not raised and not node.store.queue and not hal.moves[1:] and sum(node.cmon.base.n) == 24 * 60
    node.step()
    replay(node, board.left)
    straight, _ = make("B-203")
    expected = events(straight)
    replay(straight, rows)
    assert raised == expected, set(raised) ^ set(expected)
    get = sensor_reader("B-203", after=DAY)
    assert get()[0] == DAY + 60


def check_queue_limit():
    path = os.path.join(tempfile.mkdtemp(), "q.jsonl")
    store = Store(path)
    store.add({"k": "ALERT", "t": 0})
    store.add({"k": "FLAG", "t": 0})
    store._save, save = lambda: None, store._save
    for i in range(MAX_SUMMARIES + 50):
        store.add({"k": "SUM", "t": i})
    store._save = save
    store.add({"k": "ALERT", "t": 1})
    kinds = [m["k"] for m in store.queue]
    assert kinds.count("SUM") == MAX_SUMMARIES and kinds.count("ALERT") == 2 and "FLAG" in kinds
    assert store.dropped == 50 and [m["t"] for m in store.queue if m["k"] == "SUM"][0] == 50, "oldest go first"
    again = Store(path)
    assert (again.dropped, again.seq, len(again.queue)) == (50, store.seq, len(store.queue))

    gw = Gateway(GatewayBoard(None, {}, None), Topology.load())
    for seq in range(2, 2 * (MAX_GAPS + 500), 2):               # every other number never comes
        assert not gw._is_seen_and_record("B-101", seq)
    assert len(gw.seen_above["B-101"]) <= MAX_GAPS and gw._is_seen_and_record("B-101", 4)
    assert not gw._is_seen_and_record("B-101", 2 * (MAX_GAPS + 500) - 3)


# --- levels, light and valve (planning/valve_spec.md) ---
SPEC_R1 = ("QUALITY_LIMIT_VIOLATION", "QUALITY_TEMPORAL_ANOMALY", "QUALITY_RUNOFF", "QUALITY_CHEMICAL")
SPEC_R2 = ("LEAK_NIGHT_FLOW", "LEAK_CONTINUOUS_FLOW", "LEAK_PRESSURE_DROP", "ABNORMAL_SUPPLY",
           "ABNORMAL_CONSUMPTION", "UNUSUAL_NIGHT_CONSUMPTION", "SUDDEN_CONSUMPTION_CHANGE", "PROLONGED_ZERO_FLOW")
SPEC_GATEWAY = ("PIPE_LEAK", "PIPE_LEAK_CAUSE", "SUPPLY_EVENT", "LEAK_TANK_DRAIN", "SUPPLY_UNCERTAIN")
FINE = {"flow_lps": 0.0, "pressure_m": 10.0}


def light(node):
    node.radio_poll()
    return node.hal.colour, node.hal.blinks


def check_every_local_alert_is_red_and_closes():
    assert set(ALERTS) | set(FLAGS) == set(policy.LEVELS), "every packet name has a level"
    for names, blinks in ((SPEC_R1, 1), (SPEC_R2, 2)):
        for name in names:
            node, hal = make("B-101")
            node.remote = {"PIPE_LEAK": 0}              # no exceptions, also during a pipe leak
            node.alert(name)
            assert node.valve["closed"] and hal.is_closed and node.valve["why"] == name, name
            assert light(node) == ("red", blinks) and hal.buzzed == [name], (name, light(node), hal.buzzed)
    for number, name in enumerate(SPEC_GATEWAY, 1):             # told by the gateway: the same
        node, hal = make("B-101")
        command(node, name, 100, number)
        assert node.valve == {"closed": True, "why": name, "fault": False, "hold": [name]} and hal.is_closed, name
        assert light(node) == ("red", 2) and hal.buzzed == [name], (name, light(node), hal.buzzed)
    for name in ("SUPPLY_LOW", "VALVE_FAULT", "SOMETHING_NEW"):     # not in the spec: never closes
        node, hal = make("B-101")
        if name == "VALVE_FAULT":
            node.alert(name)
        else:                                           # a flag, and a name no packet has
            node.alerts[name] = 0
        assert not node.valve["closed"] and light(node) == ("yellow", 2) and not hal.buzzed, name


def check_lights_follow_the_spec():
    node, hal = make("B-101")
    assert light(node) == ("green", 0)
    hal.r = dict(FINE)
    node.step()
    for m in list(node.store.queue):
        node.store.ack(m["s"])
    assert light(node) == ("green", 0)
    hal.t, hal.r = 60, {"flow_lps": 0.0}                # Y2: a reading is missing
    node.step()
    assert light(node) == ("yellow", 2)
    hal.t, hal.r = 120, dict(FINE)
    node.step()
    assert light(node) == ("green", 0)
    hal.t = 120 + STALE_S + 1                           # Y2: no new reading
    assert light(node) == ("yellow", 2)
    hal.t += 60
    node.step()
    assert light(node) == ("green", 0)
    hal.clock_warning = True                            # Y2: the clock
    assert light(node) == ("yellow", 2)
    hal.clock_warning = False
    hal.utc_now = lambda: (hal.t, False)
    assert light(node) == ("yellow", 2)
    del hal.utc_now
    hal.link_broken = lambda: True                      # Y1: the radio
    assert light(node) == ("yellow", 1)
    del hal.link_broken
    node.emit("SUM", i=0, vol_l=1.0)                    # Y1: waiting more than 30 s
    sent = hal.t
    assert light(node) == ("green", 0)
    hal.t = sent + STUCK_S + 1
    hal.r = {}                                          # and a missing reading: Y1 comes first
    node.step()
    assert light(node) == ("yellow", 1)
    node.alert("LEAK_NIGHT_FLOW")                       # red over yellow
    assert light(node) == ("red", 2)
    node.alert("QUALITY_CHEMICAL")                      # R1 over R2
    assert light(node) == ("red", 1)
    node.alerts.pop("QUALITY_CHEMICAL")                 # clearing one leaves the other
    assert light(node) == ("red", 2)
    assert not any(code.startswith("SUPPLY") for code in hal.buzzed)
    node, hal = make("B-101")                           # a summary waiting for its time slot is not stuck
    hal.slot = (19, 20)
    node.emit("SUM", i=0, vol_l=1.0)
    hal.t = STUCK_S + 20
    node.transmit(hal.t)
    assert not node.stuck


def check_board_blinks_and_buzzer():
    board = NodeBoard("B-101", None, lambda: None, None, {}, gpio=False)
    board.log = lambda *a: None
    for colour, blinks, sounding in (("green", 0, False), ("yellow", 1, False), ("yellow", 2, False),
                                     ("red", 2, True), ("red", 1, True), ("green", 0, False), ("red", 2, True)):
        board.led_pattern(colour, blinks)
        assert (board.colour, board.actuators.blinks, board.actuators.buzzing) == (colour, blinks, sounding)
    # on and off times from the spec, in 200 ms steps over 2 s
    assert STEP_S == 0.2 and PATTERNS[1] == (1, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    assert PATTERNS[2] == (1, 0, 1, 0, 0, 0, 0, 0, 0, 0) and set(PATTERNS[0]) == {1}
    assert not board.clock_warning and not board.link_broken()
    board.clock_fix = 3600
    assert board.clock_warning


def command(node, code, t, number, over=0, key=None):
    hal = node.hal
    hal.inbox.append(pack({"k": "CMD", "n": node.id, "s": number, "t": t, "a": code, "over": over},
                          sim_key(key or node.id)))
    node.radio_poll()


def leaking(node, start, minutes, flow=0.05):
    raised = events(node)
    replay(node, [(start + 60 * i, {"flow_lps": 0.0 if node.hal.is_closed else flow, "pressure_m": 10.0})
                  for i in range(minutes)])
    node.__dict__.pop("emit")
    return [a for _, a in raised]


def check_flat_valve():
    folder = tempfile.mkdtemp()
    node, hal = make("B-101", folder)
    quiet = [(60 * i, {"flow_lps": 0.0, "pressure_m": 10.0}) for i in range(24 * 60)]
    replay(node, quiet)
    got = leaking(node, DAY + HOUR, 60)
    assert got.count("VALVE_CLOSED") == 1 and "LEAK_NIGHT_FLOW" in got and node.valve["closed"]
    # shut: no flow, and that is neither a recovery nor, a day later, a missing household
    base = list(node.base["flow_lps"].s)
    got = leaking(node, DAY + 2 * HOUR, 30 * 60)
    assert not got and node.valve["closed"] and "LEAK_NIGHT_FLOW" in node.open and hal.colour == "red", got
    assert node.base["flow_lps"].s[3] == base[3] and node.acc.get("vol_l", 0) == 0 and "p_min" not in node.acc
    # power cut: still closed, and the pin is driven again
    node, hal = make("B-101", folder)
    assert node.valve["closed"] and hal.moves == [True] and hal.is_closed
    # a forged order does nothing; neither does the gateway saying a problem is over
    t = node.valve_changed + 100
    command(node, "VALVE_OPEN", t, 1, key="B-102")
    command(node, "SUPPLY_CLEARED", t, 2)
    assert node.valve["closed"]
    # an order from before the last change, or one already over, is ignored
    command(node, "VALVE_OPEN", node.valve_changed - 100, 3)
    command(node, "VALVE_OPEN", t, 4, over=1)
    assert node.valve["closed"] and sum(1 for m in hal.sent if m and m["k"] == "GOT") >= 2
    # reopened at the node: the leak is still there, so by night it is caught and shut again
    hal.t = 3 * DAY + HOUR - 60
    request_open(folder)
    assert take_request(folder) and not take_request(folder)
    node.open_valve()
    assert not node.valve["closed"] and not hal.is_closed and node.valve["why"] == "local"
    command(node, "VALVE_CLOSE", node.valve_changed - 100, 5)       # older than the local reopening
    assert not node.valve["closed"]
    got = leaking(node, 3 * DAY + HOUR, 40)
    assert got.count("LEAK_NIGHT_FLOW") == 1 and got.count("VALVE_CLOSED") == 1 and node.valve["closed"], got
    # gateway order opens it; the leak fixed: the ordinary recovery follows
    command(node, "VALVE_OPEN", node.valve_changed + 10, 6)
    assert not node.valve["closed"]
    got = leaking(node, 3 * DAY + 5 * HOUR, 20, flow=0.0)
    assert "FLAT_LEAK_CLEARED" in got and not node.valve["closed"]
    for m in list(node.store.queue):
        node.store.ack(m["s"])
    assert light(node) == ("green", 0), (node.active(), light(node))


def shut_for_leak(folder=None, trials=True):
    """A flat that has learned a quiet day and shut itself on a night leak."""
    node, hal = make("B-101", folder)
    node.trial_reopen = trials
    replay(node, [(60 * i, {"flow_lps": 0.0, "pressure_m": 10.0}) for i in range(24 * 60)])
    leaking(node, DAY + HOUR, 60)
    assert node.valve["closed"] and node.valve["hold"] == ["LEAK_NIGHT_FLOW"]
    return node, hal


DRIP = 0.04       # a small leak: too little for the high-use check


def until_open(node, flow=DRIP, limit=30 * 60):
    """Readings, one a minute, until the valve opens. The (sample) time it did, or None."""
    for _ in range(limit):
        leaking(node, node.hal.t + 60, 1, flow=flow)
        if not node.valve["closed"]:
            return node.hal.t


def flows(node, flow, n):
    return leaking(node, node.hal.t + 60, n, flow=flow)


def check_trial_reopening():
    assert (TRIAL_AFTER_S, TRIAL_RETRY_S, TRIAL_TRIES, TRIAL_WINDOW, TRIAL_QUIET) == (6 * HOUR, 12 * HOUR, 2, 60, 10)
    node, hal = shut_for_leak(trials=False)                     # off unless asked for
    assert not FlatNode("B-102", Hal("B-102")).trial_reopen and until_open(node) is None

    # fails: the water never stops. Twice, then only a person can reopen
    node, hal = shut_for_leak()
    shut_at = node.valve["shut_at"]
    opened = until_open(node)
    assert opened - shut_at == TRIAL_AFTER_S and node.trial["run"] and not hal.is_closed
    assert light(node)[0] == "red", "open on trial, the problem not yet over"
    got = flows(node, DRIP, TRIAL_WINDOW - 1)
    assert not node.valve["closed"] and "VALVE_CLOSED" not in got
    got = flows(node, DRIP, 1)
    assert got == ["VALVE_CLOSED"] and node.valve["closed"] and hal.is_closed and node.trial == {"failed": 1, "run": None}
    assert node.valve["hold"] == ["LEAK_NIGHT_FLOW"] and "LEAK_NIGHT_FLOW" in node.open and light(node)[0] == "red"
    flows(node, DRIP, 1)
    again = node.valve["shut_at"]
    opened = until_open(node)
    assert opened - again == TRIAL_RETRY_S, (opened - again) / HOUR
    flows(node, DRIP, TRIAL_WINDOW)
    assert node.valve["closed"] and node.trial["failed"] == 2
    assert until_open(node, limit=48 * 60) is None and node.valve["closed"]
    node.open_valve()                                           # by hand: a new series
    assert not node.valve["closed"] and node.trial == {"failed": 0, "run": None}

    # a usage alert confirmed again during the trial fails it too, and then it holds the valve as well
    node, hal = shut_for_leak()
    until_open(node)
    got = flows(node, 0.2, TRIAL_WINDOW)
    assert "ABNORMAL_CONSUMPTION" in got and node.valve["closed"] and node.trial == {"failed": 1, "run": None}
    assert set(node.valve["hold"]) == {"LEAK_NIGHT_FLOW", "ABNORMAL_CONSUMPTION"}, node.valve

    # nine quiet minutes are not ten: the last reading of the window decides
    node, hal = shut_for_leak()
    until_open(node)
    for _ in range(6):
        flows(node, 0.0, TRIAL_QUIET - 1)
        flows(node, DRIP, 1)
    assert node.valve["closed"] and node.trial["failed"] == 1

    # passes: the water stops. Stays open, the leak is reported over, and a new one is caught
    node, hal = shut_for_leak()
    node.trial["failed"] = 1
    until_open(node)
    flows(node, DRIP, 15)
    got = flows(node, 0.0, TRIAL_QUIET)
    assert not node.valve["closed"] and node.trial == {"failed": 0, "run": None} and "FLAT_LEAK_CLEARED" in got, got
    assert "VALVE_CLOSED" not in flows(node, 0.0, 5 * 60) and not node.valve["closed"]
    got = leaking(node, 3 * DAY + HOUR, 40)
    assert "VALVE_CLOSED" in got and node.valve["closed"] and node.trial == {"failed": 0, "run": None}, got

    # a missing reading fails it
    node, hal = shut_for_leak()
    until_open(node)
    replay(node, [(hal.t + 60, {"flow_lps": 0.0})])
    assert node.valve["closed"] and node.trial["failed"] == 1

    # so do readings that stop, noticed by the radio loop alone
    node, hal = shut_for_leak()
    until_open(node)
    hal.t += STALE_S
    node.radio_poll()
    assert not node.valve["closed"]
    hal.t += 1
    node.radio_poll()
    assert node.valve["closed"] and node.trial["failed"] == 1

    # a restart: shut, never driven open on the way, and counted
    folder = tempfile.mkdtemp()
    node, hal = shut_for_leak(folder)
    until_open(node)
    flows(node, 0.0, 3)
    node, hal = make("B-101", folder)                           # trials need not be on any more
    assert node.valve["closed"] and hal.is_closed and all(hal.moves) and node.trial == {"failed": 1, "run": None}
    assert node.valve["hold"] == ["LEAK_NIGHT_FLOW"] and "LEAK_NIGHT_FLOW" in node.open
    node, hal = make("B-101", folder)                           # counted once, and remembered
    assert node.trial["failed"] == 1

    # the wait goes on across a restart
    folder = tempfile.mkdtemp()
    node, hal = shut_for_leak(folder)
    shut_at, t = node.valve["shut_at"], node.hal.t
    node, hal = make("B-101", folder)
    node.trial_reopen, hal.t = True, t
    assert until_open(node) - shut_at == TRIAL_AFTER_S

    # the gateway during a trial: a problem or an order to close shuts it at once
    for code in ("PIPE_LEAK", "ALERT_LED_ON", "VALVE_CLOSE"):
        node, hal = shut_for_leak()
        until_open(node)
        command(node, code, hal.now(), 1)
        assert node.valve["closed"] and node.trial == {"failed": 1, "run": None}, code
        assert until_open(node) is None, code                   # excluded while that lasts
    # its order to open during a trial is a person taking over: no more trial
    node, hal = shut_for_leak()
    node.trial["failed"] = 1
    node.valve["shut_at"] -= DAY
    until_open(node)
    flows(node, DRIP, 40)
    command(node, "VALVE_OPEN", hal.now() + 5, 1)
    assert not node.valve["closed"] and node.trial == {"failed": 0, "run": None}
    assert "VALVE_CLOSED" not in flows(node, DRIP, 25) and not node.valve["closed"]

    # not tried: a gateway problem (until it ends), an operator's order, a stuck valve, missing readings
    node, hal = shut_for_leak()
    command(node, "PIPE_LEAK", node.valve_changed + 10, 1)
    assert until_open(node) is None
    command(node, "PIPE_LEAK_CLEARED", hal.now(), 2)
    assert until_open(node, limit=2) is not None                # the six hours have long passed
    node, hal = shut_for_leak()
    command(node, "VALVE_CLOSE", node.valve_changed + 10, 1)
    assert until_open(node) is None
    node, hal = shut_for_leak()
    node.valve["fault"] = hal.stuck = True
    hal.is_closed = False                                       # it never shut: the water still runs
    assert until_open(node) is None and node.valve["fault"] and not node.trial["run"]
    node, hal = shut_for_leak()
    replay(node, [(hal.t + 60 * i, {"flow_lps": DRIP}) for i in range(1, 8 * 60)])
    assert node.valve["closed"] and not node.trial["run"]
    node, hal = make("B-101")                                   # shut by the gateway alone
    node.trial_reopen = True
    command(node, "PIPE_LEAK", 100, 1)
    assert until_open(node) is None

    # nor the tank, even for an alert of a kind a flat could try
    node, hal = shut_on_bad_water()
    node.trial_reopen = True
    spell(node, 600, BAD, 8 * 60)
    assert node.valve["closed"] and not node.trial["run"]

    # a valve that will not open for the trial
    node, hal = shut_for_leak()
    hal.stuck = True
    node.valve["shut_at"] -= DAY
    got = flows(node, DRIP, 1)
    assert "VALVE_FAULT" in got and node.valve["closed"] and node.trial == {"failed": 1, "run": None}, got


def check_gateway_orders_and_faults():
    node, hal = make("B-101")
    command(node, "VALVE_CLOSE", 100, 1)
    assert node.valve == {"closed": True, "why": "gateway", "fault": False, "hold": []} and hal.buzzed == ["VALVE_CLOSE"]
    command(node, "VALVE_CLOSE", 100, 1)            # repeated until confirmed: acted on once
    assert hal.moves.count(True) == 1
    command(node, "VALVE_OPEN", 200, 2)
    command(node, "VALVE_CLOSE", 150, 3)            # late: older than the opening
    assert not node.valve["closed"]
    command(node, "ALERT_LED_ON", node.valve_changed + 100, 4)
    on = node.valve_changed
    assert hal.colour == "red" and node.valve["closed"] and node.valve["why"] == "ALERT_LED_ON"
    command(node, "ALERT_LED_OFF", node.led_changed - 50, 5)       # older: ignored
    assert hal.colour == "red"
    command(node, "ALERT_LED_OFF", node.led_changed + 50, 6)
    assert hal.colour != "red" and node.valve["closed"], "the end of the red does not reopen"
    command(node, "VALVE_OPEN", on + 200, 9)
    assert not node.valve["closed"]

    hal.stuck = True                                # the valve will not move
    command(node, "VALVE_CLOSE", node.valve_changed + 100, 7)
    reports = [m["a"] for m in node.store.queue if m["k"] == "ALERT"]
    assert reports[-1] == "VALVE_FAULT" and node.valve["fault"] and not node.shut and hal.colour == "red"
    got = leaking(node, DAY + HOUR, 5)
    assert not node.flat_paused, "water still runs, so it is still watched"
    hal.stuck = False
    command(node, "VALVE_CLOSE", node.valve_changed + 100, 8)            # tried again
    assert node.shut and not node.valve["fault"]

    def broken(closed):
        raise OSError("pin driver")
    node, hal = make("B-102")
    hal.set_valve = broken
    command(node, "VALVE_CLOSE", 100, 1)
    assert node.valve["fault"]

    node, hal = make("B-103", valve=False)          # a node without a valve says so
    command(node, "VALVE_CLOSE", 100, 1)
    assert [m["a"] for m in node.store.queue if m["k"] == "ALERT"] == ["VALVE_FAULT"] and not node.shut


def check_tank_valve():
    good = {"outflow_lps": 0.1, "inflow_lps": 0.0, "level_m": 1.5, "ph": 7.2, "tds_ppm": 250.0,
            "turbidity_ntu": 0.5, "temp_c": 25.0}
    bad = {**good, "tds_ppm": 900.0, "ph": 9.4}
    folder = tempfile.mkdtemp()
    node, hal = make("B-TANK", folder)
    # bad water on the first day, while still learning: the limits need nothing learned
    replay(node, [(60 * i, bad) for i in range(10)])
    assert node.valve["closed"] and node.valve["why"] == "QUALITY_LIMIT_VIOLATION", node.valve
    raised = events(node)
    replay(node, [(600 + 60 * i, good) for i in range(30)])
    names = [a for _, a in raised]
    assert "QUALITY_CLEARED" in names and "VALVE_OPENED" not in names and node.valve["closed"], names
    node.__dict__.pop("emit")
    for m in list(node.store.queue):
        node.store.ack(m["s"])
    assert light(node) == ("green", 0), "the light follows the conditions, not the valve"
    command(node, "VALVE_OPEN", node.valve_changed + 10, 1)         # the gateway's reopen signal
    assert not node.valve["closed"] and not hal.is_closed
    # reopened while the water is still bad, also after a power cut: confirmed again, closed again
    replay(node, [(3000 + 60 * i, bad) for i in range(10)])
    assert node.valve["closed"]
    node.open_valve()
    node.save_state()
    node, hal = make("B-TANK", folder)
    raised = events(node)
    replay(node, [(3600 + 60 * i, bad) for i in range(2)])
    assert not node.valve["closed"] and hal.colour == "red", "two readings are not yet a confirmation"
    replay(node, [(3720 + 60 * i, bad) for i in range(5)])
    names = [a for _, a in raised]
    assert node.valve["closed"] and names.count("QUALITY_LIMIT_VIOLATION") == 1, names
    replay(node, [(4200 + 60 * i, good) for i in range(15)])
    assert "QUALITY_CLEARED" in [a for _, a in raised] and node.valve["closed"]
    # while shut, a still outflow is not judged (a long tolerance keeps it shut for this)
    node.reopen_after_s = DAY
    replay(node, [(DAY + 60 * i, {**good, "outflow_lps": 3.0}) for i in range(60)])
    assert "ABNORMAL_SUPPLY" not in [a for _, a in raised] and node.acc.get("vol_l", 0) == 0


GOOD = {"outflow_lps": 0.1, "inflow_lps": 0.0, "level_m": 1.5, "ph": 7.2, "tds_ppm": 250.0,
        "turbidity_ntu": 0.5, "temp_c": 25.0}
BAD = {**GOOD, "tds_ppm": 900.0, "ph": 9.4}


def spell(node, start, r, n):
    replay(node, [(start + 60 * i, r) for i in range(n)])


def shut_on_bad_water(folder=None, tolerance=600):
    node, hal = make("B-TANK", folder)
    node.reopen_after_s = tolerance
    spell(node, 0, BAD, 10)
    assert node.valve["closed"] and node.valve["why"] in SPEC_R1 and node.valve["hold"]
    return node, hal


def check_reopens_after_the_tolerance():
    assert REOPEN_AFTER_S == 1800
    node, hal = shut_on_bad_water(tolerance=REOPEN_AFTER_S)
    raised = events(node)
    spell(node, 600, GOOD, 60)
    at = {name: t for t, name in raised}
    assert not node.valve["closed"] and not hal.is_closed and node.valve["why"] == "recovered", node.valve
    # 30 normal readings, the one with the recovery message the first of them
    assert at["VALVE_OPENED"] - at["QUALITY_CLEARED"] == REOPEN_AFTER_S - 60, at
    assert [name for _, name in raised] == ["QUALITY_CLEARED", "VALVE_OPENED"]

    # anything not normal starts the whole period again
    for interruption in ({}, {k: v for k, v in GOOD.items() if k != "temp_c"}):      # readings missing
        node, hal = shut_on_bad_water()
        while node.valve["hold"]:
            spell(node, hal.t + 60, GOOD, 1)
        spell(node, hal.t + 60, GOOD, 7)
        assert node.valve["closed"] and node.calm == 8
        spell(node, hal.t + 60, interruption, 1)
        assert node.calm == 0
        spell(node, hal.t + 60, GOOD, 9)
        assert node.valve["closed"]
        spell(node, hal.t + 60, GOOD, 1)
        assert not node.valve["closed"]

    # the same reading again, or no reading at all, does not count: only new ones
    node, hal = shut_on_bad_water()
    while node.valve["hold"]:
        spell(node, hal.t + 60, GOOD, 1)
    calm = node.calm
    for _ in range(30):
        node.step()
    hal.t += DAY
    node.radio_poll()
    assert node.valve["closed"] and node.calm == calm

    # water bad again during the wait: held again, and a new period after that recovery
    node, hal = shut_on_bad_water()
    spell(node, 600, GOOD, 18)
    assert node.valve["closed"] and not node.valve["hold"]
    spell(node, hal.t + 60, BAD, 10)
    assert node.valve["closed"] and node.valve["hold"] and node.calm == 0
    spell(node, hal.t + 60, GOOD, 40)
    assert not node.valve["closed"]

    # a restart in the wait: the period starts again, and what holds the valve is remembered
    folder = tempfile.mkdtemp()
    node, hal = shut_on_bad_water(folder)
    spell(node, 600, GOOD, 18)
    node.save_state()
    node, hal = make("B-TANK", folder)
    node.reopen_after_s = 600
    spell(node, 1700, GOOD, 9)
    assert node.valve["closed"] and node.calm == 9
    spell(node, hal.t + 60, GOOD, 1)
    assert not node.valve["closed"]

    # a second condition has to end too; here it cannot be judged while shut
    node, hal = shut_on_bad_water()
    node.alert("ABNORMAL_SUPPLY")
    assert "ABNORMAL_SUPPLY" in node.valve["hold"]
    spell(node, 600, GOOD, 90)
    assert node.valve["closed"] and node.valve["hold"] == ["ABNORMAL_SUPPLY"]

    # a gateway problem holds it until the gateway ends it, then the wait, also across a restart
    folder = tempfile.mkdtemp()
    node, hal = make("B-TANK", folder)
    command(node, "SUPPLY_EVENT", 500, 1)
    node.reopen_after_s = 600
    spell(node, 600, GOOD, 60)
    assert node.valve["closed"] and node.valve["hold"] == ["SUPPLY_EVENT"]
    command(node, "SUPPLY_CLEARED", hal.t, 2)
    node.save_state()
    node, hal = make("B-TANK", folder)
    node.reopen_after_s = 600
    spell(node, 6000, GOOD, 9)
    assert node.valve["closed"]
    spell(node, 6540, GOOD, 1)
    assert not node.valve["closed"]

    # one supply state replacing another: only the one in force holds
    node, hal = make("B-TANK")
    command(node, "SUPPLY_UNCERTAIN", 100, 1)
    command(node, "LEAK_TANK_DRAIN", 200, 2)
    assert node.valve["hold"] == ["LEAK_TANK_DRAIN"]

    # the alert-LED order is a red condition: ending it starts the wait
    node, hal = make("B-101")
    node.reopen_after_s = 300
    command(node, "ALERT_LED_ON", 100, 1)
    spell(node, 200, dict(FINE), 20)
    assert node.valve["closed"]
    command(node, "ALERT_LED_OFF", node.led_changed + 50, 2)
    spell(node, 5000, dict(FINE), 5)
    assert not node.valve["closed"]

    # it reopened by itself: a gateway problem decided before that, heard late, still closes it,
    # and so does an order to close sent before it
    for number, code in ((3, "PIPE_LEAK"), (4, "VALVE_CLOSE")):
        node, hal = shut_on_bad_water()
        hal.t = 600
        spell(node, 600, GOOD, 40)
        assert not node.valve["closed"] and node.valve_changed > 700
        command(node, code, 700, number)
        assert node.valve["closed"], code

    node, hal = shut_on_bad_water(tolerance=0)                # no tolerance: at the first normal reading
    while node.valve.get("hold"):
        spell(node, hal.t + 60, GOOD, 1)
    assert not node.valve["closed"]


def check_only_a_hand_reopens():
    # no recovery message: never, however long, also when what was learned is lost
    folder = tempfile.mkdtemp()
    node, hal = shut_on_bad_water(folder)
    os.remove(os.path.join(folder, "B-TANK_state.json"))
    node, hal = make("B-TANK", folder)
    node.reopen_after_s = 600
    spell(node, 9000, GOOD, 120)
    assert node.valve["closed"] and node.valve["hold"], "nothing said the water is good again"
    command(node, "VALVE_OPEN", node.valve_changed + 10, 9)         # the gateway's reopen signal
    assert not node.valve["closed"] and hal.moves[-1] is False

    # an operator's order to close: only a person undoes it, whether it came before or after an alert
    for alert_first in (False, True):
        node, hal = shut_on_bad_water() if alert_first else make("B-TANK")
        node.reopen_after_s = 600
        command(node, "VALVE_CLOSE", node.valve_changed + 10 if alert_first else 100, 1)
        spell(node, 600, GOOD, 120)
        assert node.valve["closed"] and node.valve["why"] == "gateway" and light(node)[0] == "red"
        command(node, "VALVE_OPEN", node.valve_changed + 10, 2)
        assert not node.valve["closed"]

    # the gateway's problem and its reopen signal, in either order
    node, hal = make("B-101")
    command(node, "PIPE_LEAK", 1000, 1)
    command(node, "VALVE_OPEN", 2000, 2)                # reopened while the problem goes on:
    assert not node.valve["closed"] and light(node) == ("red", 2) and "PIPE_LEAK" in node.remote
    command(node, "PIPE_LEAK_CAUSE", 1500, 3)           # decided before the reopening: arrives late
    assert not node.valve["closed"]
    command(node, "PIPE_LEAK_CLEARED", 3000, 4)
    command(node, "PIPE_LEAK", 4000, 5)                 # a new one closes it again
    assert node.valve["closed"]
    command(node, "VALVE_OPEN", 3500, 6)                # a reopen signal older than that closing
    assert node.valve["closed"]
    command(node, "VALVE_OPEN", 5000, 7, over=1)        # one the gateway no longer means
    assert node.valve["closed"]
    command(node, "SUPPLY_EVENT", 6000, 8, over=1)      # began and ended unheard: noted, nothing moves
    assert node.missed == [["SUPPLY_EVENT", 6000]]
    command(node, "VALVE_OPEN", 7000, 9)
    assert not node.valve["closed"]

    # an operator's order to close keeps the light red until the reopen signal
    node, hal = make("B-101")
    command(node, "VALVE_CLOSE", 100, 1)
    spell(node, 200, dict(FINE), 5)
    for m in list(node.store.queue):
        node.store.ack(m["s"])
    assert light(node) == ("red", 2)
    command(node, "VALVE_OPEN", node.valve_changed + 10, 2)
    assert not node.valve["closed"] and light(node)[0] != "red"


def check_gpio_pins_and_failed_sensor():
    log = []

    def pin(kind):
        class Pin:
            def __init__(self, number):
                self.number = number
            on = lambda self: log.append((kind, self.number, True))
            off = lambda self: log.append((kind, self.number, False))
            beep = lambda self, **k: log.append((kind, self.number, "beep"))
            value = property(fset=lambda self, v: log.append((kind, self.number, v)))
            source = None
        return Pin

    gpio = GpioActuators(pin("led"), pin("relay"), pin("buzzer"))
    assert gpio.valve(True) and gpio.valve_closed
    gpio.light("red", 2)
    red = gpio.leds["red"]
    assert [next(red.source) for _ in range(20)] == list(PATTERNS[2]) * 2 and red.source_delay == STEP_S
    assert gpio.leds["green"].source is None
    gpio.light("red")                                   # solid: the pattern stops
    assert red.source is None
    gpio.beep()
    assert ("relay", VALVE_PIN, True) in log and ("buzzer", BUZZER_PIN, "beep") in log
    assert ("led", LED_PINS["red"], True) in log and ("led", LED_PINS["green"], False) in log
    gpio.valve(False)
    assert log[-1] == ("relay", VALVE_PIN, False)
    assert VALVE_PIN not in (*LED_PINS.values(), BUZZER_PIN, 8, 9, 10, 11, 22, 25), "a pin of its own"

    calls = iter([(0.0, {"flow_lps": 0.01}), OSError("sensor bus"), (120.0, {"flow_lps": 0.01}), None])

    def get():
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    board = NodeBoard("B-101", None, get, None, {}, gpio=False)
    board.log = lambda *a: None
    assert board.advance() and board.advance() and board.read() == {} and board.sample_time() == 60
    assert board.advance() and not board.advance()
    path = os.path.join(tempfile.mkdtemp(), "x.csv")
    with open(path, "w") as f:
        f.write("t_s,B-101:flow_lps,B-101:pressure_m\n0,oops,2\n60,1\nbad,1,2\n120,1,2\n")
    get = sensor_reader("B-101", path)
    assert [get(), get(), get(), get()] == [
        (0.0, {"pressure_m": 2.0}), (60.0, {"flow_lps": 1.0}), (120.0, {"flow_lps": 1.0, "pressure_m": 2.0}), None]


def check_part_of_a_building_is_no_pipe_leak():
    """The three-node tree: the tank also feeds homes with no node, which is not a pipe leak. The same
    tree without saying so would report one."""
    volumes = {"B-TANK": 100.0, "B-203": 5.0, "B-101": 5.0}
    for unmetered in (True, False):
        data = json.load(open(os.path.join(ROOT, "gateway", "virtual_3node.json")))
        data["root"]["unmetered_homes"] = unmetered
        hal = Hal("GATEWAY", wall=UTC_MIDNIGHT_IST)
        gw = Gateway(hal, Topology(data))
        first = int(hal.wall // 900)
        for i in range(3):
            hal.t = 900 * (i + 1)
            hal.inbox += [pack({"k": "SUM", "n": n, "s": 10 * i + k + 1, "t": hal.now(), "i": first + i, "vol_l": v},
                               sim_key(n)) for k, (n, v) in enumerate(volumes.items())]
            gw.step()
        assert len(gw.log) == 9, len(gw.log)
        assert ("PIPE_LEAK" in [e["event"] for e in gw.events]) == (not unmetered), gw.events


def check_gateway_problem_closes_and_gateway_reopens():
    """The real gateway and a real node: a pipe leak the gateway decides on closes the node's valve,
    its end does not reopen it, and the operator's order does."""
    wire = {"up": [], "down": []}
    gw = Gateway(Hal("GATEWAY", wall=UTC_MIDNIGHT_IST), Topology.load())
    node, hal = make("B-TANK", wall=UTC_MIDNIGHT_IST)

    def exchange(seconds=2):
        for _ in range(int(seconds * 2)):
            gw.hal.t += 0.5
            hal.t = gw.hal.t
            gw.hal.inbox += [pack(m, sim_key("B-TANK")) for m in hal.sent[:] if m and m["k"] != "ACK"]
            hal.sent.clear()
            gw.step()
            hal.inbox += [pack(m, sim_key(m["n"])) for m in gw.hal.sent if m and m["n"] in ("B-TANK", "GATEWAY")]
            gw.hal.sent.clear()
            node.radio_poll()

    exchange()
    gw.decide(gw.hal.now(), "PIPE_LEAK", interval=1, error_pct=40.0, suspected_floor=None, suspected_range=None,
              localization_confidence=None, floor_drops={})
    exchange()
    assert node.valve["closed"] and node.valve["why"] == "PIPE_LEAK" and hal.colour == "red" and not gw.outbox
    exchange(5)
    assert "VALVE_CLOSED" in [m["a"] for m in gw.log if m["k"] == "ALERT"]
    gw.end_pipe_leak(gw.hal.now())
    exchange(5)                                     # ended, and the tolerance has not run: still shut
    assert not node.remote and node.valve["closed"] and hal.colour != "red", (node.remote, hal.colour)
    gw.order("B-TANK", "VALVE_OPEN")
    exchange(10)
    assert not node.valve["closed"] and not gw.outbox
    assert gw.snapshot()["nodes"]["B-TANK"]["valve"] == "open"


# --- the node program and the gateway over the network radio ---
def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait(test, what, seconds=60):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if test():
            return
        time.sleep(0.05)
    raise AssertionError(f"waited too long for: {what}")


class RunningGateway:
    def __init__(self, folder, port):
        topology = Topology.load()
        keys = {n: sim_key(n) for n in NAMES}
        self.radio = open_radios("none", f"127.0.0.1:{port}")
        self.gw = Gateway(GatewayBoard(self.radio, keys, os.path.join(folder, "gateway.jsonl")), topology)
        self.stop_flag, self.lines = threading.Event(), []
        self.thread = threading.Thread(target=run_gateway, args=(self.gw, self.stop_flag, self.lines.append, folder),
                                       daemon=True)
        self.thread.start()

    def alerts(self):
        return [m["a"] for m in list(self.gw.log) if m["k"] == "ALERT"]

    def stop(self):
        self.stop_flag.set()
        self.thread.join(5)
        self.radio.close()


def check_programs_over_the_network():
    folder, port = tempfile.mkdtemp(), free_udp_port()
    gateway_data, sd = os.path.join(folder, "gateway"), os.path.join(folder, "node")
    os.makedirs(gateway_data)
    keys = os.path.join(folder, "node_keys.json")
    with open(keys, "w") as f:
        json.dump({n: sim_key(n).hex() for n in ("GATEWAY", "B-203")}, f)
    log = os.path.join(folder, "node.log")
    state = lambda: json.load(open(os.path.join(sd, "B-203_open.json")))
    queue = lambda: Store(os.path.join(sd, "B-203.jsonl")).queue

    def start_node():
        return subprocess.Popen(
            [sys.executable, "-u", "-m", "edge.edge_based", "B-203", "--keys", keys, "--sd", sd, "--radio", "udp",
             "--gateway", f"127.0.0.1:{port}", "--actuators", "sim", "--prime-s", "86400", "--step-s", "0.005"],
            cwd=ROOT, stdout=open(log, "a"), stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    node, gateway = None, None
    try:
        node = start_node()                         # no gateway yet: the node works on its own
        wait(lambda: "end of the sensor data" in open(log).read(), "replay of day 2", 120)
        assert state()["valve"]["closed"], "valve closed with no gateway at all"
        waiting = queue()
        assert {"LEAK_NIGHT_FLOW", "VALVE_CLOSED"} <= {m.get("a") for m in waiting}
        gateway = RunningGateway(gateway_data, port)            # back online: all of it arrives
        wait(lambda: not queue(), "the stored messages delivered")
        assert {"LEAK_NIGHT_FLOW", "VALVE_CLOSED"} <= set(gateway.alerts())
        assert len([m for m in gateway.gw.log]) >= len(waiting)
        snapshot = lambda: gateway.gw.snapshot()["nodes"]["B-203"]["valve"]
        assert snapshot() == "closed"

        order.request(gateway_data, "B-203", "valve-open")
        wait(lambda: "VALVE_OPENED" in gateway.alerts() and not gateway.gw.outbox, "order, report and confirmation")
        assert not state()["valve"]["closed"] and snapshot() == "open"
        order.request(gateway_data, "B-203", "led-on")
        wait(lambda: state()["led_order"] and "B-203 LED red" in open(log).read(), "alert LED order")
        order.request(gateway_data, "B-203", "valve-close")
        # the LED order closed it already: wait for the close order itself to arrive
        wait(lambda: state()["valve"]["why"] == "gateway" and not gateway.gw.outbox and snapshot() == "closed",
             "close order")
        time.sleep(1.5)                             # orders and reopenings are told apart to the second
        order.request(gateway_data, "B-999", "valve-open")      # refused, and the gateway carries on
        wait(lambda: any("not carried out" in x for x in gateway.lines), "bad order refused")
        assert gateway.thread.is_alive()

        request_open(sd)                                        # at the node itself
        wait(lambda: not state()["valve"]["closed"] and snapshot() == "open", "local reopening reported")
        order.request(gateway_data, "B-203", "valve-close")
        wait(lambda: state()["valve"]["closed"], "closed again")

        node.send_signal(signal.SIGTERM)                        # restart: still closed, nothing repeated
        assert node.wait(15) == 0 and "B-203 stopped" in open(log).read()
        heard = len(gateway.alerts())
        node = start_node()
        wait(lambda: open(log).read().count("VALVE closed") >= 3, "valve driven again after restart")
        time.sleep(1.5)
        assert state()["valve"]["closed"] and len(gateway.alerts()) == heard, gateway.alerts()[heard:]
        node.send_signal(signal.SIGTERM)
        assert node.wait(15) == 0
    except Exception:
        print("".join(open(log).readlines()[-25:]))
        raise
    finally:
        if node and node.poll() is None:
            node.kill()
        if gateway:
            gateway.stop()
        shutil.rmtree(folder, ignore_errors=True)


def check_orders_on_the_gateway():
    gw = Gateway(GatewayBoard(None, {}, None), Topology.load())
    gw.order("B-101", "VALVE_CLOSE")
    gw.order("B-101", "ALERT_LED_ON")
    gw.order("B-101", "VALVE_OPEN")                 # replaces the close still waiting
    assert sorted(c["a"] for c in gw.outbox.values()) == ["ALERT_LED_ON", "VALVE_OPEN"]
    for node, code in (("B-999", "VALVE_OPEN"), ("B-101", "PIPE_LEAK")):
        try:
            gw.order(node, code)
        except ValueError:
            continue
        raise AssertionError((node, code))


if __name__ == "__main__":
    for check in (check_real_and_zero_clocks_agree, check_restarts_change_nothing, check_old_baseline_file,
                  check_priming_and_resume, check_queue_limit, check_every_local_alert_is_red_and_closes,
                  check_lights_follow_the_spec, check_board_blinks_and_buzzer, check_reopens_after_the_tolerance, check_only_a_hand_reopens,
                  check_flat_valve, check_trial_reopening,
                  check_gateway_orders_and_faults, check_tank_valve, check_gpio_pins_and_failed_sensor,
                  check_orders_on_the_gateway, check_part_of_a_building_is_no_pipe_leak,
                  check_gateway_problem_closes_and_gateway_reopens, check_programs_over_the_network):
        check()
        print(f"{check.__name__}: ok", flush=True)
    print("edge node tests passed")
