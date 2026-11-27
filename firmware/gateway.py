"""Aqua Edge gateway program (one per building). Runs on the laptop; a LoRa module on an ESP32
board bridges the radio to the laptop over USB serial.

Same hardware layer as the nodes (hal.now / send / receive). Jobs:
  1. Acknowledge every node message (so nodes can delete it from their SD queue) and log it.
     Every ACK carries the gateway time ("gt") for setting node clocks (a job for the hardware
     layer; so far only the live simulation does it).
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
     marks each one NEW_ALERT or REPEAT_ALERT (same node and alert type as before). A flat's
     LEAK_PRESSURE_DROP stamped during an active pipe leak also gets possible_cause=PIPE_LEAK
     (lower building pressure plus the flat's own heavy use can trigger it). Nothing is hidden.
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

from aqua_edge import SUMMARY_S, iso_utc, save_atomic
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
        self.balance_done, self.balance_run, self.balance_ok = set(), 0, 0
        self.pipe_alerted = self.balance_paused = False
        self.pipe_since = None      # start time of the first block of the active pipe leak
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
                    if m["a"] == "ABNORMAL_SUPPLY":
                        self.tank_abnormal_supply_t = m["t"]
                        self.tank_abnormal_supply_recv_t = now
                    elif m["a"] in ("LEAK_NIGHT_FLOW", "LEAK_CONTINUOUS_FLOW", "LEAK_PRESSURE_DROP"):
                        self.flat_leak_t = m["t"]
                        self.flat_leak_recv_t = now
                elif m["k"] == "FLAG" and m["n"] == self.tank:
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
        # "possibly": a real flat leak during a pipe leak is labelled too. An alert that arrives
        # before PIPE_LEAK is decided is not labelled; the time test skips older stored alerts.
        extra = {}
        if m["a"] == "LEAK_PRESSURE_DROP" and self.pipe_alerted and m["t"] >= self.pipe_since:
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
        elif ahead <= CLOCK_AHEAD_S:
            self.skewed.discard(m["n"])

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
            leaking = err > MASS_BALANCE_PCT
            if not leaking and not self.pipe_alerted and i in self.p_sums:
                for f, p_v in self.p_sums[i].items():
                    self.p_base[f] = p_v if f not in self.p_base else 0.9 * self.p_base[f] + 0.1 * p_v
                for f, p_v in self.p_mean_sums.get(i, {}).items():
                    self.p_mean_base[f] = p_v if f not in self.p_mean_base else 0.9 * self.p_mean_base[f] + 0.1 * p_v
            self.balance_run, self.balance_ok = (self.balance_run + 1, 0) if leaking else (0, self.balance_ok + 1)
            if self.balance_run >= MASS_BALANCE_INTERVALS and not self.pipe_alerted:
                self.pipe_alerted = True
                self.pipe_since = (i - MASS_BALANCE_INTERVALS + 1) * SUMMARY_S
                drops = self._cross_floor_drops(i)
                suspected, floor_range, conf = self._localize_floor(drops)
                self.decide(now, "PIPE_LEAK", interval=i, error_pct=round(err, 1),
                            suspected_floor=suspected, suspected_range=floor_range,
                            localization_confidence=conf, floor_drops=drops)
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
