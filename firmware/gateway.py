"""Aqua Edge gateway program (one per building). Runs on the laptop; a LoRa module on an ESP32
board bridges the radio to the laptop over USB serial.

Same hardware layer as the nodes (hal.now / send / receive). Jobs:
  1. Acknowledge every node message (so nodes can delete it from their SD queue) and log it.
     Every ACK carries the gateway time ("gt") so node clocks stay in step. The ACK goes out
     inside the sender's own time slot, after its packets (borrowed from LoRaWAN Class A), so a
     reply never lands in another node's slot: our gateway is one radio and cannot hear while
     it sends. hal.slots = {node: (index, count)}, the same table the nodes got at installation.
  2. Down pipe checks, per stretch (the mass balance of Ali et al. [6], supply vs branch flows).
     The down pipe has a meter at the top (tank node) and one above each lower floor (read by
     flats 301, 201, 101, G01, PIPE_METERS). For each floor's stretch: litres in at its top meter,
     minus litres on to the next meter, minus that floor's 4 flats = litres lost in that stretch,
     so a leak gets a floor (a leak inside a flat is after its meter: the flat's own checks).
     - Night check, 02:00 to 04:00 (like a water company's minimum night flow check): flows are
       smallest, so meter errors are a few litres and a small leak stands out. More than
       NIGHT_MISSING_L lost in a stretch gives PIPE_LEAK {where, check: "night"}.
     - Fast check, every 15-minute block at any hour: more than FAST_MISSING_L lost in one block
       gives PIPE_LEAK {where, check: "fast"} at once (medium leaks and bursts, with a floor).
     One alert per stretch; a clean night re-arms it. Without the pipe meters' numbers (offline
     data, or one of those flats silent) the whole pipe is checked at night: tank minus all flats.
  3. Silent nodes: a node not heard for 45 min (3 missed summaries) gives NODE_SILENT, and
     NODE_BACK when it is heard again. While any node is silent the building check cannot be
     done honestly (its litres would look "missing"), so BALANCE_PAUSED says so; the late
     summaries are still checked when they arrive (store-and-forward).
  4. Supply event: the tank node reports its level far below normal (pump or power problem).
  5. Repeat check: nodes send a fresh alert every time a problem comes back; the gateway
     marks each one NEW_ALERT or REPEAT_ALERT (same node and alert type as before).
  6. Clock check: a message stamped more than 1 min in the future means that node's clock is
     wrong (CLOCK_SKEW). An old stamp is normal: the message waited on the SD card.
  7. Packet check (packet.py): a packet with a wrong signature is dropped (BAD_PACKET), and a
     (node, counter) pair already seen is a duplicate or a replay: acknowledged, not used again.
     The pairs seen are saved to disk, so a gateway restart does not reopen the door to replays.
     ACKs are signed with the node's key, so a fake ACK cannot make a node delete its messages.
  8. Beacon: once a minute, in the gateway's own time slot, a BEACON (signed with the building key,
     carries the gateway time). A node that hears no beacon or ACK for 2 minutes knows the gateway
     is away, slows its retries, and sends its stored messages in turns when the beacon is back.
  9. Commands (CMD): warnings only the gateway can give, because they need several nodes together.
     VALVE_CLOSED / VALVE_OPENED (from the tank node) go to every flat, PIPE_LEAK / PIPE_LEAK_CLEARED
     to the flats of that floor. Sent in the gateway's slot and repeated until the node answers GOT;
     the open commands are saved, so a gateway restart still delivers them.
 10. Tank valve shut: no water is meant to flow, so the down pipe checks and the supply rule pause
     for every 15-minute block the valve was shut in (logged once as VALVE_CLOSED).
 11. BALANCE: for every complete block, the litres lost in each stretch (for the dashboard's chart;
     not an alarm).
How the leak checks fit together: leak_detection/README.md.
"""
import json
import os

from aqua_edge import save_atomic, DAY, HOUR, SUMMARY_S, RETRY_S, SLOT_GUARD_S
from packet import pack, unpack

SUPPLY_WINDOW_S = 900          # a SUPPLY_LOW flag from the tank counts for this long
NIGHT_HOURS = (2, 4)           # down pipe check window, local time: the quietest hours
NIGHT_MISSING_L = 20.0         # litres lost in one stretch over the window that meter error cannot explain
# Measured with the simulation's realistic meters (live/water.py), 3 clean days of bursty use (each flat uses
# water in short spurts with zero flow between, build_society.py): clean nights -1.7 to +1.1 L per stretch;
# a 0.005 L/s leak loses 33-36 L in its own stretch, under 1.3 L in the others.
# So 20 L finds leaks from ~0.003 L/s and names the floor.
# ponytail: one fixed limit; on a real building measure a few clean nights first
FAST_MISSING_L = 30.0          # litres lost in one stretch in one 15-minute block, any hour
# Measured (bursty use, 3 clean days, gateway BALANCE records): no clean block lost more than 4.0 L; 0.05 L/s loses 45 L a block, 0.5 L/s 455 L, 1.5 L/s 1330 L. So 30 L finds leaks from ~0.035 L/s in 15 min.
PIPE_METERS = {"B-301": "3", "B-201": "2", "B-101": "1", "B-G01": "G"}   # flat whose node reads the meter above floor
# ponytail: building B's table, like packet.NAMES; at installation it comes with the slot table
SILENT_S = 2700                # nothing heard from a node for 3 summary periods = silent
CLOCK_AHEAD_S = 60             # message time this far ahead of the gateway clock = bad node clock
ACK_AT_S = 1.5                 # seconds into the sender's slot: its own packets start at 0.2 s
# ponytail: fits the 2.86 s slot at SF7/SF8; at SF9+ the slot fit check (HANDOVER step 8) must count ACKs
SEEN_WINDOW = 1000             # counters this far below a node's newest are refused as too old
# ponytail: 1000 is about 10 days of one node's messages; a longer outage loses its oldest backlog
GATEWAY = "GATEWAY"            # the gateway's own name in the slot table and for the building key


class Gateway:
    def __init__(self, hal, flat_ids, tank_id, pipe_meters=PIPE_METERS):
        self.hal, self.flats, self.tank = hal, set(flat_ids), tank_id
        self.pipe_meters = {n: f for n, f in pipe_meters.items() if n in self.flats}
        self.floors = sorted({n[2] for n in self.flats}, key=lambda f: -1 if f == "G" else int(f), reverse=True)   # top first
        path = getattr(hal, "storage_path", None)
        self.seen_path = path and os.path.splitext(path)[0] + "_seen.json"
        self.seen = {}              # node -> counters already received
        if self.seen_path and os.path.exists(self.seen_path):
            with open(self.seen_path) as f:
                self.seen = {n: set(v) for n, v in json.load(f).items()}
        self.log = []               # every unique message received, with receive time
        self.events = []            # gateway's own decisions
        self.alert_history = {}     # (node, alert) -> (time of latest, how many so far)
        self.tank_low_t, self.supply_active_until = -1e9, -1
        self.sums = {}              # interval -> {node: volume}
        self.pipe = {}              # interval -> {flat: down pipe litres}, from the flats with a pipe meter
        self.nights_done = set()    # nights (local day numbers) already checked
        self.fast_done = set()      # 15-minute blocks already fast-checked
        self.pipe_alerted = set()   # stretches ("3", "G"; "all" for the whole pipe) with an open alert
        self.balance_paused = False
        self.last_seen = None       # node -> time last heard (set on the first step)
        self.silent = {}            # node -> time last heard before it went silent
        self.skewed = set()
        self.valve_closed, self.valve_blocks = False, set()   # blocks the tank valve was shut in
        self.cmd_path = path and os.path.splitext(path)[0] + "_cmds.json"
        self.cmds, self.cmd_no = {}, 0   # command number -> {"n", "a", "t", "sent"}: waiting for a GOT
        if self.cmd_path and os.path.exists(self.cmd_path):
            with open(self.cmd_path) as f:
                d = json.load(f)
            self.cmds, self.cmd_no = {int(k): v for k, v in d["cmds"].items()}, d["no"]

    def save_cmds(self):
        if self.cmd_path:
            save_atomic(self.cmd_path, json.dumps({"cmds": self.cmds, "no": self.cmd_no}))

    def command(self, now, code, nodes):
        for n in sorted(nodes):
            self.cmd_no += 1
            self.cmds[self.cmd_no] = {"n": n, "a": code, "t": now, "sent": -1e9}
        self.save_cmds()

    def step(self):
        now = self.hal.now()
        if self.last_seen is None:
            self.last_seen = {n: now for n in self.flats | {self.tank}}
        bad, new = 0, False
        for raw in self.hal.receive():
            m = unpack(raw, self.hal.key)
            if m is None:
                bad += 1                                    # wrong signature or broken packet
                continue
            if m["k"] == "GOT":                             # a node confirms a command
                if self.cmds.get(m["s"], {}).get("n") == m["n"]:
                    del self.cmds[m["s"]]
                    self.save_cmds()
                self.last_seen[m["n"]] = now
                continue
            if m["k"] not in ("ALERT", "FLAG", "SUM"):
                continue
            ack = {"k": "ACK", "n": m["n"], "s": m["s"], "gt": now}       # gt: nodes sync their clock
            slot = (getattr(self.hal, "slots", None) or {}).get(m["n"])
            self.hal.send(pack(ack, self.hal.key(m["n"])), at_s=slot and slot[0] * 60 / slot[1] + ACK_AT_S)
            self.last_seen[m["n"]] = now
            seen = self.seen.setdefault(m["n"], set())
            if m["s"] in seen or m["s"] <= max(seen, default=0) - SEEN_WINDOW:
                continue                                    # duplicate (our ACK was lost) or replay
            seen.add(m["s"])
            if len(seen) > 2 * SEEN_WINDOW:
                self.seen[m["n"]] = {x for x in seen if x > max(seen) - SEEN_WINDOW}
            new = True
            self.log.append({**m, "rx": now})
            self.check_clock(now, m)
            if m["k"] == "ALERT" and m["n"] == self.tank and m["a"] in ("VALVE_CLOSED", "VALVE_OPENED"):
                self.valve_closed = m["a"] == "VALVE_CLOSED"
                self.valve_blocks.add(m["t"] // SUMMARY_S)
                self.decide(m["t"], m["a"])
                self.command(now, m["a"], self.flats)
            elif m["k"] == "ALERT" and not m["a"].endswith("_CLEARED"):     # all-clear is not a problem
                self.check_repeat(m)
            elif m["k"] == "FLAG" and m["n"] == self.tank:
                self.tank_low_t = m["t"]
            elif m["k"] == "SUM":
                self.sums.setdefault(m["i"], {})[m["n"]] = m["vol_l"]
                if "pipe_l" in m:
                    self.pipe.setdefault(m["i"], {})[m["n"]] = m["pipe_l"]
        if bad:
            self.decide(now, "BAD_PACKET", count=bad)
        if new and self.seen_path:
            save_atomic(self.seen_path, json.dumps({n: sorted(v) for n, v in self.seen.items()}))
        if self.valve_closed:
            self.valve_blocks.add(now // SUMMARY_S)
        self.check_silence(now)
        if not self.valve_closed:
            self.supply_rule(now)
        self.night_check(now)
        self.send_radio(now)

    def send_radio(self, now):
        """In the gateway's own slot: the beacon, then commands not yet confirmed (again every 2 min)."""
        slots = getattr(self.hal, "slots", None) or {}
        at = slots.get(GATEWAY) and slots[GATEWAY][0] * 60 / slots[GATEWAY][1] + SLOT_GUARD_S
        self.hal.send(pack({"k": "BEACON", "n": GATEWAY, "s": int(now) // 60 % 2 ** 32, "gt": now}, self.hal.key(GATEWAY)), at_s=at)
        for no, c in sorted(self.cmds.items()):
            if now - c["sent"] >= RETRY_S:
                c["sent"] = now
                self.hal.send(pack({"k": "CMD", "n": c["n"], "s": no, "t": c["t"], "a": c["a"]}, self.hal.key(c["n"])), at_s=at)

    def decide(self, now, kind, **info):
        self.events.append({"t": now, "event": kind, **info})

    def check_repeat(self, m):
        """Nodes re-send an alert each time a problem comes back; decide if this one is new."""
        key = (m["n"], m["a"])
        prev = self.alert_history.get(key)
        self.alert_history[key] = (m["t"], prev[1] + 1 if prev else 1)
        if prev:
            self.decide(m["t"], "REPEAT_ALERT", node=m["n"], alert=m["a"], count=prev[1] + 1,
                        since_last_min=round((m["t"] - prev[0]) / 60))
        else:
            self.decide(m["t"], "NEW_ALERT", node=m["n"], alert=m["a"])

    def check_clock(self, now, m):
        ahead = m["t"] - now
        if ahead > CLOCK_AHEAD_S and m["n"] not in self.skewed:
            self.skewed.add(m["n"])
            self.decide(now, "CLOCK_SKEW", node=m["n"], ahead_s=round(ahead))
        elif ahead <= CLOCK_AHEAD_S:
            self.skewed.discard(m["n"])

    def check_silence(self, now):
        for n, t in self.last_seen.items():
            if now - t >= SILENT_S and n not in self.silent:
                self.silent[n] = t
                self.decide(now, "NODE_SILENT", node=n, last_heard_min=round((now - t) / 60))
            elif now - t < SILENT_S and n in self.silent:
                self.decide(now, "NODE_BACK", node=n, silent_min=round((now - self.silent.pop(n)) / 60))

    def supply_rule(self, now):
        if now - self.tank_low_t <= SUPPLY_WINDOW_S:
            if now > self.supply_active_until:
                self.decide(now, "SUPPLY_EVENT", tank_low=True)
            self.supply_active_until = now + SUPPLY_WINDOW_S

    def stretch_lost(self, i):
        """Litres lost in each stretch of down pipe in block i ({floor: litres}), or None without
        every pipe meter's number."""
        g, p = self.sums[i], self.pipe.get(i, {})
        if not self.pipe_meters or any(n not in p for n in self.pipe_meters):
            return None
        into = {self.floors[0]: g[self.tank], **{f: p[n] for n, f in self.pipe_meters.items()}}
        if set(into) != set(self.floors):
            return None
        below = dict(zip(self.floors, self.floors[1:]))
        return {f: into[f] - into.get(below.get(f), 0) - sum(g[n] for n in self.flats if n[2] == f) for f in self.floors}

    def pipe_leak(self, now, stretch, lost, seconds, check):
        if stretch in self.pipe_alerted:
            return
        self.pipe_alerted.add(stretch)
        where = {} if stretch == "all" else {"where": "B-D" + ("0" if stretch == "G" else stretch)}
        self.decide(now, "PIPE_LEAK", **where, missing_l=round(lost), lps=round(lost / seconds, 3), check=check)
        self.command(now, "PIPE_LEAK", self.floor_flats(stretch))

    def floor_flats(self, stretch):
        return self.flats if stretch == "all" else {n for n in self.flats if n[2] == stretch}

    def pipe_clear(self, now, stretch):
        if stretch in self.pipe_alerted:
            self.pipe_alerted.discard(stretch)
            self.decide(now, "PIPE_LEAK_CLEARED", **({} if stretch == "all" else {"where": "B-D" + ("0" if stretch == "G" else stretch)}))
            self.command(now, "PIPE_LEAK_CLEARED", self.floor_flats(stretch))

    def night_check(self, now):
        if self.silent and not self.balance_paused:
            self.balance_paused = True
            self.decide(now, "BALANCE_PAUSED", silent=sorted(self.silent))
        elif not self.silent:
            self.balance_paused = False
        complete = [i for i, g in self.sums.items() if self.tank in g and self.flats <= set(g)]
        # Fast check: every complete block, any hour
        for i in sorted(set(complete) - self.fast_done):
            self.fast_done.add(i)
            per = self.stretch_lost(i)
            if i in self.valve_blocks:
                continue                                    # valve shut: water not meant to flow
            if per:
                self.decide(now, "BALANCE", i=i, lost={f: round(v, 1) for f, v in per.items()})
            for f, lost in (per or {}).items():
                if lost > FAST_MISSING_L:
                    self.pipe_leak(now, f, lost, SUMMARY_S, "fast")
        # Night check, 02:00 to 04:00 local
        off, (h0, h1) = getattr(self.hal, "utc_offset_s", 0), NIGHT_HOURS
        nights = {}
        for i in self.sums:
            local = i * SUMMARY_S + off
            if h0 <= local % DAY // HOUR < h1:
                nights.setdefault(local // DAY, []).append(i)
        for d in sorted(nights):
            blocks = nights[d]
            if d in self.nights_done or len(blocks) < (h1 - h0) * HOUR // SUMMARY_S or not set(blocks) <= set(complete):
                continue                                    # wait until every node's summaries arrived
            self.nights_done.add(d)
            if self.valve_blocks & set(blocks):
                continue                                    # valve shut that night: no check
            per = [self.stretch_lost(i) for i in blocks]
            if all(per):
                lost = {f: sum(x[f] for x in per) for f in self.floors}
            else:                                           # no pipe meter numbers: the whole pipe
                lost = {"all": sum(self.sums[i][self.tank] - sum(self.sums[i][n] for n in self.flats) for i in blocks)}
            for f, litres in lost.items():
                if litres > NIGHT_MISSING_L:
                    self.pipe_leak(now, f, litres, (h1 - h0) * HOUR, "night")
                else:
                    self.pipe_clear(now, f)                 # re-arm: a later leak alerts again
                    if f != "all":
                        self.pipe_clear(now, "all")


if __name__ == "__main__":
    import tempfile
    from packet import sim_key

    class FakeHAL:
        storage_path = None
        key = staticmethod(sim_key)

        def __init__(self):
            self.t, self.box, self.seq, self.sent, self.at = 0, [], 0, [], []

        def now(self):
            return self.t

        def receive(self):
            msgs, self.box = self.box, []
            return msgs

        def send(self, raw, at_s=None):
            self.sent.append(raw)
            self.at.append(at_s)

    def block(gw, i, vols, pipe={}):
        """Deliver one 15-minute block of summaries {node: litres} (pipe: {flat: down pipe litres}),
        then step the gateway 15 times."""
        for n, v in vols.items():
            gw.hal.seq += 1
            m = {"k": "SUM", "n": n, "s": gw.hal.seq, "t": gw.hal.t, "i": i, "vol_l": v, **({"pipe_l": pipe[n]} if n in pipe else {})}
            gw.hal.box.append(pack(m, sim_key(n)))
        for _ in range(15):
            gw.step()
            gw.hal.t += 60

    events = lambda gw: [e["event"] for e in gw.events if e["event"] != "BALANCE"]
    leaks = lambda gw: [e for e in gw.events if e["event"] == "PIPE_LEAK"]
    F1, F2, T = "B-101", "B-102", "B-TANK"
    night = lambda d: [(d * DAY + NIGHT_HOURS[0] * HOUR) // SUMMARY_S + k for k in range(8)]   # 02:00-04:00
    gw = Gateway(FakeHAL(), [F1, F2], T)
    ok, leak = {T: 20, F1: 10, F2: 10}, {T: 30, F1: 10, F2: 10}       # leak: 10 L x 8 blocks = 80 L missing
    for i in range(60, 64):
        block(gw, i, {T: 300, F1: 100, F2: 100})                     # 15:00: 100 L missing in the day, not checked
    for d, v in enumerate([ok, leak, leak, ok, leak]):               # clean, leak (alert), still leaking, fixed, new leak
        for i in night(d):
            block(gw, i, v)
    assert events(gw) == ["PIPE_LEAK", "PIPE_LEAK_CLEARED", "PIPE_LEAK"], events(gw)
    assert leaks(gw)[0]["missing_l"] == 80 and gw.nights_done == {0, 1, 2, 3, 4}
    # Without pipe meter numbers the whole pipe is checked (above); with them, each stretch on its own.
    # Floors 2 and 1; B-101 reads the meter above floor 1. Stretch 2 = tank - that meter - floor 2 flats,
    # stretch 1 = that meter - floor 1 flats.
    F21, F22, F11, F12 = "B-201", "B-202", "B-101", "B-102"
    def pb(gw, i, tank, meter, lost_lower=0):      # every flat uses 10 L; lost_lower: litres lost in stretch 1
        block(gw, i, {T: tank + lost_lower, F21: 10, F22: 10, F11: 10, F12: 10}, pipe={F11: meter + lost_lower})
    gw = Gateway(FakeHAL(), [F21, F22, F11, F12], T, {F11: "1"})
    pb(gw, 60, 140, 20)                                              # 15:00: 100 L lost in stretch 2 in one block
    assert [(e["where"], e["check"], e["missing_l"]) for e in leaks(gw)] == [("B-D2", "fast", 100)], gw.events
    pb(gw, 61, 140, 20)                                              # still leaking: no second alert
    for i in night(1):                                               # stretch 2 still leaks, stretch 1 starts: 5 L x 8
        pb(gw, i, 140, 20, lost_lower=5)
    assert [(e["where"], e["check"]) for e in leaks(gw)] == [("B-D2", "fast"), ("B-D1", "night")], gw.events
    assert leaks(gw)[1]["missing_l"] == 40
    for i in night(2):                                               # both fixed: a clean night re-arms them
        pb(gw, i, 40, 20)
    pb(gw, 300, 40, 20, lost_lower=40)                               # later a burst below the floor 1 meter
    assert [(e["where"], e["check"]) for e in leaks(gw)][-1] == ("B-D1", "fast") and len(leaks(gw)) == 3, gw.events
    assert [e["where"] for e in gw.events if e["event"] == "PIPE_LEAK_CLEARED"] == ["B-D2", "B-D1"]
    gw = Gateway(FakeHAL(), [F21, F22, F11, F12], T, {F11: "1"})
    for i in list(range(60, 70)) + night(0):                         # clean day and night: nothing
        pb(gw, i, 40, 20)
    assert events(gw) == [] and any(e["event"] == "BALANCE" for e in gw.events), gw.events
    # F2 goes silent for 4 blocks (60 min) of the night: alert and pause; it comes back with its
    # stored summaries and the night is checked then
    gw = Gateway(FakeHAL(), [F1, F2], T)
    for i in night(0)[:4]:
        block(gw, i, {T: 20, F1: 10})
    assert events(gw) == ["NODE_SILENT", "BALANCE_PAUSED"], events(gw)
    for i in night(0)[4:]:
        block(gw, i, ok)
    assert 0 not in gw.nights_done
    for i in night(0)[:4]:
        block(gw, i, {F2: 10})
    assert "NODE_BACK" in events(gw) and gw.nights_done == {0}, events(gw)
    assert "PIPE_LEAK" not in events(gw)
    # a message stamped 5 minutes in the future: bad node clock
    gw.hal.box.append(pack({"k": "ALERT", "n": F1, "s": 999, "t": gw.hal.t + 300, "a": "LEAK_NIGHT_FLOW"}, sim_key(F1)))
    gw.step()
    assert "CLOCK_SKEW" in events(gw)

    # Packet checks: a forged alert is dropped; a replayed one is acknowledged but not used twice,
    # even after the gateway restarts; the ACK goes back signed with the node's key
    hal = FakeHAL()
    hal.storage_path = os.path.join(tempfile.mkdtemp(), "GATEWAY.jsonl")
    hal.slots = {F1: (3, 20)}                                            # F1 owns seconds 9 to 12
    gw = Gateway(hal, [F1], T)
    real = pack({"k": "ALERT", "n": F1, "s": 1, "t": 0, "a": "LEAK_NIGHT_FLOW", "flow": 0.05}, sim_key(F1))
    hal.box = [pack({"k": "ALERT", "n": F1, "s": 2, "t": 0, "a": "LEAK_NIGHT_FLOW"}, b"attacker-guessed"), real]
    gw.step()
    assert len(gw.log) == 1 and sorted(events(gw)) == ["BAD_PACKET", "NEW_ALERT"], events(gw)
    assert unpack(hal.sent[0], sim_key) == {"k": "ACK", "n": F1, "s": 1, "gt": 0}
    assert hal.at[0] == 3 * 60 / 20 + ACK_AT_S, "ACK must go out in the sender's own slot"
    gw = Gateway(hal, [F1], T)                                           # gateway restarts
    hal.box = [real]                                                     # someone replays the alert
    gw.step()
    assert gw.log == [] and "NEW_ALERT" not in events(gw), "replay after restart must be ignored"

    # Beacon every minute in the gateway's slot; tank valve shut -> VALVE_CLOSED command to every
    # flat, repeated every 2 min until GOT, kept across a restart; pipe checks pause meanwhile
    from packet import peek
    hal = FakeHAL()
    hal.storage_path = os.path.join(tempfile.mkdtemp(), "GATEWAY.jsonl")
    hal.slots = {F1: (0, 4), F2: (1, 4), T: (2, 4), GATEWAY: (3, 4)}
    gw = Gateway(hal, [F1, F2], T)
    gw.step()
    assert [peek(r)["k"] for r in hal.sent] == ["BEACON"] and hal.at == [45 + SLOT_GUARD_S]
    assert unpack(hal.sent[0], lambda n: sim_key(n) if n == GATEWAY else None)["gt"] == 0
    hal.t, hal.sent, hal.at = 60, [], []
    hal.box = [pack({"k": "ALERT", "n": T, "s": 1, "t": 60, "a": "VALVE_CLOSED", "valve": 0}, sim_key(T))]
    gw.step()
    cmds = [unpack(r, sim_key) for r in hal.sent if peek(r)["k"] == "CMD"]
    assert sorted((c["n"], c["a"]) for c in cmds) == [(F1, "VALVE_CLOSED"), (F2, "VALVE_CLOSED")], cmds
    assert "VALVE_CLOSED" in events(gw) and gw.valve_closed
    f1 = next(c for c in cmds if c["n"] == F1)
    hal.box = [pack({"k": "GOT", "n": F1, "s": f1["s"], "t": 61}, sim_key(F1))]
    hal.t, hal.sent = 120, []
    gw.step()
    assert list(gw.cmds) == [f1["s"] + 1] and not any(peek(r)["k"] == "CMD" for r in hal.sent), "not yet 2 min"
    gw = Gateway(hal, [F1, F2], T)                                         # restart: F2's command still waits
    hal.t, hal.sent = 180, []
    gw.step()
    assert [unpack(r, sim_key)["n"] for r in hal.sent if peek(r)["k"] == "CMD"] == [F2]
    gw = Gateway(FakeHAL(), [F1, F2], T)
    gw.hal.t = night(0)[0] * SUMMARY_S
    gw.hal.box = [pack({"k": "ALERT", "n": T, "s": 999, "t": gw.hal.t, "a": "VALVE_CLOSED"}, sim_key(T))]
    for i in night(0):
        block(gw, i, {T: 0, F1: 0, F2: 0} if i > night(0)[0] else {T: 300, F1: 0, F2: 0})   # 300 L "lost": valve shutting
    assert "PIPE_LEAK" not in events(gw) and 0 in gw.nights_done, (events(gw), gw.nights_done, gw.valve_blocks)
    print("gateway self-check passed")
