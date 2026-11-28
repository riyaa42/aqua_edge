"""Aqua Edge node firmware.

Runs unchanged in Webots and on the Raspberry Pi Zero 2 W. It never touches hardware
directly; everything goes through a small hardware layer `hal` with these methods:

    hal.now()                 -> current time in seconds; every message is stamped with it ("t").
                                 Simulation: seconds since start (start = local midnight).
                                 Real node: UTC Unix seconds from the DS3231 RTC.
    hal.utc_offset_s          -> optional: seconds to add to hal.now() for local time, used for the
                                 hour of the day (India: 19800 = 5 h 30 min). Simulation leaves it out.
    hal.read()                -> dict of sensor readings, e.g. {"flow_lps": 0.01, "pressure_m": 10.4}
    hal.send(packet: bytes, at_s=None, cad=False)
                              -> radio send (LoRa on the real node); may be lost silently.
                                 Packets are encrypted binary (packet.py, AES-SIV).
                                 at_s: second of the minute to send at (time slot);
                                 cad: listen before talk (LoRa channel activity detection)
    hal.slot                  -> optional (index, count): this node's time slot in each minute
    hal.receive()             -> list of packets (bytes) received since the last call
    hal.key(name)             -> the 16-byte secret this node shares with the gateway; key("GATEWAY")
                                 is the building key that checks the gateway's beacons
    hal.led(colour)           -> "green" normal, "yellow" messages waiting, "blue" a warning from the
                                 gateway (tank valve shut, pipe leak on this floor), "red" alert active
    hal.valve(open)           -> tank node only, optional: open (True) or shut (False) the valve on
                                 the pipe leaving the tank
    hal.reopen_pressed()      -> tank node only, optional: True once when someone pressed the
                                 node's "reopen valve" button (after testing the water)
    hal.storage_path          -> file for the store-and-forward queue (SD card), or None;
                                 the learned baseline is kept next to it (<name>_baseline.json),
                                 and the open problems, gateway warnings and valve (<name>_open.json)

Detection rules (all thresholds are calibration knobs at the top):
    Flat node: flow first, as chosen and explained in leak_detection/README.md (flow beat
      pressure alone in leak_detection/compare_leaks.py). Continuous night flow, continuous flow
      and abnormal consumption against its own learned daily pattern (idea of on-node anomaly
      detection from Babazadeh [3]). Pressure only helps: high flow AND a pressure drop together
      for 10 min is a fast flat leak alarm (LEAK_PRESSURE_DROP, from the team's branch).
      Every problem stays open until the node sees it end and says so (FLAT_LEAK_CLEARED,
      CONSUMPTION_CLEARED); silence never closes one.
    Tank node: water quality in three separate tracks. The IS 10500 limits (pH, TDS, turbidity)
      are the main safety rule; an 8-bit neural network on the device (Sharma et al. [1], trained
      by tinyml/train_quality.py) only says what bad water looks like (runoff or chemical); and
      too warm (Legionella). Broken limits, runoff or chemical shut the tank valve at once; only a
      person reopens it, after testing the water. Also abnormal supply flow (possible pipe burst).
Store-and-forward: every message is timestamped, kept until the gateway acknowledges it,
alerts sent first (Mariadhason et al. [4], timestamped store-and-forward to SD card).
"""
import json
import os
from collections import deque

from packet import pack, unpack, violation_mask
from quality_model import QualityModel, check_standards

DAY, HOUR = 86400, 3600
LEARN_S = DAY                 # first day after the node's first start: learn its normal pattern
EWMA_A = 1 / 14               # after day 1, each clean hour moves the baseline 1/14 of the way to
                              # today's value: remembers about 2 weeks (Phyn learns in 21 days, Moen 7)
SUMMARY_S = 900               # send a summary every 15 minutes
MAX_SENDS_PER_STEP = 4        # radio duty-cycle budget per minute
RETRY_S = 120                 # resend if no acknowledgement after 2 minutes
SLOT_GUARD_S = 0.2            # start this far into our time slot, margin for clock error
# Gateway beacon (once a minute): a node knows whether the gateway can hear it
HEARD_S = 120                 # no beacon, ACK or command for 2 min (2 beacons missed): gateway away
AWAY_ALERT_S, AWAY_ROUTINE_S = 300, 900   # while away, try now and then only (the downlink alone may
                              # be broken): alerts every 5 min, summaries every 15, not every 2
FLUSH_STEP_S = 0.15           # gateway back: stored alerts go out node by node, this far apart
                              # (node index x 0.15 s, listening first); summaries in their own slots
REMEMBER_COMMANDS = 50        # gateway command numbers kept, so each command is acted on once

NIGHT_START_H, NIGHT_END_H = 1, 5
NIGHT_FLOW_LPS = 0.02         # flow above this at night counts as "someone or something using water"
NIGHT_FLOW_MIN = 30           # minutes in a row before a leak alert
HIGH_USE_FACTOR, HIGH_USE_MIN_LPS, HIGH_USE_MIN = 3.0, 0.05, 60
CONT_FLOW_LPS = 0.005         # below this the flat's water counts as "off"
CONT_FLOW_MIN = 180           # water never off for 3 h in a row: leak alert, day or night
# Measured on 3 clean days of bursty use (live/water.py): no flat ran more than 15 min nonstop (the old
# smooth curve ran up to 6 h, so this was 8 h). 3 h, not 30 min, leaves room for real long uses the model
# lacks, like filling storage drums during supply hours. ponytail: retune on Cache County data
# Pressure-assisted flat leak (team's branch): pressure below this hour's normal by more than
# max(3 x spread, 0.35 m) AND flow above max(3 x mean, mean + 3 x spread, 0.20 L/s), for 10 min
P_DROP_SIGMAS, P_DROP_MIN_M, P_FLOW_MIN_LPS, P_LEAK_MIN = 3.0, 0.35, 0.20, 10
FLAT_LEAK_RULES = {"LEAK_NIGHT_FLOW", "LEAK_CONTINUOUS_FLOW", "LEAK_PRESSURE_DROP"}
LEVEL_SIGMAS, LEVEL_MARGIN_M, LEVEL_MIN = 4.0, 0.3, 10   # tank level check
PUMP_ON_M, PUMP_DRY_LPS = 0.5, 0.1   # site setting: the pump switches on below 0.5 m. Supply problem = level
# below that AND under 0.1 L/s coming in AND low for this hour. With bursty use the tank runs low at a
# different time each day, so "low for this hour" alone flagged normal pump cycles (2 in 3 clean days)
ALERT_CLEAR_S = 600           # an alert re-arms after its condition has been normal this long;
                              # the gateway decides whether a new alert is a repeat
QUALITY_MIN, QUALITY_CLEAR_MIN = 3, 10
TEMP_UNSAFE_C = 35.0     # tank water above this is unsafe: Legionella bacteria multiply between about 25 and 45 C,
# fastest near 37 C (CDC; US National Academies 2020). Guidance asks for cold water below 20-25 C, which an Indian
# rooftop tank passes most of the year, so we flag the fast-growth range. ponytail: one fixed limit, a knob per site
SUPPLY_SIGMAS, SUPPLY_MARGIN_LPS, SUPPLY_MIN = 4.0, 0.5, 15
QUALITY_AVG_N = 3             # the limit and model checks see the average of the last 3 readings
SHUT_VALVE_ON = ("LIMIT_VIOLATION", "RUNOFF", "CHEMICAL")   # hot water only alarms: a slow risk, and
                              # shutting would leave 20 flats with no water

PRIORITY = {"ALERT": 0, "FLAG": 1, "SUM": 2}       # FLAG: only the tank's SUPPLY_LOW now


def save_atomic(path, text):
    """Write a file so a power cut leaves either the old version or the new one, never half.
    No battery on the nodes, so a power cut can land in the middle of any save."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())          # new contents really on the SD card
    os.replace(tmp, path)             # atomic swap: old file or new file, nothing in between
    fd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
    try:
        os.fsync(fd)                  # the swap itself really on the SD card
    finally:
        os.close(fd)


class Store:
    """Store-and-forward queue, mirrored to a file (the SD card) so a reboot loses nothing.
    Also owns the message counter: saved in the same file (first line), so it never repeats,
    even after a power cut with an empty queue. The gateway drops a repeated number as a
    duplicate or replay, so a counter starting again from 1 would silence the node."""

    def __init__(self, path):
        self.path, self.queue, self.last_sent, self.seq = path, [], {}, 0
        if path and os.path.exists(path):
            with open(path) as f:
                for line in f:
                    if line.strip():
                        d = json.loads(line)
                        if "k" in d:
                            self.queue.append(d)
                        else:
                            self.seq = d["seq"]
        self.seq = max([self.seq] + [m["s"] for m in self.queue])

    def _save(self):
        # ponytail: rewrites the whole file on change; switch to an append log if the queue gets long
        if self.path:
            save_atomic(self.path, json.dumps({"seq": self.seq}) + "\n"
                        + "".join(json.dumps(m) + "\n" for m in self.queue))

    def add(self, msg):
        """Give the message the next counter value and keep it until acknowledged."""
        self.seq += 1
        self.queue.append({**msg, "s": self.seq})
        self._save()

    def ack(self, seq):
        before = len(self.queue)
        self.queue = [m for m in self.queue if m["s"] != seq]
        self.last_sent.pop(seq, None)
        if len(self.queue) != before:
            self._save()

    def due(self, now, budget, heard=True):
        wait = lambda m: RETRY_S if heard else AWAY_ALERT_S if m["k"] == "ALERT" else AWAY_ROUTINE_S
        ready = [m for m in self.queue if now - self.last_sent.get(m["s"], -1e9) >= wait(m)]
        ready.sort(key=lambda m: (PRIORITY[m["k"]], m["t"]))      # alerts first, then oldest
        for m in ready[:budget]:
            self.last_sent[m["s"]] = now
        return ready[:budget]

    def flush(self):
        """The gateway is back: everything waiting may go now (still at most budget per minute)."""
        self.last_sent.clear()


class Baseline:
    """Mean and spread of one sensor for each hour of the day: learned on day 1, then kept up to
    date with an EWMA (an average that gives recent days more weight), one hour at a time."""

    def __init__(self):
        self.n, self.s, self.ss = [0] * 24, [0.0] * 24, [0.0] * 24
        self.today = [0, 0.0, 0.0]          # readings of the hour now running (after day 1)

    def add(self, hour, x):
        self.n[hour] += 1; self.s[hour] += x; self.ss[hour] += x * x

    def collect(self, x):
        t = self.today
        t[0] += 1; t[1] += x; t[2] += x * x

    def blend(self, hour, clean):
        """End of an hour: mix it in with weight EWMA_A, only if no alert was active (clean),
        so a leak is never learned as normal. Mean and spread both follow."""
        tn, ts, tss = self.today
        self.today = [0, 0.0, 0.0]
        if clean and tn and self.n[hour]:
            n = self.n[hour]
            self.s[hour] = (1 - EWMA_A) * self.s[hour] + EWMA_A * ts * n / tn
            self.ss[hour] = (1 - EWMA_A) * self.ss[hour] + EWMA_A * tss * n / tn

    def mean(self, hour):
        return self.s[hour] / max(self.n[hour], 1)

    def std(self, hour):
        m = self.mean(hour)
        return max(self.ss[hour] / max(self.n[hour], 1) - m * m, 0.0) ** 0.5


class Node:
    sensors = ()

    def __init__(self, node_id, hal):
        self.id, self.hal = node_id, hal
        self.store = Store(hal.storage_path)
        self.key, self.gw_key = hal.key(node_id), hal.key("GATEWAY")
        self.base = {k: Baseline() for k in self.sensors}
        self.base_path = hal.storage_path and os.path.splitext(hal.storage_path)[0] + "_baseline.json"
        self.learn_until = None        # end of the learning day: set on the first step, kept on the SD card
        if self.base_path and os.path.exists(self.base_path):    # restart after a power cut: keep what was learned
            with open(self.base_path) as f:
                saved = json.load(f)
            self.learn_until = saved.pop("learn_until", None)
            for k, d in saved.items():
                self.base[k].__dict__.update(d)
        self.hour = None
        self.clean = True              # no alert so far in the hour now running
        self.alerts = {}               # active alert type -> last time its condition was seen
        self.interval, self.acc = None, {}
        self.heard_at, self.last_beacon_gt, self.got = None, -1, []
        self.ok_minutes = {}           # recovery message -> minutes in a row the problem looked over
        # Saved on the SD card: problems reported and not yet reported over, warnings from the
        # gateway (name -> since), command numbers already acted on, the tank valve
        self.open, self.notices, self.done, self.valve_open = set(), {}, [], True
        self.open_path = hal.storage_path and os.path.splitext(hal.storage_path)[0] + "_open.json"
        if self.open_path and os.path.exists(self.open_path):
            with open(self.open_path) as f:
                d = json.load(f)
            self.open, self.notices, self.done, self.valve_open = set(d["open"]), d["notices"], d["done"], d["valve"]

    def save_open(self):
        if self.open_path:
            save_atomic(self.open_path, json.dumps({"open": sorted(self.open), "notices": self.notices,
                                                    "done": self.done, "valve": self.valve_open}))

    # --- messaging ---
    def emit(self, kind, **payload):
        self.store.add({"n": self.id, "t": self.hal.now(), "k": kind, **payload})

    def alert(self, kind, **payload):
        """Called every minute the condition holds; sends once per problem. The problem stays
        open (saved) until recover() reports it over."""
        if kind not in self.alerts and kind not in self.open:
            self.open.add(kind)
            self.save_open()                   # saved before the alert is queued
            self.emit("ALERT", a=kind, **payload)
        self.alerts[kind] = self.hal.now()

    def recover(self, message, rules, over):
        """Called every minute. Sends `message` once the open problems among `rules` have looked
        over for ALERT_CLEAR_S of readings in a row (team's branch). Silence never clears."""
        mine = self.open & rules
        self.ok_minutes[message] = self.ok_minutes.get(message, 0) + 1 if mine and over else 0
        if self.ok_minutes[message] >= ALERT_CLEAR_S // 60:
            self.emit("ALERT", a=message)
            for kind in mine:
                self.alerts.pop(kind, None)
            self.open -= mine
            self.save_open()
            self.ok_minutes[message] = 0

    # --- radio: ACKs, the gateway's beacon and commands ---
    def receive(self, now):
        """Returns True if the gateway was away and is heard again."""
        back = False
        for raw in self.hal.receive():
            m = unpack(raw, lambda n: self.key if n == self.id else self.gw_key if n == "GATEWAY" else None)
            if m is None:
                continue                       # not ours, or forged
            if m["k"] == "BEACON":
                if m["gt"] <= self.last_beacon_gt:
                    continue                   # an old beacon sent again
                self.last_beacon_gt = m["gt"]
            elif m["k"] == "ACK":
                self.store.ack(m["s"])
            elif m["k"] == "CMD":
                self.command(now, m)
            else:
                continue
            back = back or self.heard_at is None or now - self.heard_at > HEARD_S
            self.heard_at = now
        return back

    def command(self, now, m):
        """A warning only the gateway can give (from several nodes together). Act once, save, then
        confirm with GOT; the gateway repeats the command until a GOT arrives."""
        if m["s"] not in self.done:
            code = m["a"]
            if code.endswith("_CLEARED") or code == "VALVE_OPENED":
                self.notices.pop({"VALVE_OPENED": "VALVE_CLOSED"}.get(code, code.replace("_CLEARED", "")), None)
            else:
                self.notices[code] = m["t"]
            self.done = (self.done + [m["s"]])[-REMEMBER_COMMANDS:]
            self.save_open()
        self.got.append({"k": "GOT", "n": self.id, "s": m["s"], "t": now})

    def local(self, now):
        """Things a node does whatever the radio and the learning day: the tank's valve button."""

    def water_off(self):
        """Tank valve shut (the gateway said so): zero flow says nothing about leaks.
        ponytail: a flat cut off by radio during the shut-off cannot know; its leak may be reported
        over by mistake. The tank node's valve state in every flat's summary ACK would close that gap."""
        return "VALVE_CLOSED" in self.notices

    # --- main loop, called once per minute ---
    def step(self):
        now, r = self.hal.now(), self.hal.read()
        back = self.receive(now)
        self.local(now)
        if self.learn_until is None:       # very first start: learn for one day from now
            self.learn_until = now + LEARN_S
        hour = int((now + getattr(self.hal, "utc_offset_s", 0)) % DAY // HOUR)   # local hour of the day
        if hour != self.hour:              # once an hour: update the baseline, keep it on the SD card
            for b in self.base.values():
                b.blend(self.hour, self.clean)
            self.hour, self.clean = hour, True
            self.save_baseline()
        if now < self.learn_until:
            for k in self.sensors:
                self.base[k].add(hour, r[k])
        else:
            if not self.water_off():           # a shut tank valve is not a normal day: don't learn it
                for k in self.sensors:
                    self.base[k].collect(r[k])
            self.check(now, hour, r)
            self.alerts = {k: seen for k, seen in self.alerts.items() if now - seen < ALERT_CLEAR_S}
            # ponytail: minutes before an alert fires (up to 8 h for continuous flow) still count
            # as clean; at EWMA_A = 1/14 that moves the baseline a few percent at most
            self.clean = self.clean and not self.alerts and not self.water_off()
        self.summarise(now, r)
        # Collisions: alerts go out now, listening first (CSMA/CA); everything else waits for
        # this node's own time slot (TDMA), kept in step by the gateway time in every ACK and beacon.
        # Gateway back after an outage: stored alerts go at once, node by node 0.15 s apart.
        slot = getattr(self.hal, "slot", None)
        slot_at = slot and slot[0] * 60 / slot[1] + SLOT_GUARD_S
        if back:
            self.store.flush()
        heard = self.heard_at is not None and now - self.heard_at <= HEARD_S
        for m in self.store.due(now, MAX_SENDS_PER_STEP, heard):
            raw = pack(m, self.key)
            if m["k"] == "ALERT":
                self.hal.send(raw, at_s=slot[0] * FLUSH_STEP_S, cad=True) if back and slot else self.hal.send(raw, cad=True)
            elif slot:
                self.hal.send(raw, at_s=slot_at)
            else:
                self.hal.send(raw)
        for g in self.got:                     # confirmations of gateway commands: not stored, the
            self.hal.send(pack(g, self.key), at_s=slot_at)   # gateway repeats the command if one is lost
        self.got = []
        stuck = any(now - m["t"] > RETRY_S for m in self.store.queue)     # unanswered for > 2 min
        self.hal.led("red" if self.alerts or self.open else "blue" if self.notices else "yellow" if stuck else "green")

    def save_baseline(self):
        if self.base_path:
            save_atomic(self.base_path, json.dumps({"learn_until": self.learn_until,
                                                    **{k: b.__dict__ for k, b in self.base.items()}}))

    def summarise(self, now, r):
        i = int(now // SUMMARY_S)
        if self.interval is not None and i != self.interval:
            self.emit("SUM", i=self.interval, **{k: round(v, 4) for k, v in self.acc.items()})
            self.acc = {}
        self.interval = i
        self.accumulate(r)

    def check(self, now, hour, r):
        raise NotImplementedError

    def accumulate(self, r):
        raise NotImplementedError


class FlatNode(Node):
    sensors = ("flow_lps", "pressure_m")

    def __init__(self, node_id, hal):
        super().__init__(node_id, hal)
        self.night_run = self.high_run = self.cont_run = self.p_leak_run = 0

    def check(self, now, hour, r):
        """Leak checks (leak_detection/README.md section 5). Runs once a minute."""
        if self.water_off():
            return                             # checks frozen, not reset: a leak is still a leak after
        flow = r["flow_lps"]
        # Continuous night flow: nobody should be using water at 1-5 am
        at_night = NIGHT_START_H <= hour < NIGHT_END_H
        self.night_run = self.night_run + 1 if at_night and flow > NIGHT_FLOW_LPS else 0
        if self.night_run >= NIGHT_FLOW_MIN:
            self.alert("LEAK_NIGHT_FLOW", flow=round(flow, 4))
        # Continuous flow: a home's water is off now and then; a leak never stops. Catches small
        # leaks that start in the daytime hours before the night check, and ones below its limit
        self.cont_run = self.cont_run + 1 if flow > CONT_FLOW_LPS else 0
        if self.cont_run >= CONT_FLOW_MIN:
            self.alert("LEAK_CONTINUOUS_FLOW", flow=round(flow, 4))
        # Abnormal consumption against this flat's own daily pattern
        limit = max(HIGH_USE_FACTOR * self.base["flow_lps"].mean(hour), HIGH_USE_MIN_LPS)
        self.high_run = self.high_run + 1 if flow > limit else 0
        if self.high_run >= HIGH_USE_MIN:
            self.alert("ABNORMAL_CONSUMPTION", flow=round(flow, 4))
        # Pressure-assisted: a big leak in this flat pulls its own pressure down while its flow is
        # unusually high. Never on pressure alone (a down pipe leak lowers every flat's pressure)
        pb, fb = self.base["pressure_m"], self.base["flow_lps"]
        p_low = pb.mean(hour) - r["pressure_m"] > max(P_DROP_SIGMAS * pb.std(hour), P_DROP_MIN_M)
        f_high = flow > max(HIGH_USE_FACTOR * fb.mean(hour), fb.mean(hour) + 3 * fb.std(hour), P_FLOW_MIN_LPS)
        self.p_leak_run = self.p_leak_run + 1 if p_low and f_high else 0
        if self.p_leak_run >= P_LEAK_MIN:
            self.alert("LEAK_PRESSURE_DROP", flow=round(flow, 4), p_min=round(r["pressure_m"], 2))
        # Over only when seen to be over: a leak needs the water off (05:00 passing is not enough)
        self.recover("FLAT_LEAK_CLEARED", FLAT_LEAK_RULES, flow <= CONT_FLOW_LPS)
        self.recover("CONSUMPTION_CLEARED", {"ABNORMAL_CONSUMPTION"}, flow <= limit)

    def accumulate(self, r):
        self.acc["vol_l"] = self.acc.get("vol_l", 0.0) + r["flow_lps"] * 60
        self.acc["p_min"] = min(self.acc.get("p_min", 1e9), r["pressure_m"])
        if "pipe_lps" in r:     # flats 301, 201, 101, G01 also read the down pipe meter just above their floor
            self.acc["pipe_l"] = self.acc.get("pipe_l", 0.0) + r["pipe_lps"] * 60


QUALITY_MODEL = QualityModel()    # 8-bit network, Sharma et al. [1]; trained by tinyml/train_quality.py


class TankNode(Node):
    sensors = ("outflow_lps", "level_m")

    def __init__(self, node_id, hal):
        super().__init__(node_id, hal)
        self.supply_run = self.low_run = self.ok_run = 0
        self.low_flagged, self.qn = False, 0
        self.recent = deque(maxlen=QUALITY_AVG_N)
        # each water quality track: [bad readings in a row, good readings in a row, active label]
        self.tracks = {"limit": [0, 0, None], "cause": [0, 0, None], "hot": [0, 0, None]}
        getattr(hal, "valve", lambda is_open: None)(self.valve_open)     # after a reboot: as saved

    def set_valve(self, is_open, now):
        self.valve_open = is_open
        self.save_open()                       # saved first: a power cut keeps the valve as decided
        getattr(self.hal, "valve", lambda is_open: None)(is_open)
        self.emit("ALERT", a="VALVE_OPENED" if is_open else "VALVE_CLOSED", valve=int(is_open))

    def water_off(self):
        return not self.valve_open

    def local(self, now):
        # Reopen only by hand, and only once the water reads clean again (no limit or model track open)
        if getattr(self.hal, "reopen_pressed", lambda: False)() and not self.valve_open:
            if not (self.tracks["limit"][2] or self.tracks["cause"][2]):
                self.set_valve(True, now)

    @staticmethod
    def confirm(track, label):
        """Advance one track with this reading's label (None = fine): 'ALERT' after QUALITY_MIN bad
        readings in a row, 'CLEARED' after QUALITY_CLEAR_MIN good ones, else None."""
        if label:
            track[0], track[1] = track[0] + 1, 0
            if track[0] >= QUALITY_MIN and not track[2]:
                track[2] = label
                return "ALERT"
        else:
            track[0], track[1] = 0, track[1] + 1
            if track[2] and track[1] >= QUALITY_CLEAR_MIN:
                track[2] = None
                return "CLEARED"

    def check(self, now, hour, r):
        # Water quality: three tracks that confirm and clear on their own (team's branch), so the
        # network can never hide a broken limit. Limits and network see a 3-reading average.
        temp = r.get("temp_c")
        self.recent.append((r["ph"], r["tds_ppm"], r["turbidity_ntu"]))
        ph, tds, turb = (sum(x[i] for x in self.recent) / len(self.recent) for i in range(3))
        broken = check_standards(ph, tds, turb)
        cause = QUALITY_MODEL.classify(ph, tds, turb)
        labels = {"limit": broken and "LIMIT_VIOLATION", "cause": cause != "NORMAL" and cause,
                  "hot": temp is not None and temp > TEMP_UNSAFE_C and "HOT"}
        was_bad = any(t[2] for t in self.tracks.values())
        readings = {"ph": round(ph, 2), "tds": round(tds), "turb": round(turb, 2),
                    **({"temp": round(temp, 1)} if temp is not None else {})}
        for name, track in self.tracks.items():
            if self.confirm(track, labels[name]) == "ALERT":
                self.emit("ALERT", a=f"QUALITY_{track[2]}", **readings,
                          **({"viol": violation_mask(broken)} if name == "limit" else {}))
                if track[2] in SHUT_VALVE_ON and self.valve_open:
                    self.set_valve(False, now)          # at once, here on the node: works offline
        for track in self.tracks.values():               # LED red while any track is open
            if track[2]:
                self.alerts[f"QUALITY_{track[2]}"] = now
        if was_bad and not any(t[2] for t in self.tracks.values()):
            self.alerts = {k: v for k, v in self.alerts.items() if not k.startswith("QUALITY_")}
            self.emit("ALERT", a="QUALITY_CLEARED")
        if not self.valve_open:
            return                             # no water leaves the tank: supply checks frozen
        # Abnormal supply: far more water leaving the tank than normal for this hour
        b = self.base["outflow_lps"]
        high = r["outflow_lps"] > b.mean(hour) + max(SUPPLY_SIGMAS * b.std(hour), SUPPLY_MARGIN_LPS)
        self.supply_run = self.supply_run + 1 if high else 0
        if self.supply_run >= SUPPLY_MIN:
            self.alert("ABNORMAL_SUPPLY", flow=round(r["outflow_lps"], 3))
        self.recover("SUPPLY_FLOW_CLEARED", {"ABNORMAL_SUPPLY"}, not high)
        # Tank level far below normal for this hour: every flat's pressure will sag (supply side)
        lb = self.base["level_m"]
        low = r["level_m"] < lb.mean(hour) - max(LEVEL_SIGMAS * lb.std(hour), LEVEL_MARGIN_M) \
            and r["level_m"] < PUMP_ON_M and r["inflow_lps"] < PUMP_DRY_LPS
        self.low_run, self.ok_run = (self.low_run + 1, 0) if low else (0, self.ok_run + 1)
        if self.low_run >= LEVEL_MIN and not self.low_flagged:
            self.low_flagged = True
            self.emit("FLAG", f="SUPPLY_LOW", level=round(r["level_m"], 3), inflow=round(r["inflow_lps"], 3))
        elif self.ok_run >= LEVEL_MIN:
            self.low_flagged = False

    def accumulate(self, r):
        self.acc["vol_l"] = self.acc.get("vol_l", 0.0) + r["outflow_lps"] * 60
        self.acc["in_l"] = self.acc.get("in_l", 0.0) + r["inflow_lps"] * 60
        self.acc["level_m"] = r["level_m"]
        self.acc["valve"] = int(self.valve_open)
        # 15-minute averages of the water readings, for the dashboard's charts
        self.qn = 1 if "ph" not in self.acc else self.qn + 1
        for k, v in (("ph", r["ph"]), ("tds", r["tds_ppm"]), ("turb", r["turbidity_ntu"]), ("temp", r.get("temp_c"))):
            if v is not None:
                self.acc[k] = self.acc.get(k, v) + (v - self.acc.get(k, v)) / self.qn


if __name__ == "__main__":
    # Self-check: a power cut in the middle of a save must not lose the queue
    import tempfile
    from packet import peek, sim_key
    path = os.path.join(tempfile.mkdtemp(), "queue.jsonl")
    st = Store(path)
    st.add({"k": "ALERT", "t": 0}); st.add({"k": "SUM", "t": 60})
    real_replace = os.replace
    os.replace = lambda *a: (_ for _ in ()).throw(OSError("power cut"))   # die just before the swap
    try:
        st.add({"k": "SUM", "t": 120})
    except OSError:
        pass
    os.replace = real_replace
    assert [m["s"] for m in Store(path).queue] == [1, 2], "old queue must survive intact"
    st.add({"k": "SUM", "t": 180})                                       # power back: next save works
    assert [m["s"] for m in Store(path).queue] == [1, 2, 3, 4]
    for s in (1, 2, 3, 4):
        st.ack(s)
    assert Store(path).seq == 4 and Store(path).queue == []      # empty queue, counter remembered

    class FakeHAL:
        def __init__(self, path, flow):
            self.t, self.storage_path, self.flow = 0, path, flow
        def now(self): return self.t
        def read(self): return {"flow_lps": self.flow(self.t), "pressure_m": 10.0}
        def receive(self): return []
        def send(self, m, at_s=None, cad=False): pass
        def led(self, colour): pass
        def key(self, name): return sim_key(name)

    def run(node, until):
        while node.hal.t < until:
            node.step()
            node.hal.t += 60

    use = lambda t: 0.012 if t % DAY // HOUR in (7, 8, 9, 18, 19, 20) and t // 60 % 20 < 10 else 0.0   # mornings,
                                                                    # evenings, in spurts of 10 min on, 10 off
    tmp = tempfile.mkdtemp()

    # The baseline survives a power cut in the middle of learning
    a = FlatNode("B-101", FakeHAL(os.path.join(tmp, "B-101.jsonl"), use))
    run(a, 12 * HOUR)
    b = FlatNode("B-101", FakeHAL(os.path.join(tmp, "B-101.jsonl"), use))   # power back, new process
    nb, na = b.base["flow_lps"].n, a.base["flow_lps"].n
    assert nb[:11] == na[:11] and nb[7] == 60 and nb[11] == 0     # at most the unfinished hour is lost

    # Normal flat: no alerts. Small daytime leak (0.01 L/s from 10:00 on day 2, below the night
    # limit): continuous-flow alert after 3 h, the night check never fires
    alerts = {}
    for name, flow in (("B-102", use), ("B-103", lambda t: use(t) + (0.01 if t >= DAY + 10 * HOUR else 0))):
        n = FlatNode(name, FakeHAL(os.path.join(tmp, name + ".jsonl"), flow))
        run(n, 2 * DAY)
        alerts[name] = [(m["a"], m["t"]) for m in n.store.queue if m["k"] == "ALERT"]
    alerts["normal"], alerts["leak"] = alerts.pop("B-102"), alerts.pop("B-103")
    assert alerts["normal"] == [], alerts["normal"]
    assert [a for a, _ in alerts["leak"]] == ["LEAK_CONTINUOUS_FLOW"], alerts["leak"]
    assert alerts["leak"][0][1] == DAY + 10 * HOUR + (CONT_FLOW_MIN - 1) * 60     # 12:59, day 2

    # EWMA: habits that change are learned (about 2 weeks), a leak while alerting is not
    more = lambda t: use(t) * (2 if t >= DAY else 1)                  # mornings and evenings double
    n = FlatNode("B-201", FakeHAL(None, more))
    run(n, 15 * DAY)
    m7 = n.base["flow_lps"].mean(7)
    assert 0.006 + 0.6 * 0.006 < m7 < 0.012, m7                  # hour mean 0.006 doubles; 1-(13/14)^14 = 65% of the way
    assert n.base["flow_lps"].mean(3) == 0.0
    n.hal.flow = lambda t: 0.05                                        # burst: alerts within the hour
    run(n, 17 * DAY)
    assert any(m["k"] == "ALERT" for m in n.store.queue)
    assert n.base["flow_lps"].s[3] < 0.01 * 60, "leak hours must not be learned"

    # Only a correctly signed ACK deletes a message: a forged one (wrong key) is ignored
    hal = FakeHAL(os.path.join(tmp, "B-104.jsonl"), use)
    n = FlatNode("B-104", hal)
    n.emit("SUM", i=0, vol_l=1.0, p_min=10.0)
    ack = {"k": "ACK", "n": "B-104", "s": 1, "gt": 60}
    hal.receive = lambda: [pack(ack, b"attacker-guessed")]
    n.step()
    assert len(n.store.queue) == 1, "forged ACK must not delete the message"
    hal.receive = lambda: [pack(ack, sim_key("B-104"))]
    n.step()
    assert all(m["s"] != 1 for m in n.store.queue), "real ACK deletes it"

    # Real clock: UTC Unix seconds (not 0 at start), India 5 h 30 min ahead. 20:00 UTC is
    # 01:30 in India, so the hour is 1 (night check window); the first day is still learned
    utc_midnight = 1_764_000_000 // DAY * DAY
    hal = FakeHAL(None, use)
    hal.t, hal.utc_offset_s = utc_midnight + 20 * HOUR, 19800
    n = FlatNode("B-202", hal)
    n.step()
    assert n.hour == 1, n.hour
    assert sum(n.base["flow_lps"].n) == 1, "first reading must be learned, not checked"
    # Tank water too warm: alert after 3 minutes above the limit, clean again after 10 cooler minutes
    n = TankNode("B-TANK", FakeHAL(None, use))
    water = {"ph": 7.6, "tds_ppm": 250, "turbidity_ntu": 1.0, "outflow_lps": 0.0, "inflow_lps": 0.0, "level_m": 1.5}
    for k, temp in enumerate([30] * 5 + [38] * 5 + [30] * 12):
        n.check(k * 60, 14, {**water, "temp_c": temp})
    q = [(m["a"], m.get("temp")) for m in n.store.queue if m["k"] == "ALERT" and m["a"].startswith("QUALITY")]
    assert q == [("QUALITY_HOT", 38), ("QUALITY_CLEARED", None)], q
    assert n.valve_open, "hot water only alarms, the valve stays open"

    # Runoff in the tank: limit broken and the model says runoff -> valve shut at once; clean water
    # alone does not reopen it; a reopen press while the water is still bad does nothing; then it does
    class TankHAL(FakeHAL):
        pressed, valve_state = False, None
        def valve(self, is_open): self.valve_state = is_open
        def reopen_pressed(self):
            p, self.pressed = self.pressed, False
            return p
    path = os.path.join(tmp, "B-TANK.jsonl")
    hal = TankHAL(path, use)
    n = TankNode("B-TANK", hal)
    runoff = {**water, "ph": 6.3, "tds_ppm": 60, "turbidity_ntu": 25, "temp_c": 26}
    for k in range(6):
        n.check(k * 60, 14, runoff)
    sent = lambda: [m["a"] for m in n.store.queue if m["k"] == "ALERT"]
    assert sent()[:3] == ["QUALITY_LIMIT_VIOLATION", "VALVE_CLOSED", "QUALITY_RUNOFF"], sent()
    lim = next(m for m in n.store.queue if m.get("a") == "QUALITY_LIMIT_VIOLATION")
    assert lim["viol"] == violation_mask(["pH", "TURBIDITY"]) and hal.valve_state is False
    hal.pressed = True
    n.local(400)
    assert not n.valve_open, "no reopen while the water is still bad"
    for k in range(6, 30):
        n.check(k * 60, 14, {**water, "temp_c": 26})
    assert "QUALITY_CLEARED" in sent() and not n.valve_open, "clean water alone never reopens it"
    n2 = TankNode("B-TANK", TankHAL(path, use))            # reboot: still shut, and set so at start
    assert not n2.valve_open and n2.hal.valve_state is False
    hal.pressed = True
    n.local(2000)
    assert n.valve_open and hal.valve_state is True and sent()[-1] == "VALVE_OPENED", sent()

    # Recovery: a night leak is reported over only after 10 minutes of the water off
    leak = lambda t: 0.05 if DAY + HOUR <= t < DAY + 3 * HOUR else 0.0
    n = FlatNode("B-301", FakeHAL(None, leak))
    run(n, DAY + 3 * HOUR + 15 * 60)
    assert sent.__class__ and [m["a"] for m in n.store.queue if m["k"] == "ALERT"] == ["LEAK_NIGHT_FLOW", "FLAT_LEAK_CLEARED"]
    assert not n.open
    clr = next(m for m in n.store.queue if m.get("a") == "FLAT_LEAK_CLEARED")
    assert clr["t"] == DAY + 3 * HOUR + 9 * 60, clr["t"] / 3600

    # Valve shut (gateway warning): the flat's zero flow must not report its leak over
    n = FlatNode("B-301", FakeHAL(None, lambda t: 0.05 if DAY + HOUR <= t < DAY + 2 * HOUR or t >= DAY + 3 * HOUR else 0.0))
    run(n, DAY + 2 * HOUR)
    n.notices["VALVE_CLOSED"] = DAY + 2 * HOUR                 # valve shut 02:00-03:00
    run(n, DAY + 3 * HOUR)
    n.notices.pop("VALVE_CLOSED")
    run(n, DAY + 4 * HOUR)
    assert [m["a"] for m in n.store.queue if m["k"] == "ALERT"] == ["LEAK_NIGHT_FLOW"], n.store.queue

    # Pressure-assisted: big flow AND a pressure drop for 10 min; pressure alone never alarms
    class PHAL(FakeHAL):
        def __init__(self, flow, pres):
            super().__init__(None, flow)
            self.pres = pres
        def read(self): return {"flow_lps": self.flow(self.t), "pressure_m": self.pres(self.t)}
    big = lambda t: use(t) + (0.5 if t >= DAY + 10 * HOUR else 0)
    for flow, pres, want in ((big, lambda t: 10.0 - (1.0 if t >= DAY + 10 * HOUR else 0), "LEAK_PRESSURE_DROP"),
                             (use, lambda t: 10.0 - (1.0 if t >= DAY + 10 * HOUR else 0), None)):
        n = FlatNode("B-302", PHAL(flow, pres))
        run(n, DAY + 11 * HOUR)
        first = next((m for m in n.store.queue if m["k"] == "ALERT"), None)
        assert (first and first["a"]) == want, first
        if want:
            assert first["t"] == DAY + 10 * HOUR + 9 * 60

    # Gateway away and back: slow tries while away; on the first beacon after the outage the
    # stored alert goes at once, at node index x 0.15 s; gateway commands acted on once, GOT sent
    sends = []
    class RadioHAL(FakeHAL):
        slot, inbox = (4, 22), []
        def send(self, raw, at_s=None, cad=False): sends.append((self.t, peek(raw)["k"], at_s))
        def receive(self):
            got, self.inbox = self.inbox, []
            return got
    hal = RadioHAL(None, use)
    n = FlatNode("B-303", hal)
    beacon = lambda t: pack({"k": "BEACON", "n": "GATEWAY", "s": t // 60, "gt": t}, sim_key("GATEWAY"))
    hal.inbox = [beacon(0)]
    n.step()
    n.emit("ALERT", a="LEAK_NIGHT_FLOW", flow=0.05)
    for t in range(60, 15 * 60, 60):            # gateway silent: tried at once, then every 5 min only
        hal.t = t
        n.step()
    tries = [t for t, k, _ in sends if k == "ALERT"]
    assert tries == [60, 360, 660], tries
    hal.t, hal.inbox = 900, [beacon(890)]
    n.step()
    assert (900, "ALERT", 4 * FLUSH_STEP_S) in sends, sends[-3:]
    hal.inbox = [beacon(890)]                   # the same beacon again (replayed): ignored
    assert n.receive(960) is False and n.heard_at == 900
    cmd = pack({"k": "CMD", "n": "B-303", "s": 7, "t": 950, "a": "VALVE_CLOSED"}, sim_key("B-303"))
    for t in (960, 1020):                       # repeated (our GOT was lost): acted on once, confirmed twice
        hal.t, hal.inbox = t, [cmd]
        n.step()
    assert n.notices == {"VALVE_CLOSED": 950} and n.done == [7]
    assert [k for t, k, _ in sends if k == "GOT"] == ["GOT", "GOT"]
    hal.t, hal.inbox = 1080, [pack({"k": "CMD", "n": "B-303", "s": 8, "t": 1070, "a": "VALVE_OPENED"}, sim_key("B-303"))]
    n.step()
    assert n.notices == {}
    print("node self-check passed")
