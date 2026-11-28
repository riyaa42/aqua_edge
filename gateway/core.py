"""Aqua Edge gateway program (one per building). Runs on the laptop; an ESP8266 board on USB
serial is only the interface to the LoRa module (one SX1278, 433 MHz: hal/lora433.py).

Call step() whenever a packet arrives and at the time it returns (at least every 0.5 s).

Nodes decide on their own what they can see. The gateway combines the data of many nodes, which
some detections need (a pipe leak shows only when every flat's litres are set against the tank's).

The building comes in as a tree (topology.py, building_B.json): which nodes exist, which junction
of the pipes feeds each flat, what is upstream of what, and where the valves are.

Same hardware layer as the nodes (hal.now / send / receive). Jobs:
  1. Acknowledge every node message (so nodes can delete it from their SD queue) and log it.
     Every ACK carries the gateway time ("gt") for setting node clocks (a job for the hardware
     layer; so far only the live simulation does it).
  2. Building check, the mass balance (Ali et al. [6], supply vs branch flows): water leaving
     the tank minus water used by all flats, per 15-minute summary; above 10% for 2 blocks next
     to each other in time means a leak in the pipes between the tank and the flats (a leak
     inside a flat is after its meter, so the flat's own checks catch it). Re-arms after 2
     normal blocks in a row. Summaries may arrive in any order; an earlier leak that only
     becomes known later is logged as PIPE_LEAK_PAST.
  3. Silent nodes: a node not heard for 45 min (3 missed summaries) gives NODE_SILENT, and
     NODE_BACK when it is heard again. A repeated or replayed packet does not count as heard
     (a captured packet the gateway never received still counts once). While any node is silent
     the building check cannot be done honestly (its litres would look "missing"), so
     BALANCE_PAUSED says so; the late summaries are checked when they arrive.
  4. Supply event: the tank node reports its level far below normal (pump or power problem).
  5. Repeat check: nodes send a fresh alert every time a problem comes back; the gateway
     marks each one NEW_ALERT or REPEAT_ALERT (same node and alert type as before). A flat's
     LEAK_PRESSURE_DROP stamped during an active pipe leak also gets possible_cause=PIPE_LEAK
     (lower building pressure plus the flat's own heavy use can trigger it). Nothing is hidden.
  6. Recovery: a problem is closed only when the node says it is over (*_CLEARED,
     SUPPLY_RECOVERED), never because nothing more was heard. A recovery closes only what is
     older than itself, and an older alert arriving late does not reopen it. The pipe leak is
     closed by the building check, silence by NODE_BACK.
  7. Clock check: a message stamped more than 1 min in the future means that node's clock is
     wrong (CLOCK_SKEW), until a message stamped within 1 min of gateway time (CLOCK_OK). An
     old stamp is normal: the message waited on the SD card.
  8. Reading check (readings.py): a reading that cannot be true is left out (BAD_READING).
     Packet check (hal/packet.py): a packet that fails authentication is dropped (BAD_PACKET), and a
     (node, counter) pair already seen is a duplicate or a replay: acknowledged, not used again.
     ACKs are made with the node's key, so a fake ACK cannot make a node delete its messages.
  9. Ranking of open problems for the dashboard: analytics.py.
 10. Beacon every 5 s (and at startup), so nodes know when to send what they stored.
 11. Dispatch: what only the gateway can know is sent back at once to the nodes concerned, as a
     signed CMD, repeated every few seconds until the node confirms it (GOT):
       pipe leak (and its end)  -> the tank and every flat fed through the suspected junction
                                   (the suspected floor and all below it); only the tank if
                                   the spot cannot be told. The event also records the valve
                                   that would isolate it (isolation_valve); nothing closes.
       supply event / drain / uncertain (and recovery) -> the tank
       a flat's pressure alert during a pipe leak -> that flat (PIPE_LEAK_CAUSE: its own leak
                                   alert may not be its own leak)
     One that ended before it could be delivered still goes, marked "over".
     On by default; enable_commands=False keeps these results on the gateway only.
 12. Orders: an operator can have a node close or open its valve, or turn its alert LED on or
     off (order.py). The problems in item 11 close the valves of the nodes told about them; once
     they are ended a node may reopen by itself, after its tolerance period. What a node's valve did comes back as VALVE_CLOSED,
     VALVE_OPENED or VALVE_FAULT.
What the checks need is saved to one state file before any ACK is sent, so a restart keeps the
open problems and pipe check, and does not reopen the door to replays. Not saved: the in-memory
message log and event list. The gateway clock must carry on across a restart.
The rules of 2 and 4, and locating a pipe leak, are in detection/leakage/ (mass_balance.py,
localize.py, supply.py). How the leak checks fit together: detection/leakage/README.md.
"""
import json
import os
import random


from detection.leakage.mass_balance import PipeLeakCheck
from detection.leakage.supply import SUPPLY_CODES, SupplyRule
from edge.aqua_edge import iso_utc, save_atomic
from gateway import flags, readings
from gateway.analytics import FLAT_LEAK_ALERTS, GatewayAnalytics, category_of
from hal.link import MessageLink
from hal.lora433 import BEACON_S, AirtimeBudget, retry_wait
from hal.packet import add_names, pack, unpack
from gateway.topology import Topology  # noqa: F401  (callers build one for Gateway)

SUPPLY_WINDOW_S = 900          # a SUPPLY_LOW flag from the tank counts for this long
MASS_BALANCE_PCT = 10.0        # Ali et al. [6] threshold
MASS_BALANCE_INTERVALS = 2     # blocks in a row above threshold before alerting, and normal before re-arming
MIN_SUPPLY_L = 5.0             # ignore near-empty blocks (division by tiny numbers)
SILENT_S = 2700                # nothing heard from a node for 3 summary periods = silent
CLOCK_AHEAD_S = 60             # message time this far ahead of the gateway clock = bad node clock
SUPPLY_CODES = ("SUPPLY_EVENT", "LEAK_TANK_DRAIN", "SUPPLY_UNCERTAIN")
VALVE_REPORTS = ("VALVE_CLOSED", "VALVE_OPENED", "VALVE_FAULT")    # what a node's valve did
ORDER_GROUPS = (("VALVE_CLOSE", "VALVE_OPEN"), ("ALERT_LED_ON", "ALERT_LED_OFF"))   # an operator's
MAX_GAPS = 2000     # message numbers remembered above a gap, per node
REOPEN_ORDER_S = 900        # a reopen order its node has not confirmed in this long is dropped
STATE_VERSION = 1
# saved to disk as they are; the others need converting for JSON (save_state / load_state)
SAVED = ("seen_floor", "alert_history_list", "tank_low_t", "tank_low_recv_t", "supply_active_until",
         "tank_drain_reported", "tank_abnormal_supply_recv_t", "flat_leak_recv_t", "tank_abnormal_supply_t",
         "flat_leak_t", "last_tank_inflow", "supply_cleared_t", "p_base", "p_mean_base", "last_pair",
         "pipe_first", "pipe_alerted", "pipe_since", "balance_paused", "last_seen", "silent",
         "outbox", "command_number", "told", "told_pipe", "told_cause")
BY_BLOCK = ("sums", "p_sums", "p_mean_sums", "verdicts")     # {block number: value}


class Gateway(SupplyRule, PipeLeakCheck):
    def __init__(self, hal, topology, enable_commands=True):
        add_names(topology.radio_ids)           # custom nodes: their numbers on air
        self.hal, self.tree = hal, topology
        self.link = MessageLink(hal, hal.key)
        self.flats, self.tank = set(topology.flats), topology.tank
        self.enable_commands = enable_commands
        self.seen_floor = {}        # node -> highest contiguous seq received (1..floor)
        self.seen_above = {}        # node -> set of out-of-order seq > floor
        self.log = []               # every unique message received, with receive time
        self.events = []            # gateway's own decisions
        self.alert_history = {}     # (node, alert) -> (time of latest, how many so far)
        self.init_supply_rule()     # detection/leakage/supply.py
        self.init_pipe_check()      # detection/leakage/mass_balance.py
        self.last_seen = None       # node -> time last heard (set on the first step)
        self.silent = {}            # node -> time last heard before it went silent
        self.skewed = set()
        self.analytics = GatewayAnalytics(topology)
        self.outbox = {}            # command number (as text) -> command not yet confirmed by its node
        self.command_number = 0     # never repeats: a node acts on each number once
        self.told = {}              # node -> {alert code: its time}: sent as still going on
        self.told_pipe, self.told_cause = [], []    # nodes told of the active pipe leak / possible cause
        self.send_at, self.tries = {}, {}           # command number -> next time to send, sends so far
        self.rng = random.Random(0)
        self.airtime = AirtimeBudget()
        self.boot, self.beacon_at, self.dirty = None, None, False
        # Everything above except log and events survives a restart. The state file replaces
        # the older _seen.json, which is still read if it is all there is.
        path = getattr(hal, "storage_path", None)
        base = path and os.path.splitext(path)[0]
        self.state_path = base and base + "_state.json"
        self.resumed = False
        if self.state_path and os.path.exists(self.state_path):
            with open(self.state_path) as f:
                self.load_state(json.load(f))
        elif base and os.path.exists(base + "_seen.json"):
            with open(base + "_seen.json") as f:
                self._load_old_seen(json.load(f))

    def _load_old_seen(self, data):
        for n, v in data.items():
            if isinstance(v, dict):
                self.seen_floor[n] = int(v.get("floor", 0))
                self.seen_above[n] = set(v.get("above", []))
            else:                   # oldest format: a plain list
                self.seen_floor[n] = 0
                self.seen_above[n] = set(v)

    @property
    def alert_history_list(self):
        return [[*key, *value] for key, value in self.alert_history.items()]

    @alert_history_list.setter
    def alert_history_list(self, rows):
        self.alert_history = {(n, a): (t, count) for n, a, t, count in rows}

    def save_state(self):
        if not self.state_path:
            return
        state = {"version": STATE_VERSION}
        state.update({name: getattr(self, name) for name in SAVED})
        state.update({name: getattr(self, name) for name in BY_BLOCK})
        state.update(seen_above={n: sorted(v) for n, v in self.seen_above.items()},
                     balance_done=sorted(self.balance_done), reported=sorted(self.reported),
                     skewed=sorted(self.skewed), analytics=self.analytics.to_state())
        save_atomic(self.state_path, json.dumps(state))

    def load_state(self, state):
        ver = state.get("version", 1)
        if ver > STATE_VERSION:
            raise ValueError(f"Unsupported gateway state version {ver} (max {STATE_VERSION})")
        for name in SAVED:
            if name in state:       # a state file from an older version may lack some
                setattr(self, name, state[name])
        for name in BY_BLOCK:                       # JSON turned the block numbers into text
            setattr(self, name, {int(i): v for i, v in state.get(name, {}).items()})
        self.seen_above = {n: set(v) for n, v in state.get("seen_above", {}).items()}
        self.balance_done = set(state.get("balance_done", []))
        self.reported = set(state.get("reported", []))
        self.skewed = set(state.get("skewed", []))
        if "analytics" in state:
            self.analytics.load_state(state["analytics"])
        self._forget_removed_nodes()
        self.resumed = True

    def _forget_removed_nodes(self):
        """Saved state may name nodes that are no longer in the tree. Left in, one would count
        as silent for ever (pausing the building check) and its commands would never end."""
        members = self.tree.devices
        for node in set(self.silent) - members:
            self.analytics._resolve(node, "NODE_SILENT", reason="removed_from_topology")
            del self.silent[node]
        if self.last_seen is not None:
            self.last_seen = {n: t for n, t in self.last_seen.items() if n in members}
        self.outbox = {num: c for num, c in self.outbox.items() if c["n"] in members}
        self.told = {n: v for n, v in self.told.items() if n in members}
        self.told_pipe = [n for n in self.told_pipe if n in members]
        self.told_cause = [n for n in self.told_cause if n in members]

    def _is_seen_and_record(self, node, seq):
        """Return True if seq was already seen; otherwise record it (advancing contiguous floor)."""
        fl = self.seen_floor.get(node, 0)
        ab = self.seen_above.setdefault(node, set())
        if seq <= fl or seq in ab:
            return True
        ab.add(seq)
        while True:
            while (fl + 1) in ab:
                fl += 1
                ab.remove(fl)
            if len(ab) <= MAX_GAPS:
                break
            # A node that gave up old summaries (a very long time away) leaves gaps that will
            # never fill: close the oldest, so this cannot grow for ever
            fl = min(ab) - 1
        self.seen_floor[node] = fl
        return False

    def step(self):
        now = self.hal.now()
        utc_fn = getattr(self.hal, "utc_now", None)
        utc_s = utc_fn()[0] if utc_fn else now
        if self.boot is None:
            self.boot = int(utc_s)
        if self.last_seen is None:
            self.last_seen = {n: now for n in self.tree.devices}
        if self.resumed:            # the gateway was down, not the nodes: restart their silence timers
            self.resumed = False
            self.last_seen.update({n: now for n in self.tree.devices if n not in self.silent})
        acks, events_before = [], len(self.events)
        messages, bad = self.link.receive()                 # bad: failed authentication or broken packet
        for m in messages:
            if m["k"] == "GOT":
                self.confirmed(now, m)
                continue
            if m["k"] not in ("ALERT", "FLAG", "SUM"):
                continue
            if not self._is_seen_and_record(m["n"], m["s"]):
                self.hurry(now, m["n"])
                self.last_seen[m["n"]] = now    # a repeated or replayed packet is no sign of life
                m, bad_readings = readings.check(m)
                if bad_readings:
                    self.decide(now, "BAD_READING", node=m["n"], fields=bad_readings)
                self.log.append({**m, "rx": now, "rx_utc": round(utc_s), "rx_iso": iso_utc(utc_s)})
                self.analytics.on_packet(now, m)
                self.check_clock(now, m)
                if m["k"] == "ALERT" and m["a"] in VALVE_REPORTS:
                    self.decide(m["t"], m["a"], node=m["n"])
                elif m["k"] == "ALERT" and not m["a"].endswith("_CLEARED"):
                    self.check_repeat(m)
                    if self.analytics.is_stale(m["n"], category_of(m["a"]), m["t"]):
                        pass                    # already over: not evidence about the tank now
                    elif m["a"] == "ABNORMAL_SUPPLY":
                        self.tank_abnormal_supply_t = m["t"]
                        self.tank_abnormal_supply_recv_t = now
                    elif m["a"] in FLAT_LEAK_ALERTS:
                        self.flat_leak_t = m["t"]
                        self.flat_leak_recv_t = now
                elif m["k"] == "FLAG" and m["n"] == self.tank:
                    if m["f"] == "SUPPLY_RECOVERED":
                        self.supply_recovered(now, m)
                    elif m["t"] > self.supply_cleared_t:
                        self.tank_low_t = m["t"]
                        self.tank_low_recv_t = now
                    if "inflow" in m:
                        self.last_tank_inflow = m["inflow"]
                elif m["k"] == "SUM":
                    if "vol_l" in m:        # absent if the flow sensor gave nothing all block
                        self.sums.setdefault(m["i"], {})[m["n"]] = m["vol_l"]
                    if m["n"] == self.tank and "in_l" in m:
                        self.last_tank_inflow = m["in_l"] / 900.0
                    elif m["n"] in self.flats and "p_min" in m:
                        self.p_sums.setdefault(m["i"], {})[m["n"]] = m["p_min"]
                        if "p_mean" in m:
                            self.p_mean_sums.setdefault(m["i"], {})[m["n"]] = m["p_mean"]
            acks.append((m["n"], m["s"]))
        if bad:
            self.decide(now, "BAD_PACKET", count=bad)
        self.expire_orders(now)
        self.check_silence(now)
        self.supply_rule(now)
        self.mass_balance(now)
        # Save first, then transmit: a node deletes a message once acknowledged, so its effect
        # (and any command it led to) must already be on disk. (If the save fails, it is resent.)
        if acks or self.dirty or len(self.events) > events_before:
            self.save_state()
            self.dirty = False
        for n, seq in acks:
            ack = {"k": "ACK", "n": n, "s": seq, "gt": now}     # gt: nodes sync their clock
            self.transmit(now, ack)
        self.send_commands(now)
        if self.beacon_at is None or now >= self.beacon_at or now < self.beacon_at - BEACON_S:
            beacon = {"k": "BEACON", "n": "GATEWAY", "s": self.boot, "gt": now}
            self.transmit(now, beacon)
            self.beacon_at = now + BEACON_S
        return min([self.beacon_at] + [self.send_at.get(num, now) for num in self.outbox])

    # --- radio and dispatch ---
    def transmit(self, now, message):
        return self.link.send(message, admit=lambda s: self.airtime.take(now, s, True))

    def send_commands(self, now):
        """Unconfirmed commands that are due; ongoing problems go before ones already over."""
        for num, c in sorted(self.outbox.items(), key=lambda kv: (kv[1]["over"], kv[1]["t"], int(kv[0]))):
            if now < self.send_at.get(num, now):
                continue
            command = {"k": "CMD", "n": c["n"], "s": int(num), "t": c["t"], "a": c["a"], "over": c["over"]}
            if self.transmit(now, command):
                tries = self.tries.get(num, 0)
                self.send_at[num] = now + retry_wait(tries, True, c["n"] not in self.silent, self.rng)
                self.tries[num] = tries + 1
            else:
                self.send_at[num] = now + 60

    def hurry(self, now, node):
        """A node just spoke, so it can hear: send what is waiting for it now."""
        for num, c in self.outbox.items():
            if c["n"] == node and self.tries.get(num):
                self.send_at[num], self.tries[num] = now, 0

    def confirmed(self, now, m):
        num = str(m["s"])
        if self.outbox.get(num, {}).get("n") != m["n"]:
            return                  # not waiting for it (a repeat, or a replay): proves nothing
        del self.outbox[num]
        self.send_at.pop(num, None)
        self.tries.pop(num, None)
        self.dirty = True
        self.hurry(now, m["n"])
        self.last_seen[m["n"]] = now

    def command(self, now, node, code, t, over=0):
        self.command_number = max(self.command_number + 1, int(now))
        self.outbox[str(self.command_number)] = {"n": node, "a": code, "t": t, "over": over}
        self.dirty = True

    def order(self, node, code, t=None):
        """An operator's order to a node (order.py, or the dashboard). It replaces any not yet
        delivered order about the same thing. t: when the operator asked, if that was earlier; a
        node ignores an order older than what has happened to it since. ValueError: no such node or
        order, or a newer one about the same thing is waiting."""
        group = next((g for g in ORDER_GROUPS if code in g), None)
        if group is None or node not in self.tree.devices:
            raise ValueError(f"cannot send {code!r} to {node!r}")
        now = self.hal.now()
        t = now if t is None else min(t, now)
        waiting = [(num, c) for num, c in self.outbox.items() if c["n"] == node and c["a"] in group]
        if any(c["t"] > t for _, c in waiting):
            raise ValueError(f"a newer order to {node} is waiting")
        for num, c in waiting:
            if num in self.outbox:
                del self.outbox[num]
                self.send_at.pop(num, None)
                self.tries.pop(num, None)
        self.command(now, node, code, t)
        self.decide(now, "ORDER", node=node, order=code)

    def expire_orders(self, now):
        """A reopen order must not open a valve long after it was asked for."""
        for num, c in list(self.outbox.items()):
            if c["a"] == "VALVE_OPEN" and now - c["t"] > REOPEN_ORDER_S:
                del self.outbox[num]
                self.send_at.pop(num, None)
                self.tries.pop(num, None)
                self.decide(now, "ORDER_EXPIRED", node=c["n"], order=c["a"])

    def tell(self, now, node, code, t):
        """Tell a node a problem is going on (once, until it ends)."""
        active = self.told.setdefault(node, {})
        if code in SUPPLY_CODES:
            for other in SUPPLY_CODES:
                if other != code and active.pop(other, None) is not None:
                    self._mark_over(node, (other,), t)          # replaced by the newer state
        if code not in active:
            active[code] = t
            self.command(now, node, code, t)

    def _mark_over(self, node, codes, t_end):
        """Commands not yet delivered whose problem has ended go out marked over."""
        for num, c in list(self.outbox.items()):
            if c["n"] == node and c["a"] in codes and not c["over"] and c["t"] <= t_end:
                del self.outbox[num]
                self.send_at.pop(num, None)
                self.tries.pop(num, None)
                self.command(now=self.hal.now(), node=node, code=c["a"], t=c["t"], over=1)

    def tell_ended(self, now, node, clear_code, codes, t_end):
        active = self.told.get(node, {})
        ended = [code for code in codes if code in active and active[code] <= t_end]
        if ended:
            for code in ended:
                del active[code]
            self._mark_over(node, codes, t_end)
            self.command(now, node, clear_code, t_end)

    def pipe_leak_nodes(self, suspected):
        """The tank, and every flat the leak could cut off: those fed through the suspected
        junction. If the spot is unknown (None), the tank only."""
        junction = self.tree.labels.get(suspected)
        return sorted((self.tree.flats_below(junction) if junction else set()) | {self.tank})   # needs a tank

    def dispatch(self, now, ev):
        if not self.enable_commands:
            return
        kind = ev["event"]
        if kind == "PIPE_LEAK":
            self.told_pipe = self.pipe_leak_nodes(ev.get("suspected_floor"))
            for node in self.told_pipe:
                self.tell(now, node, "PIPE_LEAK", self.pipe_since if self.pipe_since is not None else ev["t"])
        elif kind == "PIPE_LEAK_PAST":                      # found only now, and already history
            for node in self.pipe_leak_nodes(ev.get("suspected_floor")):
                self.command(now, node, "PIPE_LEAK", ev["began_t"], over=1)
        elif kind in SUPPLY_CODES:
            self.tell(now, self.tank, kind, ev.get("event_t", ev["t"]))
        elif kind == "SUPPLY_RECOVERED":
            self.tell_ended(now, self.tank, "SUPPLY_CLEARED", SUPPLY_CODES, ev["event_t"])
        elif kind in ("NEW_ALERT", "REPEAT_ALERT") and ev.get("possible_cause") == "PIPE_LEAK":
            self.tell(now, ev["node"], "PIPE_LEAK_CAUSE", ev["t"])
            if ev["node"] not in self.told_cause:
                self.told_cause.append(ev["node"])

    def end_pipe_leak(self, now):
        for node in self.told_pipe:
            self.tell_ended(now, node, "PIPE_LEAK_CLEARED", ("PIPE_LEAK",), now)
        for node in self.told_cause:
            self.tell_ended(now, node, "PIPE_LEAK_CAUSE_CLEARED", ("PIPE_LEAK_CAUSE",), now)
        self.told_pipe, self.told_cause = [], []

    def supply_recovered(self, now, m):
        if m["t"] < self.tank_low_t:
            return                  # an old recovery arriving after a newer low-tank flag
        self.supply_rule(now)       # a low flag delivered together with this still gets decided
        self.supply_cleared_t = m["t"]
        self.tank_low_recv_t, self.supply_active_until, self.tank_drain_reported = -1e9, -1, False
        self.decide(now, "SUPPLY_RECOVERED", event_t=m["t"])

    def decide(self, now, kind, **info):
        if kind in ("PIPE_LEAK", "PIPE_LEAK_PAST"):
            junction = self.tree.labels.get(info.get("suspected_floor"))
            info.update(suspected_node=junction,        # recorded for future valve control only
                        isolation_valve=self.tree.valve_for(junction) if junction else None)
        ev = {"t": now, "event": kind, **info}
        ev["level"] = flags.event_level(ev)             # planning/valve_spec.md
        self.events.append(ev)
        self.analytics.on_event(now, ev)
        self.dispatch(self.hal.now(), ev)

    def snapshot(self, now=None):
        """Return a JSON-serializable operational snapshot for the dashboard."""
        return self.analytics.snapshot(self, now=now)

    def check_repeat(self, m):
        """Nodes re-send an alert each time a problem comes back; decide if this one is new."""
        key = (m["n"], m["a"])
        prev = self.alert_history.get(key)
        self.alert_history[key] = (m["t"], prev[1] + 1 if prev else 1)
        # "possibly": a real flat leak during a pipe leak is labelled too. An alert that arrives
        # before PIPE_LEAK is decided is not labelled; the time test skips older stored alerts.
        extra = {}
        if m["a"] == "LEAK_PRESSURE_DROP" and self.pipe_alerted and m["t"] >= self.pipe_since \
                and not self.analytics.is_stale(m["n"], "FLAT_LEAK", m["t"]):
            extra["possible_cause"] = "PIPE_LEAK"
        if prev:
            self.decide(m["t"], "REPEAT_ALERT", node=m["n"], alert=m["a"], count=prev[1] + 1,
                        since_last_min=round((m["t"] - prev[0]) / 60), **extra)
        else:
            self.decide(m["t"], "NEW_ALERT", node=m["n"], alert=m["a"], **extra)

    def check_clock(self, now, m):
        ahead = m["t"] - now
        if ahead > CLOCK_AHEAD_S and m["n"] not in self.skewed:
            self.skewed.add(m["n"])
            self.decide(now, "CLOCK_SKEW", node=m["n"], ahead_s=round(ahead))
        elif abs(ahead) <= CLOCK_AHEAD_S and m["n"] in self.skewed:     # fresh and on time
            self.skewed.discard(m["n"])
            self.decide(now, "CLOCK_OK", node=m["n"])

    def check_silence(self, now):
        for n, t in self.last_seen.items():
            if now - t >= SILENT_S and n not in self.silent:
                self.silent[n] = t
                self.decide(now, "NODE_SILENT", node=n, last_heard_min=round((now - t) / 60))
            elif now - t < SILENT_S and n in self.silent:
                self.decide(now, "NODE_BACK", node=n, silent_min=round((now - self.silent.pop(n)) / 60))

    def _cross_floor_drops(self, interval=None):
        """Compute per-floor pressure drop (m) relative to clean baseline (prefers p_mean over p_min)."""
        use_mean = bool(self.p_mean_sums and self.p_mean_base)
        sums = self.p_mean_sums if use_mean else self.p_sums
        base = self.p_mean_base if use_mean else self.p_base
        if not sums or not base:
            return {}
        i = interval if interval is not None else max(sums)
        cur = sums.get(i, {})
        by_floor = {}
        for f, p_now in cur.items():
            section = self.tree.section_of(f)       # the junction feeding the flat ("floor")
            if f in base and section is not None:
                by_floor.setdefault(section, []).append(base[f] - p_now)
        return {fl: round(sum(vs) / len(vs), 3) for fl, vs in by_floor.items() if vs}

    def _localize_floor(self, drops):
        """Confidence-aware localization using the downward headloss gradient along the pipes.
        -> (suspected section or None, sections to check, confidence). A section is the
        junction feeding some flats, named by its label.

        A leak at a junction increases flow (and thus headloss) in all pipe segments upstream
        of it, creating positive pressure-drop steps step[s] = drop[s] - drop[upstream of s]
        down to it, and ~0 step beyond it.
        """
        if not drops or max(drops.values()) < 0.05:
            return None, [], "unlocalised"
        if len(drops) == 1:
            only = next(iter(drops))
            return only, [only], "medium"

        above = {s: self.tree.upstream_sections(s) for s in drops}          # nearest first
        up = {s: next((a for a in above[s] if a in drops), None) for s in drops}
        steps = {s: round(drops[s] - drops[up[s]], 4) for s in drops if up[s] is not None}
        sig = [s for s in steps if steps[s] >= 0.022]
        deepest = [s for s in sig if not any(s in above[other] for other in sig)]
        if len(deepest) == 1:           # the furthest section with a significant headloss step
            suspected = deepest[0]
            return suspected, [suspected] + [s for s in drops if up[s] == suspected], "high"
        if not sig:
            # No significant step: either a large burst at a junction fed straight from the
            # tank (drop > 0.45 m) or a medium/small leak whose gradient is below burst noise.
            top = [s for s in drops if not above[s] and drops[s] >= 0.45]
            if len(top) == 1:
                return top[0], top + [s for s in drops if above[s][:1] == top], "high"
        # (steps on two separate branches also end up here: one leak cannot be both)
        ranked = sorted(drops.items(), key=lambda kv: -kv[1])
        return None, [s for s, _ in ranked[:2]], "unlocalised"

    def supply_rule(self, now):
        """Multi-signal supply classification: distinguishes supply interruption from leak-induced tank drain."""
        if now - self.tank_low_recv_t <= SUPPLY_WINDOW_S:
            leak_ev = (
                self.pipe_alerted
                or (now - self.tank_abnormal_supply_recv_t <= SUPPLY_WINDOW_S)
                or (abs(self.tank_abnormal_supply_t - self.tank_low_t) <= SUPPLY_WINDOW_S)
                or (now - self.flat_leak_recv_t <= SUPPLY_WINDOW_S)
                or (abs(self.flat_leak_t - self.tank_low_t) <= SUPPLY_WINDOW_S)
            )
            if leak_ev and not self.tank_drain_reported:
                self.tank_drain_reported = True
                self.decide(now, "LEAK_TANK_DRAIN", tank_low=True, inflow_lps=self.last_tank_inflow, event_t=self.tank_low_t)
                self.supply_active_until = now + SUPPLY_WINDOW_S
                return
            if now > self.supply_active_until:
                inflow_low = self.last_tank_inflow is not None and self.last_tank_inflow < 0.1
                inflow_active = self.last_tank_inflow is not None and self.last_tank_inflow >= 0.1
                drops = self._cross_floor_drops()
                uniform_drop = len(drops) >= 2 and sum(1 for d in drops.values() if d > 0.2) >= max(len(drops) - 1, 1)
                if inflow_low or (not inflow_active and uniform_drop):
                    self.decide(now, "SUPPLY_EVENT", tank_low=True, inflow_lps=self.last_tank_inflow, floor_drops=drops, event_t=self.tank_low_t)
                else:
                    self.decide(now, "SUPPLY_UNCERTAIN", tank_low=True, inflow_lps=self.last_tank_inflow, event_t=self.tank_low_t)
                self.supply_active_until = now + SUPPLY_WINDOW_S
        else:
            self.tank_drain_reported = False
        self.tank_abnormal_supply_recv_t, self.flat_leak_recv_t = -1e9, -1e9

    def mass_balance(self, now):
        # One side only, or a tank that also feeds homes with no node: nothing to compare. All of
        # that water would otherwise look like a pipe leak
        if not (self.tank and self.flats) or self.tree.unmetered_homes:
            self._forget_old_blocks()
            return
        if self.silent and not self.balance_paused:
            self.balance_paused = True
            self.decide(now, "BALANCE_PAUSED", silent=sorted(self.silent))
        elif not self.silent:
            self.balance_paused = False
        for i in sorted(self.sums):
            got = self.sums[i]
            if i in self.balance_done or self.tank not in got or not self.flats <= set(got):
                continue                                    # wait until every node's summary for i arrived
            self.balance_done.add(i)
            supply = got[self.tank]
            if supply < MIN_SUPPLY_L:
                continue
            err = 100 * (supply - sum(got[n] for n in self.flats)) / supply
            self.analytics.last_balance_pct = round(err, 2)
            leaking = err > MASS_BALANCE_PCT
            if not leaking and not self.pipe_alerted and i in self.p_sums:
                for f, p_v in self.p_sums[i].items():
                    self.p_base[f] = p_v if f not in self.p_base else 0.9 * self.p_base[f] + 0.1 * p_v
                for f, p_v in self.p_mean_sums.get(i, {}).items():
                    self.p_mean_base[f] = p_v if f not in self.p_mean_base else 0.9 * self.p_mean_base[f] + 0.1 * p_v
            self.verdicts[i] = (leaking, round(err, 1))
            self._extend_leak_back()
            # Only two blocks next to each other in time decide anything, in whatever order
            # their summaries arrived
            for first in (i - 1, i):
                if first in self.verdicts and first + 1 in self.verdicts \
                        and self.verdicts[first][0] == self.verdicts[first + 1][0]:
                    self._judge_pair(now, first, self.verdicts[first][0])
        self._forget_old_blocks()

    def _extend_leak_back(self):
        """Late summaries can show that the active leak began earlier than first known."""
        if not self.pipe_alerted:
            return
        while self.verdicts.get(self.pipe_first - 1, (False,))[0]:
            self.pipe_first -= 1
            self.reported.add(self.pipe_first)
        self.pipe_since = self.pipe_first * SUMMARY_S

    def _judge_pair(self, now, first, leaking):
        newest = first >= self.last_pair
        self.last_pair = max(first, self.last_pair)
        if not leaking:
            if newest and self.pipe_alerted:
                self.pipe_alerted = False                   # re-arm: a later leak alerts again
                self.analytics.clear_pipe_leak(t_resolved=now)
                self.end_pipe_leak(now)
            return
        pair, error_pct = {first, first + 1}, self.verdicts[first + 1][1]
        if newest and not self.pipe_alerted:
            self.pipe_alerted, self.pipe_first = True, first
            self.reported |= pair
            self._extend_leak_back()
            drops = self._cross_floor_drops(first + 1)
            suspected, floor_range, conf = self._localize_floor(drops)
            self.decide(now, "PIPE_LEAK", interval=first + 1, error_pct=error_pct,
                        suspected_floor=suspected, suspected_range=floor_range,
                        localization_confidence=conf, floor_drops=drops)
            return
        if not newest and not pair & self.reported:         # an earlier leak, known only now
            suspected, floor_range, conf = self._localize_floor(self._cross_floor_drops(first + 1))
            self.decide(now, "PIPE_LEAK_PAST", interval=first + 1, error_pct=error_pct,
                        began_t=first * SUMMARY_S, suspected_floor=suspected, suspected_range=floor_range,
                        localization_confidence=conf)
        self.reported |= pair

    def _forget_old_blocks(self):
        if not self.sums:
            return
        oldest = max(self.sums) - KEEP_BLOCKS
        for data in (self.sums, self.p_sums, self.p_mean_sums, self.verdicts):
            for i in [i for i in data if i < oldest]:
                del data[i]
        self.balance_done = {i for i in self.balance_done if i >= oldest}
        self.reported = {i for i in self.reported if i >= oldest}


if __name__ == "__main__":
    import tempfile
    from hal.packet import sim_key

    class FakeHAL:
        storage_path = None
        key = staticmethod(sim_key)

        def __init__(self):
            self.t, self.box, self.seq, self.sent = 0, [], 0, []

        def now(self):
            return self.t

        def receive(self):
            msgs, self.box = self.box, []
            return msgs

        def send(self, raw, cad=False):
            self.sent.append(raw)

    def block(gw, i, vols):
        """Deliver one 15-minute block of summaries {node: litres}, then step the gateway 15 times."""
        for n, v in vols.items():
            gw.hal.seq += 1
            m = {"k": "SUM", "n": n, "s": gw.hal.seq, "t": gw.hal.t, "i": i, "vol_l": v}
            gw.hal.box.append(pack(m, sim_key(n)))
        for _ in range(15):
            gw.step()
            gw.hal.t += 60

    events = lambda gw: [e["event"] for e in gw.events]
    F1, F2, T = "B-101", "B-102", "B-TANK"
    house = Topology.load()
    gw = Gateway(FakeHAL(), house.only([F1, F2]))
    ok, leak = {T: 100, F1: 50, F2: 50}, {T: 100, F1: 40, F2: 40}
    for i, v in enumerate([ok, leak, leak, ok, ok, leak, leak]):     # leak, fixed, second leak
        block(gw, i, v)
    assert events(gw) == ["PIPE_LEAK", "PIPE_LEAK"], events(gw)
    # A flat pressure alert is labelled only during a pipe leak and only if stamped after it began
    gw4 = Gateway(FakeHAL(), house.only([F1, F2]))

    def label(t, alert="LEAK_PRESSURE_DROP"):
        gw4.hal.seq += 1
        m = {"k": "ALERT", "n": F1, "s": 500 + gw4.hal.seq, "t": t, "a": alert}
        gw4.hal.box.append(pack(m, sim_key(F1)))
        gw4.step()
        return [e for e in gw4.events if e.get("alert") == alert][-1].get("possible_cause")

    assert label(0) is None                                   # no pipe leak
    for i, v in enumerate([ok, leak, leak]):
        block(gw4, i, v)
    assert gw4.pipe_since == 900                              # block 1 was the first leaking one
    assert label(gw4.hal.t) == "PIPE_LEAK"                    # also covers REPEAT_ALERT
    assert label(899) is None                                 # older alert arriving late
    assert label(gw4.hal.t, "LEAK_NIGHT_FLOW") is None        # flow-only checks are not affected
    for i, v in enumerate([ok, ok], start=3):
        block(gw4, i, v)
    assert label(gw4.hal.t) is None                           # pipe check re-armed

    # Dispatch. Pipe leak with no pressure data: spot unknown, so the tank only; repeated until
    # confirmed; ended by the balance check; a confirmation from the wrong node proves nothing
    def commands(gw, since=0):
        out = []
        for raw in gw.hal.sent[since:]:
            m = unpack(raw, sim_key)
            if m["k"] == "CMD":
                out.append((m["n"], m["a"], m["over"], m["s"]))
        return out

    def deliver(gw, *packets):
        gw.hal.box = [pack(m, sim_key(m["n"])) for m in packets]
        gw.step()

    gw5 = Gateway(FakeHAL(), house.only([F1, F2]))
    gw5.step()
    assert unpack(gw5.hal.sent[0], sim_key) == {"k": "BEACON", "n": "GATEWAY", "s": 0, "gt": 0}, "beacon at startup"
    for i, v in enumerate([ok, leak, leak]):
        block(gw5, i, v)
    first = {(n, a) for n, a, _, _ in commands(gw5)}
    assert first == {(T, "PIPE_LEAK")}, first
    assert gw5.events[-1]["suspected_node"] is None and gw5.events[-1]["isolation_valve"] is None
    number = commands(gw5)[0][3]
    gw5.hal.t += 10
    deliver(gw5, {"k": "GOT", "n": F2, "s": number, "t": gw5.hal.t})
    assert len(gw5.outbox) == 1, "wrong node"
    deliver(gw5, {"k": "GOT", "n": T, "s": number, "t": gw5.hal.t})
    assert not gw5.outbox
    seen = len(gw5.hal.sent)
    for i in (3, 4):
        block(gw5, i, ok)
    assert {(n, a, o) for n, a, o, _ in commands(gw5, seen)} == {(T, "PIPE_LEAK_CLEARED", 0)}

    # Located from the pressure summaries: the tank and every flat fed through that junction
    def pressure_leak(tree, drops):
        """Two quiet blocks, then two with water missing and each section's pressure down by drops."""
        gw = Gateway(FakeHAL(), tree)
        share = {f: 4.0 for f in tree.flats}
        for i in range(4):
            leaking = i >= 2
            for n in sorted(tree.flats) + [tree.tank]:
                gw.hal.seq += 1
                m = {"k": "SUM", "n": n, "s": gw.hal.seq, "t": gw.hal.t, "i": i}
                if n == tree.tank:
                    m["vol_l"] = 4.0 * len(tree.flats) * (1.4 if leaking else 1.0)
                else:
                    p = 10.0 - (drops[tree.section_of(n)] if leaking else 0.0)
                    m.update(vol_l=share[n], p_min=p, p_mean=p)
                gw.hal.box.append(pack(m, sim_key(n)))
            gw.step()
            gw.hal.t += 900
        ev = [e for e in gw.events if e["event"] == "PIPE_LEAK"][0]
        return ev, {c["n"] for c in gw.outbox.values() if c["a"] == "PIPE_LEAK"}

    on = lambda floors: {f for f in house.flats if f[2] in floors}
    ev, told = pressure_leak(house, {"4": .514, "3": .558, "2": .617, "1": .657, "G": .663})    # at D1
    assert ev["suspected_floor"] == "1" and ev["suspected_range"] == ["1", "G"], ev
    assert (ev["suspected_node"], ev["isolation_valve"]) == ("B-D1", T)
    assert told == on("1G") | {T}, "floors 2 to 4 are upstream of the leak"
    ev, told = pressure_leak(house, {"4": .51, "3": .501, "2": .51, "1": .502, "G": .508})       # at D4
    assert ev["suspected_floor"] == "4" and told == house.flats | {T}
    ev, told = pressure_leak(house, {"4": .295, "3": .286, "2": .295, "1": .287, "G": .293})
    assert ev["suspected_floor"] is None and told == {T}
    ev, told = pressure_leak(house, {"4": .1, "3": .1, "2": .1, "1": .1, "G": .2})
    assert ev["suspected_floor"] == "G" and told == on("G") | {T}

    # A building whose pipe splits in two below the tank
    def entry(name, kind, *children, **more):
        return {"id": name, "type": kind, "valve": kind != "junction",
                **({"children": list(children)} if children else {}), **more}

    def branch(label, *flats, valve=False, below=()):
        e = entry(f"J-{label}", "junction", *(entry(f, "flat") for f in flats), *below, label=label)
        return {**e, "valve": valve}

    forked = Topology({"schema_version": 1, "building": "forked", "root": entry(
        T, "tank",
        branch("east", "B-401", "B-402", valve=True, below=[branch("east-low", "B-301")]),
        branch("west", "B-201", below=[branch("west-low", "B-101")]))})
    quiet = {"east": .1, "east-low": .1, "west": .1, "west-low": .1}
    ev, told = pressure_leak(forked, {**quiet, "east-low": .2})
    assert told == {"B-301", T} and ev["isolation_valve"] == "J-east", (ev, told)    # nearest valve above
    ev, told = pressure_leak(forked, {**quiet, "east-low": .2, "west-low": .2})
    assert ev["suspected_floor"] is None and told == {T}, "two branches: spot unknown"
    ev, told = pressure_leak(forked, {**quiet, "east": .5, "east-low": .5})
    assert ev["suspected_floor"] == "east" and told == {"B-401", "B-402", "B-301", T}, (ev, told)

    # A flat taken out of the tree between restarts leaves nothing behind in the saved state
    hal8 = FakeHAL()
    hal8.storage_path = os.path.join(tempfile.mkdtemp(), "GW8.jsonl")
    gw8 = Gateway(hal8, house.only([F1, F2]))
    gw8.step()
    hal8.t = SILENT_S + 60
    gw8.tell(hal8.t, F2, "PIPE_LEAK", hal8.t)
    deliver(gw8, {"k": "SUM", "n": F1, "s": 1, "t": hal8.t, "i": 3, "vol_l": 1.0},
            {"k": "SUM", "n": T, "s": 1, "t": hal8.t, "i": 3, "vol_l": 1.0})
    assert F2 in gw8.silent and gw8.outbox
    gw8 = Gateway(hal8, house.only([F1]))
    gw8.step()
    assert not gw8.silent and not gw8.outbox and F2 not in gw8.last_seen
    assert (F2, "NODE_SILENT") not in gw8.analytics.incidents
    assert not gw8.balance_paused

    # Supply states go to the tank; one replaces another; over before delivery: sent as over.
    # The gateway restarts with it undelivered, and still delivers
    hal7 = FakeHAL()
    hal7.storage_path = os.path.join(tempfile.mkdtemp(), "GW7.jsonl")
    hal7.t = 1000
    gw7 = Gateway(hal7, house.only([F1]))
    deliver(gw7, {"k": "FLAG", "n": T, "s": 1, "t": 1000, "f": "SUPPLY_LOW", "level": 0.4, "inflow": 0.0})
    assert [(c["n"], c["a"], c["over"]) for c in gw7.outbox.values()] == [(T, "SUPPLY_EVENT", 0)]
    hal7.t = 1060
    gw7.decide(1060, "LEAK_TANK_DRAIN", event_t=1030)
    assert sorted((c["a"], c["over"]) for c in gw7.outbox.values()) == [("LEAK_TANK_DRAIN", 0), ("SUPPLY_EVENT", 1)]
    deliver(gw7, {"k": "FLAG", "n": T, "s": 2, "t": 1050, "f": "SUPPLY_RECOVERED", "level": 1.5, "inflow": 0.3})
    waiting = sorted((c["a"], c["over"]) for c in gw7.outbox.values())
    assert waiting == [("LEAK_TANK_DRAIN", 1), ("SUPPLY_CLEARED", 0), ("SUPPLY_EVENT", 1)], waiting
    hal7.sent.clear()
    gw7 = Gateway(hal7, house.only([F1]))
    gw7.step()
    assert len(commands(gw7)) == 3 and unpack(hal7.sent[-1], sim_key)["k"] == "BEACON"

    # A flat's pressure alert during a pipe leak: that flat is told the likely cause
    assert any(c["n"] == F1 and c["a"] == "PIPE_LEAK_CAUSE" for c in gw4.outbox.values())

    # F2 goes silent for 4 blocks (60 min): alert and pause; it comes back with its stored
    # summaries and the late blocks are checked
    gw = Gateway(FakeHAL(), house.only([F1, F2]))
    for i in range(4):
        block(gw, i, {T: 100, F1: 10})
    assert events(gw) == ["NODE_SILENT", "BALANCE_PAUSED"], events(gw)
    assert all(i not in gw.balance_done for i in range(4))
    block(gw, 4, {F2: 90, **{k: v for k, v in ok.items() if k != F2}})
    for i in range(4):
        block(gw, i, {F2: 90})
    assert "NODE_BACK" in events(gw) and all(i in gw.balance_done for i in range(5)), events(gw)
    assert "PIPE_LEAK" not in events(gw)
    # a message stamped 5 minutes in the future: bad node clock
    gw.hal.box.append(pack({"k": "ALERT", "n": F1, "s": 999, "t": gw.hal.t + 300, "a": "LEAK_NIGHT_FLOW"}, sim_key(F1)))
    gw.step()
    assert "CLOCK_SKEW" in events(gw)

    # A summary with no volume (flow sensor dead all block) is logged; that block is not balanced
    gw3 = Gateway(FakeHAL(), house.only([F1, F2]))
    block(gw3, 0, {T: 100, F1: 50})
    gw3.hal.box = [pack({"k": "SUM", "n": F2, "s": 900, "t": gw3.hal.t, "i": 0, "p_min": 10.0}, sim_key(F2))]
    gw3.step()
    assert len(gw3.log) == 3 and 0 not in gw3.balance_done and "PIPE_LEAK" not in events(gw3)

    # Packet checks: a forged alert is dropped; a replayed one is acknowledged but not used twice,
    # even after the gateway restarts; the ACK goes back signed with the node's key
    hal = FakeHAL()
    hal.storage_path = os.path.join(tempfile.mkdtemp(), "GATEWAY.jsonl")
    gw = Gateway(hal, house.only([F1]))
    real = pack({"k": "ALERT", "n": F1, "s": 1, "t": 0, "a": "LEAK_NIGHT_FLOW", "flow": 0.05}, sim_key(F1))
    hal.box = [pack({"k": "ALERT", "n": F1, "s": 2, "t": 0, "a": "LEAK_NIGHT_FLOW"}, b"attacker-guessed"), real]
    gw.step()
    assert len(gw.log) == 1 and sorted(events(gw)) == ["BAD_PACKET", "NEW_ALERT"], events(gw)
    acks = [m for m in (unpack(raw, sim_key) for raw in hal.sent) if m["k"] == "ACK"]
    assert acks == [{"k": "ACK", "n": F1, "s": 1, "gt": 0}]
    gw = Gateway(hal, house.only([F1]))                                           # gateway restarts
    hal.box = [real]                                                     # someone replays the alert
    gw.step()
    assert gw.log == [] and "NEW_ALERT" not in events(gw), "replay after restart must be ignored"

    # Out-of-order backlog after >1000-message gap: s=1500 arrives first, then old buffered s=2 arrives
    hal2 = FakeHAL()
    hal2.storage_path = os.path.join(tempfile.mkdtemp(), "GW2.jsonl")
    gw2 = Gateway(hal2, house.only([F1]))
    hal2.box = [pack({"k": "ALERT", "n": F1, "s": 1500, "t": 9000, "a": "LEAK_NIGHT_FLOW"}, sim_key(F1))]
    gw2.step()
    hal2.box = [pack({"k": "SUM", "n": F1, "s": 2, "t": 900, "i": 1, "vol_l": 12.0, "p_min": 10.0}, sim_key(F1))]
    gw2.step()
    assert [m["s"] for m in gw2.log] == [1500, 2], "old out-of-order summary after >1000 gap must NOT be dropped"
    print("gateway self-check passed")
