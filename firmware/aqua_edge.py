"""Aqua Edge node firmware.

Runs unchanged in Webots and on the Raspberry Pi Zero 2 W. It never touches hardware
directly; everything goes through a small hardware layer `hal` with these methods:

    hal.now()                 -> current time in seconds; every message is stamped with it ("t").
                                 Simulation: seconds since start. Real node: from the DS3231 RTC
                                 (planned, to discuss next: UTC Unix seconds, ISO 8601 in logs).
    hal.read()                -> dict of sensor readings, e.g. {"flow_lps": 0.01, "pressure_m": 10.4}
    hal.send(msg: dict, at_s=None, cad=False)
                              -> radio send (LoRa on the real node); may be lost silently.
                                 at_s: second of the minute to send at (time slot);
                                 cad: listen before talk (LoRa channel activity detection)
    hal.slot                  -> optional (index, count): this node's time slot in each minute
    hal.receive()             -> list of dict messages received since the last call
    hal.led(colour)           -> "green" normal, "yellow" messages waiting, "red" alert active
    hal.storage_path          -> file for the store-and-forward queue (SD card), or None;
                                 the learned baseline is kept next to it (<name>_baseline.json)

Detection rules (all thresholds are calibration knobs at the top):
    Flat node: FLOW ONLY, as chosen and explained in leak_detection/README.md (flow beat
      pressure in leak_detection/compare_leaks.py: every leak caught, no false alarms).
      Continuous night flow and abnormal consumption against its own learned daily pattern
      (idea of on-node anomaly detection from Babazadeh [3]). Pressure is still read, learned
      and sent in summaries, but raises no alarms.
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
CONT_FLOW_LPS = 0.005         # below this the flat's water counts as "off"
CONT_FLOW_MIN = 480           # water never off for 8 h in a row: leak alert, day or night
# ponytail: 8 h because simulated demand is smooth (normal flats run up to 6 h 1 min nonstop);
# real use comes in bursts with zero flow between, so retune (likely shorter) on Cache County data
PRESSURE_SIGMAS, PRESSURE_MARGIN_M, PRESSURE_MIN = 4.0, 0.3, 10   # tank level check
ALERT_CLEAR_S = 600           # an alert re-arms after its condition has been normal this long;
                              # the gateway decides whether a new alert is a repeat
QUALITY_MIN, QUALITY_CLEAR_MIN = 3, 10
SUPPLY_SIGMAS, SUPPLY_MARGIN_LPS, SUPPLY_MIN = 4.0, 0.5, 15

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
    """Store-and-forward queue, mirrored to a file (the SD card) so a reboot loses nothing."""

    def __init__(self, path):
        self.path, self.queue, self.last_sent = path, [], {}
        if path and os.path.exists(path):
            with open(path) as f:
                self.queue = [json.loads(line) for line in f if line.strip()]

    def _save(self):
        # ponytail: rewrites the whole file on change; switch to an append log if the queue gets long
        if self.path:
            save_atomic(self.path, "".join(json.dumps(m) + "\n" for m in self.queue))

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
        self.base_path = hal.storage_path and os.path.splitext(hal.storage_path)[0] + "_baseline.json"
        if self.base_path and os.path.exists(self.base_path):    # restart after a power cut: keep what was learned
            with open(self.base_path) as f:
                for k, d in json.load(f).items():
                    self.base[k].__dict__.update(d)
        self.hour = None
        self.alerts = {}               # active alert type -> last time its condition was seen
        self.interval, self.acc = None, {}

    # --- messaging ---
    def emit(self, kind, **payload):
        self.seq += 1
        self.store.add({"n": self.id, "s": self.seq, "t": self.hal.now(), "k": kind, **payload})

    def alert(self, kind, **payload):
        """Called every minute the condition holds; sends once per episode."""
        if kind not in self.alerts:
            self.emit("ALERT", a=kind, **payload)
        self.alerts[kind] = self.hal.now()

    # --- main loop, called once per minute ---
    def step(self):
        now, r = self.hal.now(), self.hal.read()
        for m in self.hal.receive():
            if m.get("k") == "ACK" and m.get("n") == self.id:
                self.store.ack(m["s"])
        hour = int(now % DAY // HOUR)
        if hour != self.hour:              # once an hour: keep the learned baseline on the SD card
            self.hour = hour
            self.save_baseline()
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

    def save_baseline(self):
        if self.base_path:
            save_atomic(self.base_path, json.dumps({k: b.__dict__ for k, b in self.base.items()}))

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
        self.night_run = self.high_run = self.cont_run = 0

    def check(self, now, hour, r):
        """Leak checks, flow only (leak_detection/README.md section 5). Runs once a minute."""
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
        self.bad_class, self.low_flagged, self.quality_alerted = None, False, False

    def check(self, now, hour, r):
        q = classify_quality(r["ph"], r["tds_ppm"], r["turbidity_pct"])
        if q != "NORMAL":
            self.bad_run, self.good_run, self.bad_class = self.bad_run + 1, 0, q
            if self.bad_run >= QUALITY_MIN:
                self.alert(f"QUALITY_{q}", ph=round(r["ph"], 2), tds=round(r["tds_ppm"]))
                self.quality_alerted = True
        else:
            self.bad_run, self.good_run = 0, self.good_run + 1
            if self.quality_alerted and self.good_run >= QUALITY_CLEAR_MIN:
                self.alerts.pop(f"QUALITY_{self.bad_class}", None)  # allow a new alert next time
                self.quality_alerted = False
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


if __name__ == "__main__":
    # Self-check: a power cut in the middle of a save must not lose the queue
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), "queue.jsonl")
    st = Store(path)
    st.add({"s": 1, "k": "ALERT", "t": 0}); st.add({"s": 2, "k": "SUM", "t": 60})
    real_replace = os.replace
    os.replace = lambda *a: (_ for _ in ()).throw(OSError("power cut"))   # die just before the swap
    try:
        st.add({"s": 3, "k": "SUM", "t": 120})
    except OSError:
        pass
    os.replace = real_replace
    assert [m["s"] for m in Store(path).queue] == [1, 2], "old queue must survive intact"
    st.add({"s": 4, "k": "SUM", "t": 180})                               # power back: next save works
    assert [m["s"] for m in Store(path).queue] == [1, 2, 3, 4]

    class FakeHAL:
        def __init__(self, path, flow):
            self.t, self.storage_path, self.flow = 0, path, flow
        def now(self): return self.t
        def read(self): return {"flow_lps": self.flow(self.t), "pressure_m": 10.0}
        def receive(self): return []
        def send(self, m, at_s=None, cad=False): pass
        def led(self, colour): pass

    def run(node, until):
        while node.hal.t < until:
            node.step()
            node.hal.t += 60

    use = lambda t: 0.012 if t % DAY // HOUR in (7, 8, 9, 18, 19, 20) else 0.0    # mornings, evenings
    tmp = tempfile.mkdtemp()

    # The baseline survives a power cut in the middle of learning
    a = FlatNode("B-101", FakeHAL(os.path.join(tmp, "B-101.jsonl"), use))
    run(a, 12 * HOUR)
    b = FlatNode("B-101", FakeHAL(os.path.join(tmp, "B-101.jsonl"), use))   # power back, new process
    nb, na = b.base["flow_lps"].n, a.base["flow_lps"].n
    assert nb[:11] == na[:11] and nb[7] == 60 and nb[11] == 0     # at most the unfinished hour is lost

    # Normal flat: no alerts. Small daytime leak (0.01 L/s from 10:00 on day 2, below the night
    # limit): continuous-flow alert after 8 h, the night check never fires
    alerts = {}
    for name, flow in (("normal", use), ("leak", lambda t: use(t) + (0.01 if t >= DAY + 10 * HOUR else 0))):
        n = FlatNode(name, FakeHAL(os.path.join(tmp, name + ".jsonl"), flow))
        run(n, 2 * DAY)
        alerts[name] = [(m["a"], m["t"]) for m in n.store.queue if m["k"] == "ALERT"]
    assert alerts["normal"] == [], alerts["normal"]
    assert [a for a, _ in alerts["leak"]] == ["LEAK_CONTINUOUS_FLOW"], alerts["leak"]
    # morning use (from 07:00) runs straight into the leak, so the water has not stopped since 07:00
    assert alerts["leak"][0][1] == DAY + 7 * HOUR + (CONT_FLOW_MIN - 1) * 60      # 14:59, day 2
    print("node self-check passed")
