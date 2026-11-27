"""Aqua Edge gateway program (one per building). Runs on the laptop; an ESP8266 board on USB
serial is only the interface to the LoRa module (one SX1278, 433 MHz: lora433.py).

Call step() whenever a packet arrives and at the time it returns (at least every 0.5 s).

Nodes decide on their own what they can see. The gateway combines the data of many nodes, which
some detections need (a pipe leak shows only when every flat's litres are set against the tank's).

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
  8. Packet check (packet.py): a packet with a wrong signature is dropped (BAD_PACKET), and a
     (node, counter) pair already seen is a duplicate or a replay: acknowledged, not used again.
     ACKs are signed with the node's key, so a fake ACK cannot make a node delete its messages.
  9. Ranking of open problems for the dashboard: gateway_analytics.py.
 10. Beacon every 5 s (and at startup), so nodes know when to send what they stored.
 11. Dispatch: what only the gateway can know is sent back at once to the nodes concerned, as a
     signed CMD, repeated every few seconds until the node confirms it (GOT):
       pipe leak (and its end)  -> the tank and the flats on the suspected floors;
                                   every node if the floor cannot be told
       supply event / drain / uncertain (and recovery) -> the tank
       a flat's pressure alert during a pipe leak -> that flat (PIPE_LEAK_CAUSE: its own leak
                                   alert may not be its own leak)
     One that ended before it could be delivered still goes, marked "over".
     On by default; enable_commands=False keeps these results on the gateway only.
What the checks need is saved to one state file before any ACK is sent, so a restart keeps the
open problems and pipe check, and does not reopen the door to replays. Not saved: the in-memory
message log and event list. The gateway clock must carry on across a restart.
How the leak checks fit together: leak_detection/README.md.
"""
import json
import os
import random
import sys

_GW_DIR = os.path.dirname(os.path.abspath(__file__))
_FW_DIR = os.path.dirname(_GW_DIR)
for _d in (_GW_DIR, os.path.join(_FW_DIR, "edge_node"), os.path.join(_FW_DIR, "communication")):
    if _d not in sys.path:
        sys.path.append(_d)

from aqua_edge import SUMMARY_S, iso_utc, save_atomic
from gateway_analytics import FLAT_LEAK_ALERTS, GatewayAnalytics, category_of
from lora433 import BEACON_S, AirtimeBudget, airtime_s, retry_wait
from packet import pack, unpack

SUPPLY_WINDOW_S = 900          # a SUPPLY_LOW flag from the tank counts for this long
MASS_BALANCE_PCT = 10.0        # Ali et al. [6] threshold
MASS_BALANCE_INTERVALS = 2     # blocks in a row above threshold before alerting, and normal before re-arming
MIN_SUPPLY_L = 5.0             # ignore near-empty blocks (division by tiny numbers)
SILENT_S = 2700                # nothing heard from a node for 3 summary periods = silent
CLOCK_AHEAD_S = 60             # message time this far ahead of the gateway clock = bad node clock
SUPPLY_CODES = ("SUPPLY_EVENT", "LEAK_TANK_DRAIN", "SUPPLY_UNCERTAIN")
KEEP_BLOCKS = 7 * 86400 // SUMMARY_S    # per-block data kept: 7 days, for late summaries
STATE_VERSION = 1
# saved to disk as they are; the others need converting for JSON (save_state / load_state)
SAVED = ("seen_floor", "alert_history_list", "tank_low_t", "tank_low_recv_t", "supply_active_until",
         "tank_drain_reported", "tank_abnormal_supply_recv_t", "flat_leak_recv_t", "tank_abnormal_supply_t",
         "flat_leak_t", "last_tank_inflow", "supply_cleared_t", "p_base", "p_mean_base", "last_pair",
         "pipe_first", "pipe_alerted", "pipe_since", "balance_paused", "last_seen", "silent",
         "outbox", "command_number", "told", "told_pipe", "told_cause")
BY_BLOCK = ("sums", "p_sums", "p_mean_sums", "verdicts")     # {block number: value}


class Gateway:
    def __init__(self, hal, flat_ids, tank_id, enable_commands=True):
        self.hal, self.flats, self.tank = hal, set(flat_ids), tank_id
        self.enable_commands = enable_commands
        self.seen_floor = {}        # node -> highest contiguous seq received (1..floor)
        self.seen_above = {}        # node -> set of out-of-order seq > floor
        self.log = []               # every unique message received, with receive time
        self.events = []            # gateway's own decisions
        self.alert_history = {}     # (node, alert) -> (time of latest, how many so far)
        self.tank_low_t, self.tank_low_recv_t, self.supply_active_until = -1e9, -1e9, -1
        self.tank_drain_reported = False
        self.tank_abnormal_supply_recv_t, self.flat_leak_recv_t = -1e9, -1e9
        self.tank_abnormal_supply_t, self.flat_leak_t = -1e9, -1
        self.last_tank_inflow = None
        self.sums = {}              # interval -> {node: volume}
        self.p_sums = {}            # interval -> {flat: p_min}
        self.p_base = {}            # flat -> clean baseline p_min (EWMA)
        self.p_mean_sums = {}       # interval -> {flat: p_mean}
        self.p_mean_base = {}       # flat -> clean baseline p_mean (EWMA)
        self.supply_cleared_t = -1e9    # tank time of the latest SUPPLY_RECOVERED
        self.balance_done = set()
        self.verdicts = {}          # interval -> (leaking, error %) of each block balanced
        self.reported = set()       # leaking blocks already covered by a PIPE_LEAK(_PAST)
        self.last_pair = -1         # first block of the newest pair of equal verdicts
        self.pipe_alerted = self.balance_paused = False
        self.pipe_first = None      # first block of the active pipe leak
        self.pipe_since = None      # its start time
        self.last_seen = None       # node -> time last heard (set on the first step)
        self.silent = {}            # node -> time last heard before it went silent
        self.skewed = set()
        self.analytics = GatewayAnalytics(self.flats, self.tank)
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
        self.resumed = True

    def _is_seen_and_record(self, node, seq):
        """Return True if seq was already seen; otherwise record it (advancing contiguous floor)."""
        fl = self.seen_floor.get(node, 0)
        ab = self.seen_above.setdefault(node, set())
        if seq <= fl or seq in ab:
            return True
        ab.add(seq)
        while (fl + 1) in ab:
            fl += 1
            ab.remove(fl)
        self.seen_floor[node] = fl
        return False

    def step(self):
        now = self.hal.now()
        utc_fn = getattr(self.hal, "utc_now", None)
        utc_s = utc_fn()[0] if utc_fn else now
        if self.boot is None:
            self.boot = int(utc_s)
        if self.last_seen is None:
            self.last_seen = {n: now for n in self.flats | {self.tank}}
        if self.resumed:            # the gateway was down, not the nodes: restart their silence timers
            self.resumed = False
            self.last_seen.update({n: now for n in self.flats | {self.tank} if n not in self.silent})
        bad, acks, events_before = 0, [], len(self.events)
        for raw in self.hal.receive():
            m = unpack(raw, self.hal.key)
            if m is None:
                bad += 1                                    # wrong signature or broken packet
                continue
            if m["k"] == "GOT":
                self.confirmed(now, m)
                continue
            if m["k"] not in ("ALERT", "FLAG", "SUM"):
                continue
            if not self._is_seen_and_record(m["n"], m["s"]):
                self.hurry(now, m["n"])
                self.last_seen[m["n"]] = now    # a repeated or replayed packet is no sign of life
                self.log.append({**m, "rx": now, "rx_utc": round(utc_s), "rx_iso": iso_utc(utc_s)})
                self.analytics.on_packet(now, m)
                self.check_clock(now, m)
                if m["k"] == "ALERT" and not m["a"].endswith("_CLEARED"):
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
            self.transmit(now, pack(ack, self.hal.key(n)))
        self.send_commands(now)
        if self.beacon_at is None or now >= self.beacon_at or now < self.beacon_at - BEACON_S:
            beacon = {"k": "BEACON", "n": "GATEWAY", "s": self.boot, "gt": now}
            self.transmit(now, pack(beacon, self.hal.key("GATEWAY")))
            self.beacon_at = now + BEACON_S
        return min([self.beacon_at] + [self.send_at.get(num, now) for num in self.outbox])

    # --- radio and dispatch ---
    def transmit(self, now, raw):
        if not self.airtime.take(now, airtime_s(len(raw)), True):
            return False
        self.hal.send(raw, cad=True)
        return True

    def send_commands(self, now):
        """Unconfirmed commands that are due; ongoing problems go before ones already over."""
        for num, c in sorted(self.outbox.items(), key=lambda kv: (kv[1]["over"], kv[1]["t"], int(kv[0]))):
            if now < self.send_at.get(num, now):
                continue
            raw = pack({"k": "CMD", "n": c["n"], "s": int(num), "t": c["t"], "a": c["a"], "over": c["over"]},
                       self.hal.key(c["n"]))
            if self.transmit(now, raw):
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

    def pipe_leak_nodes(self, floor_range, confidence):
        if confidence == "unlocalised" or not floor_range:
            return sorted(self.flats | {self.tank})
        return sorted({f for f in self.flats if self._floor_of(f) in floor_range} | {self.tank})

    def dispatch(self, now, ev):
        if not self.enable_commands:
            return
        kind = ev["event"]
        if kind == "PIPE_LEAK":
            self.told_pipe = self.pipe_leak_nodes(ev.get("suspected_range"), ev.get("localization_confidence"))
            for node in self.told_pipe:
                self.tell(now, node, "PIPE_LEAK", self.pipe_since if self.pipe_since is not None else ev["t"])
        elif kind == "PIPE_LEAK_PAST":                      # found only now, and already history
            for node in self.pipe_leak_nodes(ev["suspected_range"], ev["localization_confidence"]):
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
        ev = {"t": now, "event": kind, **info}
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

    def _floor_of(self, flat):
        # e.g. "B-203" -> "2", "B-G01" -> "G"
        part = flat.split("-")[1]
        return "G" if part.startswith("G") else part[0]

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
            if f in base:
                by_floor.setdefault(self._floor_of(f), []).append(base[f] - p_now)
        return {fl: round(sum(vs) / len(vs), 3) for fl, vs in by_floor.items() if vs}

    def _localize_floor(self, drops):
        """Confidence-aware floor localization using downward riser headloss gradient.

        A leak on floor k increases flow (and thus headloss) in all riser segments
        above k (4 -> ... -> k), creating positive downward pressure-drop steps
        step[f] = drop[f] - drop[f_above] down to k, and ~0 step below k.
        """
        if not drops:
            return None, [], "unlocalised"
        order = [f for f in ("4", "3", "2", "1", "G") if f in drops]
        if not order or max(drops.values()) < 0.05:
            return None, [], "unlocalised"
        if len(order) == 1:
            return order[0], [order[0]], "medium"

        steps = {order[idx]: round(drops[order[idx]] - drops[order[idx - 1]], 4) for idx in range(1, len(order))}
        sig_floors = [f for f in order[1:] if steps[f] >= 0.022]
        if sig_floors:
            suspected = sig_floors[-1]  # lowest floor with significant downward headloss step
            idx = order.index(suspected)
            rng = [suspected] if idx == len(order) - 1 else [suspected, order[idx + 1]]
            return suspected, rng, "high"
        # No significant inter-floor step: either top-floor large burst (drop[4] > 0.45 m)
        # or a medium/small leak whose inter-floor gradient is below 15-min burst noise.
        if order[0] == "4" and drops["4"] >= 0.45:
            return "4", ["4", "3"] if "3" in drops else ["4"], "high"
        ranked = sorted(drops.items(), key=lambda kv: -kv[1])
        return None, [f for f, _ in ranked[:2]], "unlocalised"

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
            _, floor_range, conf = self._localize_floor(self._cross_floor_drops(first + 1))
            self.decide(now, "PIPE_LEAK_PAST", interval=first + 1, error_pct=error_pct,
                        began_t=first * SUMMARY_S, suspected_range=floor_range, localization_confidence=conf)
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
    from packet import sim_key

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
    gw = Gateway(FakeHAL(), [F1, F2], T)
    ok, leak = {T: 100, F1: 50, F2: 50}, {T: 100, F1: 40, F2: 40}
    for i, v in enumerate([ok, leak, leak, ok, ok, leak, leak]):     # leak, fixed, second leak
        block(gw, i, v)
    assert events(gw) == ["PIPE_LEAK", "PIPE_LEAK"], events(gw)
    # A flat pressure alert is labelled only during a pipe leak and only if stamped after it began
    gw4 = Gateway(FakeHAL(), [F1, F2], T)

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

    # Dispatch. Pipe leak with no pressure data: floor unknown, so every node; repeated until
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

    gw5 = Gateway(FakeHAL(), [F1, F2], T)
    gw5.step()
    assert unpack(gw5.hal.sent[0], sim_key) == {"k": "BEACON", "n": "GATEWAY", "s": 0, "gt": 0}, "beacon at startup"
    for i, v in enumerate([ok, leak, leak]):
        block(gw5, i, v)
    first = {(n, a) for n, a, _, _ in commands(gw5)}
    assert first == {(n, "PIPE_LEAK") for n in (F1, F2, T)}, first
    mine = {n: num for n, _, _, num in commands(gw5)}
    gw5.hal.t += 10
    deliver(gw5, {"k": "GOT", "n": F2, "s": mine[F1], "t": gw5.hal.t})
    assert len(gw5.outbox) == 3, "wrong node"
    deliver(gw5, *({"k": "GOT", "n": n, "s": num, "t": gw5.hal.t} for n, num in mine.items()))
    assert not gw5.outbox
    seen = len(gw5.hal.sent)
    for i in (3, 4):
        block(gw5, i, ok)
    assert {(n, a, o) for n, a, o, _ in commands(gw5, seen)} == {(n, "PIPE_LEAK_CLEARED", 0) for n in (F1, F2, T)}

    # Floor known: only the tank and those floors
    gw6 = Gateway(FakeHAL(), ["B-401", "B-301", "B-101"], T)
    gw6.decide(0, "PIPE_LEAK", suspected_floor="3", suspected_range=["3"], localization_confidence="high")
    assert sorted(c["n"] for c in gw6.outbox.values()) == ["B-301", T]

    # Supply states go to the tank; one replaces another; over before delivery: sent as over.
    # The gateway restarts with it undelivered, and still delivers
    hal7 = FakeHAL()
    hal7.storage_path = os.path.join(tempfile.mkdtemp(), "GW7.jsonl")
    hal7.t = 1000
    gw7 = Gateway(hal7, [F1], T)
    deliver(gw7, {"k": "FLAG", "n": T, "s": 1, "t": 1000, "f": "SUPPLY_LOW", "level": 0.4, "inflow": 0.0})
    assert [(c["n"], c["a"], c["over"]) for c in gw7.outbox.values()] == [(T, "SUPPLY_EVENT", 0)]
    hal7.t = 1060
    gw7.decide(1060, "LEAK_TANK_DRAIN", event_t=1030)
    assert sorted((c["a"], c["over"]) for c in gw7.outbox.values()) == [("LEAK_TANK_DRAIN", 0), ("SUPPLY_EVENT", 1)]
    deliver(gw7, {"k": "FLAG", "n": T, "s": 2, "t": 1050, "f": "SUPPLY_RECOVERED", "level": 1.5, "inflow": 0.3})
    waiting = sorted((c["a"], c["over"]) for c in gw7.outbox.values())
    assert waiting == [("LEAK_TANK_DRAIN", 1), ("SUPPLY_CLEARED", 0), ("SUPPLY_EVENT", 1)], waiting
    hal7.sent.clear()
    gw7 = Gateway(hal7, [F1], T)
    gw7.step()
    assert len(commands(gw7)) == 3 and unpack(hal7.sent[-1], sim_key)["k"] == "BEACON"

    # A flat's pressure alert during a pipe leak: that flat is told the likely cause
    assert any(c["n"] == F1 and c["a"] == "PIPE_LEAK_CAUSE" for c in gw4.outbox.values())

    # F2 goes silent for 4 blocks (60 min): alert and pause; it comes back with its stored
    # summaries and the late blocks are checked
    gw = Gateway(FakeHAL(), [F1, F2], T)
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
    gw3 = Gateway(FakeHAL(), [F1, F2], T)
    block(gw3, 0, {T: 100, F1: 50})
    gw3.hal.box = [pack({"k": "SUM", "n": F2, "s": 900, "t": gw3.hal.t, "i": 0, "p_min": 10.0}, sim_key(F2))]
    gw3.step()
    assert len(gw3.log) == 3 and 0 not in gw3.balance_done and "PIPE_LEAK" not in events(gw3)

    # Packet checks: a forged alert is dropped; a replayed one is acknowledged but not used twice,
    # even after the gateway restarts; the ACK goes back signed with the node's key
    hal = FakeHAL()
    hal.storage_path = os.path.join(tempfile.mkdtemp(), "GATEWAY.jsonl")
    gw = Gateway(hal, [F1], T)
    real = pack({"k": "ALERT", "n": F1, "s": 1, "t": 0, "a": "LEAK_NIGHT_FLOW", "flow": 0.05}, sim_key(F1))
    hal.box = [pack({"k": "ALERT", "n": F1, "s": 2, "t": 0, "a": "LEAK_NIGHT_FLOW"}, b"attacker-guessed"), real]
    gw.step()
    assert len(gw.log) == 1 and sorted(events(gw)) == ["BAD_PACKET", "NEW_ALERT"], events(gw)
    acks = [m for m in (unpack(raw, sim_key) for raw in hal.sent) if m["k"] == "ACK"]
    assert acks == [{"k": "ACK", "n": F1, "s": 1, "gt": 0}]
    gw = Gateway(hal, [F1], T)                                           # gateway restarts
    hal.box = [real]                                                     # someone replays the alert
    gw.step()
    assert gw.log == [] and "NEW_ALERT" not in events(gw), "replay after restart must be ignored"

    # Out-of-order backlog after >1000-message gap: s=1500 arrives first, then old buffered s=2 arrives
    hal2 = FakeHAL()
    hal2.storage_path = os.path.join(tempfile.mkdtemp(), "GW2.jsonl")
    gw2 = Gateway(hal2, [F1], T)
    hal2.box = [pack({"k": "ALERT", "n": F1, "s": 1500, "t": 9000, "a": "LEAK_NIGHT_FLOW"}, sim_key(F1))]
    gw2.step()
    hal2.box = [pack({"k": "SUM", "n": F1, "s": 2, "t": 900, "i": 1, "vol_l": 12.0, "p_min": 10.0}, sim_key(F1))]
    gw2.step()
    assert [m["s"] for m in gw2.log] == [1500, 2], "old out-of-order summary after >1000 gap must NOT be dropped"
    print("gateway self-check passed")
