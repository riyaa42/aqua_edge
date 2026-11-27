"""Aqua Edge node firmware.

Runs unchanged in Webots; meant for the Raspberry Pi 3B. It never touches hardware
directly; everything goes through a small hardware layer `hal` with these methods:

    hal.now()                 -> current time in seconds; every message is stamped with it ("t").
                                 Simulation: seconds since start. Real node: from the DS3231 RTC
                                 (planned, to discuss next: UTC Unix seconds, ISO 8601 in logs).
    hal.read()                -> dict of sensor readings, e.g. {"flow_lps": 0.01, "pressure_m": 10.4}
    hal.send(packet: bytes, at_s=None, cad=False)
                              -> radio send (LoRa on the real node); may be lost silently.
                                 Packets are signed binary (packet.py).
                                 at_s: second of the minute to send at (time slot);
                                 cad: listen before talk (LoRa channel activity detection)
    hal.slot                  -> optional (index, count): this node's time slot in each minute
    hal.receive()             -> list of packets (bytes) received since the last call
    hal.key(name)             -> the 16-byte secret this node shares with the gateway
    hal.led(colour)           -> "green" normal, "yellow" messages waiting, "red" alert active
    hal.storage_path          -> file for the store-and-forward queue (SD card), or None;
                                 the learned baseline is kept next to it (<name>_baseline.json)

Detection rules (all thresholds are calibration knobs at the top):
    Flat node: night flow, continuous flow and high use (flow only), a pressure-assisted leak
      check that needs a pressure drop AND high flow on the same flat (pressure alone raises
      nothing; see leak_detection/README.md), and the consumption detector in
      abnormal_consumption/detectors.py. On-node anomaly detection follows Babazadeh [3].
    Tank node: water-quality standards check, 8-bit neural network (Sharma et al. [1]) and
      temporal autoencoder (quality_model.py), abnormal supply flow, and low tank level.
    A missing reading restarts the "minutes in a row" count of every check that uses it.
Store-and-forward: every message is timestamped, kept until the gateway acknowledges it,
alerts sent first (Mariadhason et al. [4], timestamped store-and-forward to SD card).
"""
from datetime import datetime, timezone
import json
import math
import os
import random
import sys

_EDGE_DIR = os.path.dirname(os.path.abspath(__file__))
_FW_DIR = os.path.dirname(_EDGE_DIR)
_ROOT_DIR = os.path.dirname(_FW_DIR)
for _d in (_EDGE_DIR, os.path.join(_FW_DIR, "communication"), os.path.join(_ROOT_DIR, "abnormal_consumption")):
    if _d not in sys.path:
        sys.path.append(_d)

from detectors import StatisticalZeroFlowDetector
from packet import pack, unpack, violation_mask
from quality_model import QualityModel, QualityMonitor, TemporalModel

DAY, HOUR = 86400, 3600
LEARN_S = DAY                 # first day: learn this node's normal pattern
EWMA_A = 1 / 14               # after day 1, each clean hour moves the baseline 1/14 of the way to
                              # today's value: remembers about 2 weeks (Phyn learns in 21 days, Moen 7)
SUMMARY_S = 900               # send a summary every 15 minutes
MAX_SENDS_PER_STEP = 4        # radio duty-cycle budget per minute
RETRY_S = 120                 # initial retry wait (2 minutes)
BACKOFF_ALERT_S = (120, 240, 480, 900)        # alerts: 2 -> 4 -> 8 -> 15 min cap
BACKOFF_ROUTINE_S = (120, 240, 480, 960, 1800)  # summaries/flags: 2 -> 4 -> 8 -> 16 -> 30 min cap
JITTER_FRAC = 0.15            # +/- 15% jitter to prevent synchronized retry storms
SLOT_GUARD_S = 0.2            # start this far into our time slot, margin for clock error


def iso_utc(utc_s):
    return datetime.fromtimestamp(utc_s, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

NIGHT_START_H, NIGHT_END_H = 1, 5
NIGHT_FLOW_LPS = 0.02         # flow above this at night counts as "someone or something using water"
NIGHT_FLOW_MIN = 30           # minutes in a row before a leak alert
HIGH_USE_FACTOR, HIGH_USE_MIN_LPS, HIGH_USE_MIN = 3.0, 0.05, 60
CONT_FLOW_LPS = 0.005         # below this the flat's water counts as "off"
CONT_FLOW_MIN = 180           # water never off for 3 h in a row: leak alert, day or night
# Retuned from 480 min. Longest run in bursty simulated Building B: 15 min. Cache County Sites
# 006/010/020: 91 min (121.5 recorded days), but Sites 003 and 031 exceed 3 h (630 and 406 min),
# so false alerts are possible: leak_detection/validation_results.md section 5.
PRESSURE_SIGMAS, PRESSURE_MARGIN_M, PRESSURE_MIN = 4.0, 0.3, 10   # tank level check
PRESSURE_DROP_SIGMAS, PRESSURE_DROP_MIN_M, PRESSURE_LEAK_MIN = 3.0, 0.35, 10  # pressure+flow flat leak check
ALERT_CLEAR_S = 600           # an alert re-arms after its condition has been normal this long;
                              # the gateway decides whether a new alert is a repeat
QUALITY_MIN, QUALITY_CLEAR_MIN = 3, 10
SUPPLY_SIGMAS, SUPPLY_MARGIN_LPS, SUPPLY_MIN = 4.0, 0.5, 15
FLAT_LEAK_RULES = {"LEAK_NIGHT_FLOW", "LEAK_CONTINUOUS_FLOW", "LEAK_PRESSURE_DROP"}
CONSUMPTION_RULES = {"ABNORMAL_CONSUMPTION", "UNUSUAL_NIGHT_CONSUMPTION", "SUDDEN_CONSUMPTION_CHANGE"}
RECOVERABLE = FLAT_LEAK_RULES | CONSUMPTION_RULES | {"PROLONGED_ZERO_FLOW", "ABNORMAL_SUPPLY"}

PRIORITY = {"ALERT": 0, "FLAG": 1, "SUM": 2}       # FLAG: the tank's SUPPLY_LOW and SUPPLY_RECOVERED


def save_atomic(path, text):
    """Write a file so a power cut leaves either the old version or the new one, never half.
    No battery on the nodes, so a power cut can land in the middle of any save."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())          # new contents really on the SD card
    os.replace(tmp, path)             # atomic swap: old file or new file, nothing in between
    if os.name != "nt":               # POSIX: fsync parent directory; Windows does not support os.open on dirs
        fd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
        try:
            os.fsync(fd)              # the swap itself really on the SD card
        finally:
            os.close(fd)


class Store:
    """Store-and-forward queue, mirrored to a file (the SD card) so a reboot loses nothing.
    Also owns the message counter: saved in the same file (first line), so it never repeats,
    even after a power cut with an empty queue. The gateway drops a repeated number as a
    duplicate or replay, so a counter starting again from 1 would silence the node."""

    def __init__(self, path, seed=0):
        self.path, self.queue, self.next_try, self.retries, self.seq = path, [], {}, {}, 0
        self.total_resends = 0
        self.rng = random.Random(seed)
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
        self.next_try.pop(seq, None)
        self.retries.pop(seq, None)
        if len(self.queue) != before:
            self._save()

    def due(self, now, budget):
        ready = [m for m in self.queue if now >= self.next_try.get(m["s"], -1e9)]
        ready.sort(key=lambda m: (PRIORITY[m["k"]], m["t"]))      # alerts first, then oldest
        for m in ready[:budget]:
            s = m["s"]
            n_try = self.retries.get(s, 0)
            if n_try > 0:
                self.total_resends += 1
            sched = BACKOFF_ALERT_S if m["k"] == "ALERT" else BACKOFF_ROUTINE_S
            base = sched[min(n_try, len(sched) - 1)]
            wait = base * self.rng.uniform(1.0 - JITTER_FRAC, 1.0 + JITTER_FRAC)
            self.next_try[s] = now + wait
            self.retries[s] = n_try + 1
        return ready[:budget]


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
        self.store = Store(hal.storage_path, seed=sum(ord(c) for c in node_id))
        self.key = hal.key(node_id)
        self.base = {k: Baseline() for k in self.sensors}
        self.base_path = hal.storage_path and os.path.splitext(hal.storage_path)[0] + "_baseline.json"
        if self.base_path and os.path.exists(self.base_path):    # restart after a power cut: keep what was learned
            with open(self.base_path) as f:
                for k, d in json.load(f).items():
                    self.base[k].__dict__.update(d)
        self.hour = None
        self.clean = True              # no alert so far in the hour now running
        self.alerts = {}               # active alert type -> last time its condition was seen
        # Problems reported and not yet reported recovered (alert names, and SUPPLY_LOW). Kept
        # on the SD card, so a rebooted node can still send the recovery.
        self.open = set()
        self.open_path = hal.storage_path and os.path.splitext(hal.storage_path)[0] + "_open.json"
        if self.open_path and os.path.exists(self.open_path):
            with open(self.open_path) as f:
                self.open = set(json.load(f))
        self.ok_minutes = {}           # recovery message -> minutes in a row its problem looked over
        self.interval, self.acc = None, {}

    # --- messaging ---
    def emit(self, kind, **payload):
        sim_t = self.hal.now()
        utc_fn = getattr(self.hal, "utc_now", None)
        utc_s, rtc_ok = utc_fn() if utc_fn else (sim_t, True)
        self.store.add(
            {"n": self.id, "t": sim_t, "ts_utc": round(utc_s), "ts_iso": iso_utc(utc_s), "rtc_ok": bool(rtc_ok), "k": kind, **payload}
        )

    def alert(self, kind, **payload):
        """Called every minute the condition holds; sends once per episode."""
        if kind in RECOVERABLE:
            self.set_open(self.open | {kind})      # saved before the alert is queued
        if kind not in self.alerts:
            self.emit("ALERT", a=kind, **payload)
        self.alerts[kind] = self.hal.now()

    def set_open(self, problems):
        if problems != self.open:
            self.open = problems
            if self.open_path:
                save_atomic(self.open_path, json.dumps(sorted(problems)))

    def recover(self, message, rules, looks_over):
        """Called every minute. Sends `message` once the open problems among `rules` have looked
        over for ALERT_CLEAR_S of readings in a row. Silence or a missing reading never clears."""
        mine = self.open & rules
        self.ok_minutes[message] = self.ok_minutes.get(message, 0) + 1 if mine and looks_over(mine) else 0
        if self.ok_minutes[message] >= ALERT_CLEAR_S // 60:
            self.emit("ALERT", a=message)          # queued first: a power cut here only repeats it
            for kind in mine:
                self.alerts.pop(kind, None)
            self.set_open(self.open - mine)
            self.ok_minutes[message] = 0

    # --- main loop, called once per minute ---
    def step(self):
        now, r = self.hal.now(), self.hal.read()
        for raw in self.hal.receive():
            m = unpack(raw, lambda n: self.key if n == self.id else None)   # None: not ours, or forged
            if m and m["k"] == "ACK":
                self.store.ack(m["s"])
        hour = int(now % DAY // HOUR)
        if hour != self.hour:              # once an hour: update the baseline, keep it on the SD card
            for b in self.base.values():
                b.blend(self.hour, self.clean)
            self.hour, self.clean = hour, True
            self.save_baseline()
        if now < LEARN_S:
            for k in self.sensors:
                v = r.get(k)
                if v is not None and not math.isnan(v):
                    self.base[k].add(hour, v)
        else:
            for k in self.sensors:
                v = r.get(k)
                if v is not None and not math.isnan(v):
                    self.base[k].collect(v)
            self.check(now, hour, r)
            self.alerts = {k: seen for k, seen in self.alerts.items() if now - seen < ALERT_CLEAR_S}
            # ponytail: minutes before an alert fires (up to 3 h for continuous flow) still count
            # as clean; at EWMA_A = 1/14 that moves the baseline a few percent at most
            self.clean = self.clean and not self.alerts
        self.summarise(now, r)
        # Collisions: alerts go out now, listening first (CSMA/CA); everything else waits for
        # this node's own time slot (TDMA); the hardware layer must keep node clocks in step
        slot = getattr(self.hal, "slot", None)
        for m in self.store.due(now, MAX_SENDS_PER_STEP):
            wire = {k: v for k, v in m.items() if k not in ("ts_utc", "ts_iso", "rtc_ok")}
            raw = pack(wire, self.key)
            if m["k"] == "ALERT":
                self.hal.send(raw, cad=True)
            elif slot:
                self.hal.send(raw, at_s=slot[0] * 60 / slot[1] + SLOT_GUARD_S)
            else:
                self.hal.send(raw)
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


QUALITY_MODEL = QualityModel()    # Module 3: 8-bit 3-class classifier, Sharma et al. [1]
TEMPORAL_MODEL = TemporalModel()  # Module 2: 8-bit Temporal AE (Exp 2)


def _finite(val):
    return val is not None and not math.isnan(val)


class EdgeRuntime(Node):
    """Composable edge runtime supporting 'flat', 'tank', and 'combined' pipeline modes."""

    MODE_SENSORS = {
        "flat": ("flow_lps", "pressure_m"),
        "tank": ("outflow_lps", "level_m"),
        "combined": ("flow_lps", "pressure_m", "outflow_lps", "level_m"),
    }

    def __init__(self, node_id, hal, mode="combined"):
        if mode not in self.MODE_SENSORS:
            raise ValueError(f"unknown EdgeRuntime mode: {mode}")
        self.mode = mode
        self.sensors = self.MODE_SENSORS[mode]
        super().__init__(node_id, hal)
        self._p_sum, self._p_cnt = 0.0, 0

        if mode in ("flat", "combined"):
            self.night_run = self.high_run = self.cont_run = self.p_leak_run = 0
            self.cmon = StatisticalZeroFlowDetector()
            self.flow_returned = False
        if mode in ("tank", "combined"):
            self.qmon = QualityMonitor(
                QUALITY_MODEL, temporal_model=TEMPORAL_MODEL, window=3, confirm_n=QUALITY_MIN, clear_n=QUALITY_CLEAR_MIN
            )
            self.supply_run = self.low_run = self.ok_run = 0
            self.low_flagged = "SUPPLY_LOW" in self.open
            self.min_level = None
            self._day1_levels = []
            self.prev_level = None

    def check(self, now, hour, r):
        if self.mode in ("flat", "combined"):
            self._check_flat(now, hour, r)
        if self.mode in ("tank", "combined"):
            self._check_tank(now, hour, r)

    def _check_flat(self, now, hour, r):
        """Flat-side leak & consumption checks. Skips missing/NaN streams safely."""
        flow = r.get("flow_lps")
        if not _finite(flow):       # a missing reading breaks every "minutes in a row" count
            self.night_run = self.cont_run = self.high_run = self.p_leak_run = 0
            self.ok_minutes.clear()
            return
        at_night = NIGHT_START_H <= hour < NIGHT_END_H
        self.night_run = self.night_run + 1 if at_night and flow > NIGHT_FLOW_LPS else 0
        if self.night_run >= NIGHT_FLOW_MIN:
            self.alert("LEAK_NIGHT_FLOW", flow=round(flow, 4))
        self.cont_run = self.cont_run + 1 if flow > CONT_FLOW_LPS else 0
        if self.cont_run >= CONT_FLOW_MIN:
            self.alert("LEAK_CONTINUOUS_FLOW", flow=round(flow, 4))
        limit = max(HIGH_USE_FACTOR * self.base["flow_lps"].mean(hour), HIGH_USE_MIN_LPS)
        self.high_run = self.high_run + 1 if flow > limit else 0
        if self.high_run >= HIGH_USE_MIN:
            self.alert("ABNORMAL_CONSUMPTION", flow=round(flow, 4))
        pres = r.get("pressure_m")
        if _finite(pres):
            p_base = self.base["pressure_m"]
            f_base = self.base["flow_lps"]
            p_drop = p_base.mean(hour) - pres
            p_low = p_drop > max(PRESSURE_DROP_SIGMAS * p_base.std(hour), PRESSURE_DROP_MIN_M)
            f_high = flow > max(HIGH_USE_FACTOR * f_base.mean(hour), f_base.mean(hour) + 3.0 * f_base.std(hour), 0.20)
            self.p_leak_run = self.p_leak_run + 1 if (p_low and f_high) else 0
            if self.p_leak_run >= PRESSURE_LEAK_MIN:
                self.alert("LEAK_PRESSURE_DROP", flow=round(flow, 4), p_min=round(pres, 2))
        else:
            self.p_leak_run = 0
        for ev_type, _ in self.cmon.step(now, flow):
            if ev_type != "UNUSUAL_HIGH_CONSUMPTION":
                self.alert(ev_type, flow=round(flow, 4))

        # Recovery. A flat leak needs the water to be off: 05:00 passing or the pressure coming
        # back does not mean the leak stopped.
        self.recover("FLAT_LEAK_CLEARED", FLAT_LEAK_RULES, lambda _: flow <= CONT_FLOW_LPS)
        still = self.cmon.holding | ({"ABNORMAL_CONSUMPTION"} if flow > limit else set())
        self.recover("CONSUMPTION_CLEARED", CONSUMPTION_RULES, lambda mine: not mine & still)
        if "PROLONGED_ZERO_FLOW" not in self.open:
            self.flow_returned = False
        elif flow > 0.002:                          # the detector's "no flow" limit
            self.flow_returned = True
        self.recover("ZERO_FLOW_CLEARED", {"PROLONGED_ZERO_FLOW"},
                     lambda _: self.flow_returned and self.cmon.zero_run < self.cmon.active_zero_min)

    def _check_tank(self, now, hour, r):
        """Tank-side water quality & outflow/level checks. Skips missing/NaN streams safely."""
        ph, tds, turb = r.get("ph"), r.get("tds_ppm"), r.get("turbidity_ntu")
        if _finite(ph) and _finite(tds) and _finite(turb):
            temp_c = r.get("temp_c")          # missing: only the standards check runs
            _, _, _, limit_ev, class_ev, t_ev = self.qmon.update(temp_c, ph, tds, turb, ts_s=now)
            cleared = False
            for ev in (limit_ev, class_ev):   # limit violation first: it is the authoritative one
                if not ev:
                    continue
                kind, name, avg, violations = ev
                if kind == "ALERT":
                    extra = {}
                    if name == "LIMIT_VIOLATION":
                        extra = {"turb": round(avg[3], 2), "viol": violation_mask(violations)}
                    self.alert(f"QUALITY_{name}", ph=round(avg[1], 2), tds=round(avg[2]), **extra)
                else:
                    self.alerts.pop(f"QUALITY_{name}", None)
                    cleared = True
            # one QUALITY_CLEARED, only when neither a limit nor a class alert is still active
            if cleared and not self.qmon.limit_active and self.qmon.active_class is None:
                self.emit("ALERT", a="QUALITY_CLEARED")
            if t_ev:
                t_kind, t_score, _ = t_ev
                if t_kind == "TEMPORAL_ALERT":
                    self.alert("QUALITY_TEMPORAL_ANOMALY", score=round(t_score, 3))
                elif t_kind == "TEMPORAL_CLEARED":
                    self.alerts.pop("QUALITY_TEMPORAL_ANOMALY", None)
                    self.emit("ALERT", a="QUALITY_TEMPORAL_CLEARED")

        outflow = r.get("outflow_lps")
        if _finite(outflow):
            b = self.base["outflow_lps"]
            high = outflow > b.mean(hour) + max(SUPPLY_SIGMAS * b.std(hour), SUPPLY_MARGIN_LPS)
            self.supply_run = self.supply_run + 1 if high else 0
            if self.supply_run >= SUPPLY_MIN:
                self.alert("ABNORMAL_SUPPLY", flow=round(outflow, 3))
            self.recover("SUPPLY_FLOW_CLEARED", {"ABNORMAL_SUPPLY"}, lambda _: not high)
        else:
            self.supply_run = 0
            self.ok_minutes["SUPPLY_FLOW_CLEARED"] = 0

        lvl = r.get("level_m")
        if _finite(lvl):
            lb = self.base["level_m"]
            floor_m = (self.min_level - 0.03) if self.min_level is not None else (
                lb.mean(hour) - max(PRESSURE_SIGMAS * lb.std(hour), PRESSURE_MARGIN_M)
            )
            low = lvl < min(floor_m, lb.mean(hour) - PRESSURE_MARGIN_M)
            self.low_run, self.ok_run = (self.low_run + 1, 0) if low else (0, self.ok_run + 1)
            inflow = r.get("inflow_lps", 0.0)
            inflow_v = inflow if _finite(inflow) else 0.0
            if self.low_run >= PRESSURE_MIN and not self.low_flagged:
                self.low_flagged = True
                self.set_open(self.open | {"SUPPLY_LOW"})
                self.emit("FLAG", f="SUPPLY_LOW", level=round(lvl, 3), inflow=round(inflow_v, 3))
            elif self.ok_run >= PRESSURE_MIN and self.low_flagged:
                self.low_flagged = False
                self.emit("FLAG", f="SUPPLY_RECOVERED", level=round(lvl, 3), inflow=round(inflow_v, 3))
                self.set_open(self.open - {"SUPPLY_LOW"})
        else:
            self.low_run = self.ok_run = 0

    def accumulate(self, r):
        now = self.hal.now()
        if self.mode in ("flat", "combined"):
            flow = r.get("flow_lps")
            if now < LEARN_S and _finite(flow):
                self.cmon.step(now, flow)
            if _finite(flow):
                self.acc["vol_l"] = self.acc.get("vol_l", 0.0) + flow * 60
            pres = r.get("pressure_m")
            if _finite(pres):
                self.acc["p_min"] = min(self.acc.get("p_min", 1e9), pres)
                self._p_sum += pres
                self._p_cnt += 1
                self.acc["p_mean"] = self._p_sum / self._p_cnt
        if self.mode in ("tank", "combined"):
            lvl = r.get("level_m")
            if _finite(lvl):
                if 1800 <= now < LEARN_S:
                    self._day1_levels.append(lvl)
                elif now >= LEARN_S and self.min_level is None and self._day1_levels:
                    s = sorted(self._day1_levels)
                    self.min_level = s[int(0.10 * (len(s) - 1))]
                    self._day1_levels.clear()
                self.prev_level = lvl
                self.acc["level_m"] = lvl
            outflow = r.get("outflow_lps")
            if self.mode == "tank" and _finite(outflow):
                self.acc["vol_l"] = self.acc.get("vol_l", 0.0) + outflow * 60
            inflow = r.get("inflow_lps")
            if _finite(inflow):
                self.acc["in_l"] = self.acc.get("in_l", 0.0) + inflow * 60

    def summarise(self, now, r):
        i = int(now // SUMMARY_S)
        if self.interval is not None and i != self.interval:
            self.emit("SUM", i=self.interval, **{k: round(v, 4) for k, v in self.acc.items()})
            self.acc = {}
            self._p_sum, self._p_cnt = 0.0, 0
        self.interval = i
        self.accumulate(r)


class FlatNode(EdgeRuntime):
    """Physical flat deployment: flow + pressure -> leak + abnormal consumption."""
    def __init__(self, node_id, hal):
        super().__init__(node_id, hal, mode="flat")


class TankNode(EdgeRuntime):
    """Physical tank deployment: level/inflow/outflow + water-quality probes."""
    def __init__(self, node_id, hal):
        super().__init__(node_id, hal, mode="tank")


if __name__ == "__main__":
    # Self-check: a power cut in the middle of a save must not lose the queue
    import tempfile
    from packet import sim_key
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

    use = lambda t: 0.024 if (t % DAY // HOUR in (7, 8, 9, 18, 19, 20) and (t // 60) % 20 < 10) else 0.0    # bursty mornings, evenings (mean 0.012)
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
    assert alerts["leak"][0][1] == DAY + 10 * HOUR + (CONT_FLOW_MIN - 1) * 60      # 3 h after leak starts at 10:00, day 2

    # A sensor dropout breaks "minutes in a row": 100 + 80 min of flow with a gap is not 3 h
    gap = lambda t: 0.01 if 0 <= t - DAY - 10 * HOUR < 100 * 60 or 0 <= t - DAY - 20 * HOUR < 80 * 60 else (
        math.nan if DAY + 10 * HOUR + 100 * 60 <= t < DAY + 20 * HOUR else 0.0)
    n = FlatNode("B-103", FakeHAL(None, gap))
    run(n, 2 * DAY)
    assert not any(m["k"] == "ALERT" and m["a"] == "LEAK_CONTINUOUS_FLOW" for m in n.store.queue)

    # Recovery: a leak running past 05:00 stays open; turning the water off for 10 minutes clears
    # it, also after a reboot; a missing reading restarts the 10 minutes
    leak = {"on": True}
    flow = lambda t: 0.05 if leak["on"] and t >= DAY + HOUR else (math.nan if t == leak.get("gap") else 0.0)
    path = os.path.join(tmp, "B-301.jsonl")
    n = FlatNode("B-301", FakeHAL(path, flow))
    sent = lambda node: [m["a"] for m in node.store.queue if m["k"] == "ALERT"]
    run(n, DAY + 7 * HOUR)
    assert "LEAK_NIGHT_FLOW" in sent(n) and "FLAT_LEAK_CLEARED" not in sent(n), sent(n)
    n = FlatNode("B-301", FakeHAL(path, flow))                          # power cut
    n.hal.t, leak["on"], leak["gap"] = DAY + 7 * HOUR, False, DAY + 7 * HOUR + 5 * 60
    run(n, DAY + 7 * HOUR + 15 * 60)
    assert "FLAT_LEAK_CLEARED" not in sent(n), "the gap restarted the count"
    run(n, DAY + 7 * HOUR + 17 * 60)
    assert sent(n).count("FLAT_LEAK_CLEARED") == 1 and not n.open & FLAT_LEAK_RULES, sent(n)

    # High use, then back to normal: cleared. A day without water, then water again: cleared
    start = DAY + 10 * HOUR
    n = FlatNode("B-302", FakeHAL(None, lambda t: 0.3 if start <= t < start + 2 * HOUR else use(t)))
    run(n, start + 2 * HOUR)
    assert "ABNORMAL_CONSUMPTION" in sent(n) and "CONSUMPTION_CLEARED" not in sent(n), sent(n)
    run(n, start + 9 * HOUR)                      # the 6-hour average has to come down too
    assert "CONSUMPTION_CLEARED" in sent(n) and not n.open & CONSUMPTION_RULES, (sent(n), n.open)
    away = lambda t: 0.0 if DAY <= t < 2 * DAY + 2 * HOUR else use(t)
    n = FlatNode("B-303", FakeHAL(None, away))
    run(n, 2 * DAY + 2 * HOUR)
    assert "PROLONGED_ZERO_FLOW" in sent(n) and "ZERO_FLOW_CLEARED" not in sent(n), sent(n)
    run(n, 2 * DAY + 9 * HOUR)
    assert "ZERO_FLOW_CLEARED" in sent(n), sent(n)

    # Tank: abnormal outflow and low level are both reported over, also by a rebooted node
    class TankHAL(FakeHAL):
        def read(self):
            bad = start <= self.t < start + HOUR
            return {"outflow_lps": 1.5 if bad else 0.1, "level_m": 0.5 if bad else 2.0, "inflow_lps": 0.0}

    path = os.path.join(tmp, "B-TANK.jsonl")
    n = TankNode("B-TANK", TankHAL(path, None))
    run(n, start + HOUR)
    reports = lambda node: [m.get("a") or m.get("f") for m in node.store.queue if m["k"] in ("ALERT", "FLAG")]
    assert reports(n) == ["SUPPLY_LOW", "ABNORMAL_SUPPLY"], reports(n)
    n = TankNode("B-TANK", TankHAL(path, None))                         # power cut
    assert n.low_flagged
    n.hal.t = start + HOUR
    run(n, start + HOUR + 20 * 60)
    assert sorted(reports(n)[2:]) == ["SUPPLY_FLOW_CLEARED", "SUPPLY_RECOVERED"] and not n.open, reports(n)

    # EWMA: habits that change are learned (about 2 weeks), a leak while alerting is not
    more = lambda t: use(t) * (2 if t >= DAY else 1)                  # mornings and evenings double
    n = FlatNode("B-201", FakeHAL(None, more))
    run(n, 15 * DAY)
    m7 = n.base["flow_lps"].mean(7)
    assert 0.012 + 0.6 * 0.012 < m7 < 0.024, m7                  # 1-(13/14)^14 = 65% of the way
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

    # Exponential backoff + priority: during 3.5h outage, total_resends is far lower than 105/item
    st_bo = Store(None, seed=42)
    st_bo.add({"k": "SUM", "t": 0})
    st_bo.add({"k": "ALERT", "t": 0})
    sends_by_kind = {"SUM": 0, "ALERT": 0}
    for t_s in range(0, 210 * 60, 60):
        for m in st_bo.due(t_s, 4):
            sends_by_kind[m["k"]] += 1
    assert sends_by_kind["SUM"] <= 12 and sends_by_kind["ALERT"] <= 18, sends_by_kind
    assert sends_by_kind["ALERT"] >= sends_by_kind["SUM"], "alerts should retry at least as often as summaries"
    print("node self-check passed")
