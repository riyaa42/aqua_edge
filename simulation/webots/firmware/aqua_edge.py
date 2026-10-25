"""Aqua Edge node firmware.

Runs unchanged in Webots and on the Raspberry Pi Zero 2 W. It never touches hardware
directly; everything goes through a small hardware layer `hal` with these methods:

    hal.now()                 -> seconds since start (from the DS3231 RTC on the real node)
    hal.read()                -> dict of sensor readings, e.g. {"flow_lps": 0.01, "pressure_m": 10.4}
    hal.send(msg: dict, at_s=None, cad=False)
                              -> radio send (LoRa on the real node); may be lost silently.
                                 at_s: second of the minute to send at (time slot);
                                 cad: listen before talk (LoRa channel activity detection)
    hal.slot                  -> optional (index, count): this node's time slot in each minute
    hal.receive()             -> list of dict messages received since the last call
    hal.set_valve(closed, reason)
    hal.led(colour)           -> "green" normal, "yellow" messages waiting, "red" alert active
    hal.storage_path          -> file for the store-and-forward queue (SD card), or None

Detection rules (all thresholds are calibration knobs at the top):
    Flat node: continuous night flow and abnormal consumption against its own learned
      daily pattern (our step 4, idea of on-node anomaly detection from Babazadeh [3]);
      pressure drop with valve closure unless the gateway calls a supply event
      (Alghamdi et al. [2], pressure-drop leak rule and smart valve closure).
    Tank node: water quality class (Sharma et al. [1], 8-bit neural network on the device;
      a rule-based stand-in for now) and abnormal supply flow (possible pipe burst).
Store-and-forward: every message is timestamped, kept until the gateway acknowledges it,
alerts sent first (Mariadhason et al. [4], timestamped store-and-forward to SD card).
"""
import json
import os

DAY, HOUR = 86400, 3600
LEARN_S = DAY                 # first day: learn this node's normal pattern
SUMMARY_S = 900               # send a summary every 15 minutes
MAX_SENDS_PER_STEP = 4        # radio duty-cycle budget per minute
RETRY_S = 120                 # resend if no acknowledgement after 2 minutes
SLOT_GUARD_S = 0.2            # start this far into our time slot, margin for clock error

NIGHT_START_H, NIGHT_END_H = 1, 5
NIGHT_FLOW_LPS = 0.02         # flow above this at night counts as "someone or something using water"
NIGHT_FLOW_MIN = 30           # minutes in a row before a leak alert
HIGH_USE_FACTOR, HIGH_USE_MIN_LPS, HIGH_USE_MIN = 3.0, 0.05, 60
PRESSURE_SIGMAS, PRESSURE_MARGIN_M, PRESSURE_MIN = 4.0, 0.3, 10
PRESSURE_GRACE_S = 600        # wait this long for the gateway's supply-event verdict before closing
ALERT_CLEAR_S = 600           # an alert re-arms after its condition has been normal this long;
                              # the gateway decides whether a new alert is a repeat
QUALITY_MIN, QUALITY_CLEAR_MIN = 3, 10
SUPPLY_SIGMAS, SUPPLY_MARGIN_LPS, SUPPLY_MIN = 4.0, 0.5, 15

PRIORITY = {"ALERT": 0, "FLAG": 1, "SUM": 2}


class Store:
    """Store-and-forward queue, mirrored to a file (the SD card) so a reboot loses nothing."""

    def __init__(self, path):
        self.path, self.queue, self.last_sent = path, [], {}
        if path and os.path.exists(path):
            with open(path) as f:
                self.queue = [json.loads(line) for line in f if line.strip()]

    def _save(self):
        # ponytail: rewrites the whole file on change; switch to an append log if the queue gets long
        if self.path:
            with open(self.path, "w") as f:
                f.writelines(json.dumps(m) + "\n" for m in self.queue)

    def add(self, msg):
        self.queue.append(msg)
        self._save()

    def ack(self, seq):
        before = len(self.queue)
        self.queue = [m for m in self.queue if m["s"] != seq]
        self.last_sent.pop(seq, None)
        if len(self.queue) != before:
            self._save()

    def due(self, now, budget):
        ready = [m for m in self.queue if now - self.last_sent.get(m["s"], -1e9) >= RETRY_S]
        ready.sort(key=lambda m: (PRIORITY[m["k"]], m["t"]))      # alerts first, then oldest
        for m in ready[:budget]:
            self.last_sent[m["s"]] = now
        return ready[:budget]


class Baseline:
    """Mean and spread of one sensor for each hour of the day, learned on day 1."""

    def __init__(self):
        self.n, self.s, self.ss = [0] * 24, [0.0] * 24, [0.0] * 24

    def add(self, hour, x):
        self.n[hour] += 1; self.s[hour] += x; self.ss[hour] += x * x

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
        self.seq = max([m["s"] for m in self.store.queue], default=0)
        self.base = {k: Baseline() for k in self.sensors}
        self.alerts = {}               # active alert type -> last time its condition was seen
        self.interval, self.acc = None, {}
        self.valve_closed = False

    # --- messaging ---
    def emit(self, kind, **payload):
        self.seq += 1
        self.store.add({"n": self.id, "s": self.seq, "t": self.hal.now(), "k": kind, **payload})

    def alert(self, kind, **payload):
        """Called every minute the condition holds; sends once per episode."""
        if kind not in self.alerts:
            self.emit("ALERT", a=kind, **payload)
        self.alerts[kind] = self.hal.now()

    def close_valve(self, reason):
        if not self.valve_closed:
            self.valve_closed = True
            self.hal.set_valve(True, reason)

    def open_valve(self, reason):
        if self.valve_closed:
            self.valve_closed = False
            self.hal.set_valve(False, reason)

    # --- main loop, called once per minute ---
    def step(self):
        now, r = self.hal.now(), self.hal.read()
        for m in self.hal.receive():
            if m.get("k") == "ACK" and m.get("n") == self.id:
                self.store.ack(m["s"])
            elif m.get("k") == "SUPPLY":
                self.on_supply_event(m)
        hour = int(now % DAY // HOUR)
        if now < LEARN_S:
            for k in self.sensors:
                self.base[k].add(hour, r[k])
        else:
            self.check(now, hour, r)
            self.alerts = {k: seen for k, seen in self.alerts.items() if now - seen < ALERT_CLEAR_S}
        self.summarise(now, r)
        # Collisions: alerts go out now, listening first (CSMA/CA); everything else waits for
        # this node's own time slot (TDMA), kept in step by the gateway time in every ACK
        slot = getattr(self.hal, "slot", None)
        for m in self.store.due(now, MAX_SENDS_PER_STEP):
            if m["k"] == "ALERT":
                self.hal.send(m, cad=True)
            elif slot:
                self.hal.send(m, at_s=slot[0] * 60 / slot[1] + SLOT_GUARD_S)
            else:
                self.hal.send(m)
        stuck = any(now - m["t"] > RETRY_S for m in self.store.queue)     # unanswered for > 2 min
        self.hal.led("red" if self.alerts else "yellow" if stuck else "green")

    def summarise(self, now, r):
        i = int(now // SUMMARY_S)
        if self.interval is not None and i != self.interval:
            self.emit("SUM", i=self.interval, **{k: round(v, 4) for k, v in self.acc.items()})
            self.acc = {}
        self.interval = i
        self.accumulate(r)

    def on_supply_event(self, m):
        pass

    def check(self, now, hour, r):
        raise NotImplementedError

    def accumulate(self, r):
        raise NotImplementedError


class FlatNode(Node):
    sensors = ("flow_lps", "pressure_m")

    def __init__(self, node_id, hal):
        super().__init__(node_id, hal)
        self.night_run = self.high_run = self.low_run = self.ok_run = 0
        self.pressure_flag_t = None    # when this node flagged a pressure drop
        self.supply_event_t = -1e9

    def check(self, now, hour, r):
        flow, p = r["flow_lps"], r["pressure_m"]
        # Continuous night flow: nobody should be using water at 1-5 am
        at_night = NIGHT_START_H <= hour < NIGHT_END_H
        self.night_run = self.night_run + 1 if at_night and flow > NIGHT_FLOW_LPS else 0
        if self.night_run >= NIGHT_FLOW_MIN:
            self.alert("LEAK_NIGHT_FLOW", flow=round(flow, 4))
            self.close_valve("continuous night flow")
        # Abnormal consumption against this flat's own daily pattern
        limit = max(HIGH_USE_FACTOR * self.base["flow_lps"].mean(hour), HIGH_USE_MIN_LPS)
        self.high_run = self.high_run + 1 if flow > limit else 0
        if self.high_run >= HIGH_USE_MIN:
            self.alert("ABNORMAL_CONSUMPTION", flow=round(flow, 4))
        # Pressure drop (Alghamdi et al. [2]): flag it, close only if the gateway does not call a supply event
        b = self.base["pressure_m"]
        low = p < b.mean(hour) - max(PRESSURE_SIGMAS * b.std(hour), PRESSURE_MARGIN_M)
        self.low_run, self.ok_run = (self.low_run + 1, 0) if low else (0, self.ok_run + 1)
        if self.pressure_flag_t is None:
            if self.low_run >= PRESSURE_MIN:                 # 1. low for 10 min: tell the gateway, start waiting
                self.pressure_flag_t = now
                self.emit("FLAG", f="PRESSURE_LOW", p=round(p, 3))
        elif self.ok_run >= PRESSURE_MIN:                    # 4. pressure normal again: episode over
            self.pressure_flag_t = None
        elif self.supply_event_t >= self.pressure_flag_t - PRESSURE_GRACE_S:
            pass                                             # 2. gateway says supply event: no valve action
        elif now - self.pressure_flag_t >= PRESSURE_GRACE_S and flow > limit:
            # 3. no supply verdict AND this flat is using far more water than normal: a leak here.
            #    Pressure alone is not enough: a slowly draining tank lowers every flat's pressure,
            #    hours apart, while their flow stays normal (found in the offline test).
            self.alert("LEAK_PRESSURE_DROP", p=round(p, 3), flow=round(flow, 4))
            self.close_valve("pressure drop with high flow")

    def on_supply_event(self, m):
        self.supply_event_t = self.hal.now()

    def accumulate(self, r):
        self.acc["vol_l"] = self.acc.get("vol_l", 0.0) + r["flow_lps"] * 60
        self.acc["p_min"] = min(self.acc.get("p_min", 1e9), r["pressure_m"])


def classify_quality(ph, tds, turbidity_pct):
    """Stand-in for the 8-bit neural network of Sharma et al. [1]; same three classes."""
    # ponytail: threshold rules; replace with the quantized model once trained on real readings
    if tds > 600 or ph > 8.5:
        return "CHEMICAL"
    if tds < 120 and ph < 6.8:
        return "RUNOFF"
    return "NORMAL"


class TankNode(Node):
    sensors = ("outflow_lps", "level_m")

    def __init__(self, node_id, hal):
        super().__init__(node_id, hal)
        self.bad_run = self.good_run = self.supply_run = self.low_run = self.ok_run = 0
        self.bad_class, self.low_flagged = None, False

    def check(self, now, hour, r):
        q = classify_quality(r["ph"], r["tds_ppm"], r["turbidity_pct"])
        if q != "NORMAL":
            self.bad_run, self.good_run, self.bad_class = self.bad_run + 1, 0, q
            if self.bad_run >= QUALITY_MIN:
                self.alert(f"QUALITY_{q}", ph=round(r["ph"], 2), tds=round(r["tds_ppm"]))
                self.close_valve(f"water quality {q.lower()}")
        else:
            self.bad_run, self.good_run = 0, self.good_run + 1
            if self.valve_closed and self.good_run >= QUALITY_CLEAR_MIN:
                self.alerts.pop(f"QUALITY_{self.bad_class}", None)  # allow a new alert next time
                self.open_valve("water quality back to normal")
                self.emit("ALERT", a="QUALITY_CLEARED")
        # Abnormal supply: far more water leaving the tank than normal for this hour
        b = self.base["outflow_lps"]
        high = r["outflow_lps"] > b.mean(hour) + max(SUPPLY_SIGMAS * b.std(hour), SUPPLY_MARGIN_LPS)
        self.supply_run = self.supply_run + 1 if high else 0
        if self.supply_run >= SUPPLY_MIN:
            self.alert("ABNORMAL_SUPPLY", flow=round(r["outflow_lps"], 3))
        # Tank level far below normal for this hour: every flat's pressure will sag (supply side)
        lb = self.base["level_m"]
        low = r["level_m"] < lb.mean(hour) - max(PRESSURE_SIGMAS * lb.std(hour), PRESSURE_MARGIN_M)
        self.low_run, self.ok_run = (self.low_run + 1, 0) if low else (0, self.ok_run + 1)
        if self.low_run >= PRESSURE_MIN and not self.low_flagged:
            self.low_flagged = True
            self.emit("FLAG", f="SUPPLY_LOW", level=round(r["level_m"], 3), inflow=round(r["inflow_lps"], 3))
        elif self.ok_run >= PRESSURE_MIN:
            self.low_flagged = False

    def accumulate(self, r):
        self.acc["vol_l"] = self.acc.get("vol_l", 0.0) + r["outflow_lps"] * 60
        self.acc["in_l"] = self.acc.get("in_l", 0.0) + r["inflow_lps"] * 60
        self.acc["level_m"] = r["level_m"]
