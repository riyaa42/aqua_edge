"""Aqua Edge gateway program (one per building). Runs on the laptop; a LoRa module on an ESP32
board bridges the radio to the laptop over USB serial.

Same hardware layer as the nodes (hal.now / send / receive). Jobs:
  1. Acknowledge every node message (so nodes can delete it from their SD queue) and log it.
     Every ACK carries the gateway time ("gt") so node clocks stay in step. The ACK goes out
     inside the sender's own time slot, after its packets (borrowed from LoRaWAN Class A), so a
     reply never lands in another node's slot: our gateway is one radio and cannot hear while
     it sends. hal.slots = {node: (index, count)}, the same table the nodes got at installation.
  2. Down pipe check, once a night (the mass balance of Ali et al. [6], supply vs branch flows,
     done at the quietest hours, like a water company's minimum night flow check): litres that
     left the tank minus litres that reached all flats from 02:00 to 04:00. More than
     NIGHT_MISSING_L missing means a leak in the pipes between the tank and the flats (a leak
     inside a flat is after its meter, so the flat's own checks catch it). At night the flows
     are smallest, so meter errors are a few litres and a small leak stands out; in the day the
     same leak hides inside the meters' 2-5% error. Bursts are found sooner by the tank node.
     Alerts once; re-arms after a night with nothing missing.
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
How the leak checks fit together: leak_detection/README.md.
"""
import json
import os

from aqua_edge import save_atomic, DAY, HOUR, SUMMARY_S
from packet import pack, unpack

SUPPLY_WINDOW_S = 900          # a SUPPLY_LOW flag from the tank counts for this long
NIGHT_HOURS = (2, 4)           # down pipe check window, local time: the quietest hours
NIGHT_MISSING_L = 20.0         # litres missing over the window that meter error cannot explain
# Measured with the simulation's realistic meters (live/water.py): clean nights -4 to +5 L; a 0.005 L/s
# pipe leak 36-42 L, 0.01 L/s 72-81 L. So 20 L (4x the worst clean night) finds leaks from ~0.003 L/s.
# ponytail: one fixed limit; on a real building measure a few clean nights first, since real night use
# comes in bursts (a flush, a glass of water) and our simulated use is smooth
SILENT_S = 2700                # nothing heard from a node for 3 summary periods = silent
CLOCK_AHEAD_S = 60             # message time this far ahead of the gateway clock = bad node clock
ACK_AT_S = 1.5                 # seconds into the sender's slot: its own packets start at 0.2 s
# ponytail: fits the 2.86 s slot at SF7/SF8; at SF9+ the slot fit check (HANDOVER step 8) must count ACKs
SEEN_WINDOW = 1000             # counters this far below a node's newest are refused as too old
# ponytail: 1000 is about 10 days of one node's messages; a longer outage loses its oldest backlog


class Gateway:
    def __init__(self, hal, flat_ids, tank_id):
        self.hal, self.flats, self.tank = hal, set(flat_ids), tank_id
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
        self.nights_done = set()    # nights (local day numbers) already checked
        self.pipe_alerted = self.balance_paused = False
        self.last_seen = None       # node -> time last heard (set on the first step)
        self.silent = {}            # node -> time last heard before it went silent
        self.skewed = set()

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
            if m["k"] == "ALERT" and not m["a"].endswith("_CLEARED"):     # all-clear is not a problem
                self.check_repeat(m)
            elif m["k"] == "FLAG" and m["n"] == self.tank:
                self.tank_low_t = m["t"]
            elif m["k"] == "SUM":
                self.sums.setdefault(m["i"], {})[m["n"]] = m["vol_l"]
        if bad:
            self.decide(now, "BAD_PACKET", count=bad)
        if new and self.seen_path:
            save_atomic(self.seen_path, json.dumps({n: sorted(v) for n, v in self.seen.items()}))
        self.check_silence(now)
        self.supply_rule(now)
        self.night_check(now)

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

    def night_check(self, now):
        if self.silent and not self.balance_paused:
            self.balance_paused = True
            self.decide(now, "BALANCE_PAUSED", silent=sorted(self.silent))
        elif not self.silent:
            self.balance_paused = False
        off, (h0, h1) = getattr(self.hal, "utc_offset_s", 0), NIGHT_HOURS
        nights = {}
        for i, got in self.sums.items():
            local = i * SUMMARY_S + off
            if h0 <= local % DAY // HOUR < h1:
                nights.setdefault(local // DAY, []).append(got)
        for d in sorted(nights):
            blocks = nights[d]
            if d in self.nights_done or len(blocks) < (h1 - h0) * HOUR // SUMMARY_S or \
                    any(self.tank not in g or not self.flats <= set(g) for g in blocks):
                continue                                    # wait until every node's summaries arrived
            self.nights_done.add(d)
            missing = sum(g[self.tank] - sum(g[n] for n in self.flats) for g in blocks)
            if missing > NIGHT_MISSING_L and not self.pipe_alerted:
                self.pipe_alerted = True
                self.decide(now, "PIPE_LEAK", missing_l=round(missing), lps=round(missing / ((h1 - h0) * HOUR), 3))
            elif missing <= NIGHT_MISSING_L:
                self.pipe_alerted = False                   # re-arm: a later leak alerts again


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
    night = lambda d: [(d * DAY + NIGHT_HOURS[0] * HOUR) // SUMMARY_S + k for k in range(8)]   # 02:00-04:00
    gw = Gateway(FakeHAL(), [F1, F2], T)
    ok, leak = {T: 20, F1: 10, F2: 10}, {T: 30, F1: 10, F2: 10}       # leak: 10 L x 8 blocks = 80 L missing
    for i in range(60, 64):
        block(gw, i, {T: 300, F1: 100, F2: 100})                     # 15:00: 100 L missing in the day, not checked
    for d, v in enumerate([ok, leak, leak, ok, leak]):               # clean, leak (alert), still leaking, fixed, new leak
        for i in night(d):
            block(gw, i, v)
    assert events(gw) == ["PIPE_LEAK", "PIPE_LEAK"], events(gw)
    assert gw.events[0]["missing_l"] == 80 and gw.nights_done == {0, 1, 2, 3, 4}
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
    print("gateway self-check passed")
