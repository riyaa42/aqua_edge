"""Aqua Edge gateway program (one per building). Runs on the laptop; a LoRa module on an ESP32
board bridges the radio to the laptop over USB serial.

Same hardware layer as the nodes (hal.now / send / receive). Jobs:
  1. Acknowledge every node message (so nodes can delete it from their SD queue) and log it.
     Every ACK carries the gateway time ("gt") so node clocks stay in step.
  2. Building check, the mass balance (Ali et al. [6], supply vs branch flows): water leaving
     the tank minus water used by all flats, per 15-minute summary; above 10% for 2 blocks means
     a leak in the pipes between the tank and the flats (a leak inside a flat is after its
     meter, so the flat's own checks catch it). Re-arms after 2 normal blocks.
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

from aqua_edge import iso_utc, save_atomic
from packet import pack, unpack

SUPPLY_WINDOW_S = 900          # a SUPPLY_LOW flag from the tank counts for this long
MASS_BALANCE_PCT = 10.0        # Ali et al. [6] threshold
MASS_BALANCE_INTERVALS = 2     # blocks in a row above threshold before alerting, and normal before re-arming
MIN_SUPPLY_L = 5.0             # ignore near-empty blocks (division by tiny numbers)
SILENT_S = 2700                # nothing heard from a node for 3 summary periods = silent
CLOCK_AHEAD_S = 60             # message time this far ahead of the gateway clock = bad node clock


class Gateway:
    def __init__(self, hal, flat_ids, tank_id):
        self.hal, self.flats, self.tank = hal, set(flat_ids), tank_id
        path = getattr(hal, "storage_path", None)
        self.seen_path = path and os.path.splitext(path)[0] + "_seen.json"
        self.seen_floor = {}        # node -> highest contiguous seq received (1..floor)
        self.seen_above = {}        # node -> set of out-of-order seq > floor
        if self.seen_path and os.path.exists(self.seen_path):
            with open(self.seen_path) as f:
                data = json.load(f)
                for n, v in data.items():
                    if isinstance(v, dict):
                        self.seen_floor[n] = int(v.get("floor", 0))
                        self.seen_above[n] = set(v.get("above", []))
                    else:
                        # legacy list format compatibility
                        self.seen_floor[n] = 0
                        self.seen_above[n] = set(v)
        self.log = []               # every unique message received, with receive time
        self.events = []            # gateway's own decisions
        self.alert_history = {}     # (node, alert) -> (time of latest, how many so far)
        self.tank_low_t, self.supply_active_until = -1e9, -1
        self.sums = {}              # interval -> {node: volume}
        self.balance_done, self.balance_run, self.balance_ok = set(), 0, 0
        self.pipe_alerted = self.balance_paused = False
        self.last_seen = None       # node -> time last heard (set on the first step)
        self.silent = {}            # node -> time last heard before it went silent
        self.skewed = set()

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

    def _save_seen(self):
        if self.seen_path:
            payload = {
                n: {"floor": self.seen_floor.get(n, 0), "above": sorted(self.seen_above.get(n, set()))}
                for n in set(self.seen_floor) | set(self.seen_above)
            }
            save_atomic(self.seen_path, json.dumps(payload))

    def step(self):
        now = self.hal.now()
        utc_fn = getattr(self.hal, "utc_now", None)
        utc_s = utc_fn()[0] if utc_fn else now
        if self.last_seen is None:
            self.last_seen = {n: now for n in self.flats | {self.tank}}
        bad = 0
        for raw in self.hal.receive():
            m = unpack(raw, self.hal.key)
            if m is None:
                bad += 1                                    # wrong signature or broken packet
                continue
            if m["k"] not in ("ALERT", "FLAG", "SUM"):
                continue
            self.last_seen[m["n"]] = now
            is_dup = self._is_seen_and_record(m["n"], m["s"])
            if not is_dup:
                # Persist seen state BEFORE sending ACK so a node never deletes unpersisted data
                self._save_seen()
                self.log.append({**m, "rx": now, "rx_utc": round(utc_s), "rx_iso": iso_utc(utc_s)})
                self.check_clock(now, m)
                if m["k"] == "ALERT" and not m["a"].endswith("_CLEARED"):
                    self.check_repeat(m)
                elif m["k"] == "FLAG" and m["n"] == self.tank:
                    self.tank_low_t = m["t"]
                elif m["k"] == "SUM":
                    self.sums.setdefault(m["i"], {})[m["n"]] = m["vol_l"]
            ack = {"k": "ACK", "n": m["n"], "s": m["s"], "gt": now}       # gt: nodes sync their clock
            self.hal.send(pack(ack, self.hal.key(m["n"])))
        if bad:
            self.decide(now, "BAD_PACKET", count=bad)
        self.check_silence(now)
        self.supply_rule(now)
        self.mass_balance(now)

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
            leaking = err > MASS_BALANCE_PCT
            self.balance_run, self.balance_ok = (self.balance_run + 1, 0) if leaking else (0, self.balance_ok + 1)
            if self.balance_run >= MASS_BALANCE_INTERVALS and not self.pipe_alerted:
                self.pipe_alerted = True
                self.decide(now, "PIPE_LEAK", interval=i, error_pct=round(err, 1))
            elif self.balance_ok >= MASS_BALANCE_INTERVALS:
                self.pipe_alerted = False                   # re-arm: a later leak alerts again


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

        def send(self, raw):
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

    # Packet checks: a forged alert is dropped; a replayed one is acknowledged but not used twice,
    # even after the gateway restarts; the ACK goes back signed with the node's key
    hal = FakeHAL()
    hal.storage_path = os.path.join(tempfile.mkdtemp(), "GATEWAY.jsonl")
    gw = Gateway(hal, [F1], T)
    real = pack({"k": "ALERT", "n": F1, "s": 1, "t": 0, "a": "LEAK_NIGHT_FLOW", "flow": 0.05}, sim_key(F1))
    hal.box = [pack({"k": "ALERT", "n": F1, "s": 2, "t": 0, "a": "LEAK_NIGHT_FLOW"}, b"attacker-guessed"), real]
    gw.step()
    assert len(gw.log) == 1 and sorted(events(gw)) == ["BAD_PACKET", "NEW_ALERT"], events(gw)
    assert unpack(hal.sent[0], sim_key) == {"k": "ACK", "n": F1, "s": 1, "gt": 0}
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
