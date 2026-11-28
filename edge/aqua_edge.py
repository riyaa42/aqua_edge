"""Aqua Edge node firmware.

Runs unchanged in Webots; meant for the Raspberry Pi 3B. It never touches hardware
directly; everything goes through a small hardware layer `hal` with these methods:

    hal.now()                 -> current time in seconds; every message is stamped with it ("t").
                                 Simulation: seconds since start. Real node: UTC Unix seconds
                                 (from the DS3231 RTC, planned). Also times retries.
    hal.sample_time()         -> optional: the local time, in seconds, of the readings read()
                                 returns now. Every detector goes by it (hour of day, learning
                                 day, "minutes in a row"). Replayed data gives its own time.
                                 Without it: now() + hal.utc_offset_s (0 if not there)
    hal.read()                -> dict of sensor readings, e.g. {"flow_lps": 0.01, "pressure_m": 10.4}
    hal.send(packet: bytes, cad=False)
                              -> radio send now (SX1278 at 433 MHz on the real node, settings in
                                 hal/lora433.py); may be lost silently. Packets are encrypted binary
                                 (hal/packet.py). cad: listen before talk (channel activity detection)
    hal.slot                  -> optional (index, count): this node's time slot in each minute
    hal.buzzer(code)          -> optional: sound the local alarm for this alert
    hal.set_valve(closed)     -> optional: close or open the water valve; False if it failed.
                                 Without it the node has no valve and nothing pauses
    hal.on_gateway_alert(code, active)
                              -> optional: the gateway says a cross-node problem started or ended
    hal.receive()             -> list of packets (bytes) received since the last call
    hal.key(name)             -> the 16-byte secret this node shares with the gateway; "GATEWAY"
                                 is the building-wide key that signs the gateway's beacon
    hal.led(colour)           -> "green", "yellow" or "red", as policy.py decides
    hal.led_pattern(colour, blinks)   optional, instead of led(): 0 solid, 1 or 2 blinks
    hal.link_broken()         optional: True while the radio is faulty
    hal.clock_warning         optional: True while the node's clock cannot be trusted
    hal.storage_path          -> file for the store-and-forward queue (SD card), or None. Kept
                                 next to it: <name>_state.json (what the detectors have learned,
                                 no raw readings) and <name>_open.json (open problems, valve)

Detection rules (all thresholds are calibration knobs at the top):
    Flat node: night flow, continuous flow and high use (flow only), a pressure-assisted leak
      check that needs a pressure drop AND high flow on the same flat (pressure alone raises
      nothing; see detection/leakage/README.md), and the consumption detector in
      detection/consumption/detectors.py. On-node anomaly detection follows Babazadeh [3].
    Tank node: water-quality standards check, 8-bit neural network (Sharma et al. [1]) and
      temporal autoencoder (detection/quality/quality_model.py), abnormal supply flow, and low tank level.
    A missing reading restarts the "minutes in a row" count of every check that uses it.
Two entry points: step() once a minute (sensors and checks), and radio_poll(), which the HAL
calls as soon as a packet arrives and again at the time radio_poll() returns.
Alerts, lights and valve: planning/valve_spec.md, with its table in policy.py. Every red condition,
    found by the node or told by the gateway, turns the LED red and closes the valve. THE VALVE IS
    ONLY REOPENED BY HAND: a local request (valve.py) or the gateway's VALVE_OPEN order. No
    recovery reopens it (this overrides the spec's section 7).
Delivery: every message is timestamped and kept until the gateway acknowledges it (Mariadhason
et al. [4], timestamped store-and-forward to SD card). Alerts and tank flags go the moment they
are raised and are retried every few seconds. The gateway sends a beacon; when it comes back
after being away, stored alerts go at once, then the other messages one at a time.
"""
from datetime import datetime, timezone
import json
import math
import os
import random


from detection.consumption.detectors import StatisticalZeroFlowDetector
from edge import policy
from hal.link import MessageLink
from hal.lora433 import (BEACON_S, FLUSH_ROUTINE_AFTER_S, FLUSH_STEP_S, HEARD_S,  # noqa: F401
                     AirtimeBudget, airtime_s, retry_wait)
from hal.packet import CUSTOM_IDS, is_node, node_number, pack, unpack, violation_mask
from detection.quality.quality_model import QualityModel, QualityMonitor, TemporalModel, check_standards

DAY, HOUR = 86400, 3600
LEARN_S = DAY                 # first day: learn this node's normal pattern
EWMA_A = 1 / 14               # after day 1, each clean hour moves the baseline 1/14 of the way to
                              # today's value: remembers about 2 weeks (Phyn learns in 21 days, Moen 7)
SUMMARY_S = 900               # send a summary every 15 minutes
STUCK_S = 30                  # unacknowledged this long: yellow, one blink
STALE_S = 180                 # no new reading this long: the sensor data is stale (yellow, two blinks)
SLOT_GUARD_S = 0.2            # a summary starts this far into our time slot, margin for clock error
SUPPLY_STATES = {"SUPPLY_EVENT", "LEAK_TANK_DRAIN", "SUPPLY_UNCERTAIN"}    # one at a time
COMMAND_ENDS = {"PIPE_LEAK_CLEARED": {"PIPE_LEAK"}, "PIPE_LEAK_CAUSE_CLEARED": {"PIPE_LEAK_CAUSE"},
                "SUPPLY_CLEARED": SUPPLY_STATES}
REMEMBER_COMMANDS = 200
MAX_SUMMARIES = 7 * 86400 // SUMMARY_S    # kept while unsent: 7 days, as far back as the gateway's balance check goes
VALVE_ORDERS = {"VALVE_CLOSE": True, "VALVE_OPEN": False}
LED_ORDERS = {"ALERT_LED_ON": True, "ALERT_LED_OFF": False}
ORDER_SLACK_S = 1             # a command's time is sent in whole seconds


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
# so false alerts are possible: detection/leakage/validation_results.md section 5.
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
        self.total_resends, self.dropped = 0, 0       # dropped: old summaries given up for room
        self.rng = random.Random(seed)
        if path and os.path.exists(path):
            with open(path) as f:
                for line in f:
                    if line.strip():
                        d = json.loads(line)
                        if "k" in d:
                            self.queue.append(d)
                        else:
                            self.seq, self.dropped = d["seq"], d.get("dropped", 0)
        self.seq = max([self.seq] + [m["s"] for m in self.queue])

    def _save(self):
        # ponytail: rewrites the whole file on change; switch to an append log if the queue gets long
        if self.path:
            save_atomic(self.path, json.dumps({"seq": self.seq, "dropped": self.dropped}) + "\n"
                        + "".join(json.dumps(m) + "\n" for m in self.queue))

    def add(self, msg):
        """Give the message the next counter value and keep it until acknowledged. Alerts and
        flags are never given up; of the summaries only the newest MAX_SUMMARIES are kept."""
        self.seq += 1
        self.queue.append({**msg, "s": self.seq})
        summaries = [m["s"] for m in self.queue if m["k"] == "SUM"]
        if len(summaries) > MAX_SUMMARIES:
            old = set(summaries[:len(summaries) - MAX_SUMMARIES])
            self.queue = [m for m in self.queue if m["s"] not in old]
            for s in old:
                self.next_try.pop(s, None)
                self.retries.pop(s, None)
            self.dropped += len(old)
        self._save()

    def ack(self, seq):
        before = len(self.queue)
        self.queue = [m for m in self.queue if m["s"] != seq]
        self.next_try.pop(seq, None)
        self.retries.pop(seq, None)
        if len(self.queue) != before:
            self._save()

    @staticmethod
    def urgent(m):
        return m["k"] != "SUM"                  # alerts and the tank's flags

    def eligible(self):
        """What may be on air: every unacknowledged alert and flag; with none waiting, only the
        oldest summary (one at a time, so a long backlog does not flood the channel)."""
        pending = sorted((m for m in self.queue if self.urgent(m)), key=lambda m: (PRIORITY[m["k"]], m["t"]))
        if pending or not self.queue:
            return pending
        return [min(self.queue, key=lambda m: (m["t"], m["s"]))]

    def due(self, now, heard):
        """The messages to send now; each is scheduled to be sent again unless acknowledged."""
        ready = [m for m in self.eligible() if now >= self.next_try.get(m["s"], -1e18)]
        for m in ready:
            s = m["s"]
            tries = self.retries.get(s, 0)
            if tries:
                self.total_resends += 1
            self.next_try[s] = now + retry_wait(tries, self.urgent(m), heard, self.rng)
            self.retries[s] = tries + 1
        return ready

    def wake(self, now):
        """When something next needs sending, or None."""
        times = [self.next_try.get(m["s"], now) for m in self.eligible()]
        return min(times) if times else None

    def flush(self, now, index):
        """The gateway is back: send what is waiting, node by node so they do not all collide.
        Only ever brings a retry forward."""
        for m in self.queue:
            s = m["s"]
            if self.retries.get(s):
                start = now + index * FLUSH_STEP_S + (0 if self.urgent(m) else FLUSH_ROUTINE_AFTER_S)
                self.next_try[s] = min(self.next_try[s], start)
                self.retries[s] = 0


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


def saved_sample_time(storage_path):
    """The time of the last reading a node's saved state includes, or None. Replayed data
    resumes after it."""
    path = storage_path and os.path.splitext(storage_path)[0] + "_state.json"
    if not path or not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f).get("last_sample")


class Node:
    sensors = ()                   # the readings it learns a baseline of
    expected = ()                  # every reading it should get each time
    flat_checks = tank_checks = False

    def __init__(self, node_id, hal):
        self.id, self.hal = node_id, hal
        self.store = Store(hal.storage_path, seed=sum(ord(c) for c in node_id))
        self.key = hal.key(node_id)
        self.base = {k: Baseline() for k in self.sensors}
        beside = hal.storage_path and os.path.splitext(hal.storage_path)[0]
        self.state_path = beside and beside + "_state.json"
        self.old_baseline = False
        self.learn_until = None        # end of the learning day (sample time), saved to SD
        self.last_sample = None        # sample time of the last reading taken in
        self.hour = None
        self.clean = True              # no alert so far in the hour now running
        self.alerts = {}               # active alert type -> last time its condition was seen
        # Problems reported and not yet reported recovered (alert names, and SUPPLY_LOW). Kept
        # on the SD card, so a rebooted node can still send the recovery.
        self.open = set()
        # Gateway commands (cross-node problems): active ones, when each last ended, ones that
        # were already over when they arrived, and the numbers acted on (never act twice)
        self.remote, self.remote_ended, self.missed, self.commands_done = {}, {}, [], []
        # The valve: what was last wanted, why, and whether doing it failed. Orders from the
        # gateway older than the last change are ignored
        self.has_valve = hasattr(hal, "set_valve")
        self.valve = {"closed": False, "why": None, "fault": False}
        self.valve_changed, self.led_order, self.led_changed = -1e18, False, -1e18
        self.open_path = beside and beside + "_open.json"
        if self.open_path and os.path.exists(self.open_path):    # restart after a power cut
            with open(self.open_path) as f:
                saved = json.load(f)
            if isinstance(saved, list):                          # older form: open problems only
                saved = {"open": saved}
            self.open = set(saved["open"])
            self.remote, self.remote_ended = saved.get("remote", {}), saved.get("ended", {})
            self.missed, self.commands_done = saved.get("missed", []), saved.get("done", [])
            self.valve = saved.get("valve", self.valve)
            self.valve_changed = saved.get("valve_changed", self.valve_changed)
            self.led_order = saved.get("led_order", False)
            self.led_changed = saved.get("led_changed", self.led_changed)
        slot = getattr(hal, "slot", None)
        # without a slot, the radio number sets this node's turn when stored messages go out. Custom
        # numbers start again from 0, so their turns are not half a minute late
        self.index = slot[0] if slot else node_number(node_id) % CUSTOM_IDS[0] if is_node(node_id) else 0
        self.beacon_key = hal.key("GATEWAY")
        # these two keys are all this node can read: packets to or from other nodes are rejected
        self.link = MessageLink(hal, {node_id: self.key, "GATEWAY": self.beacon_key}.get)
        self.heard_at, self.gateway_boot, self.last_beacon_gt = None, None, -1
        self.airtime = AirtimeBudget()
        self.ok_minutes = {}           # recovery message -> minutes in a row its problem looked over
        self.interval, self.acc = None, {}
        self.saved_as, self.stuck = None, False
        self.reading_t, self.reading_at, self.missing = None, None, False      # sensor health, for the light

    def start(self):
        """Call once the whole node is built: load what was learned, set the valve as saved."""
        self.load_state()
        if self.has_valve:
            failed = not self.drive_valve()
            if failed != self.valve["fault"]:
                self.valve["fault"] = failed
                self.save_open()
                if failed:
                    self.emit("ALERT", a="VALVE_FAULT")

    # --- what the detectors have learned (never raw readings) ---
    def state(self):
        return {"learn_until": self.learn_until, "last_sample": self.last_sample, "hour": self.hour,
                "clean": self.clean, "alerts": self.alerts, "ok_minutes": self.ok_minutes,
                "interval": self.interval, "acc": self.acc,
                "base": {k: b.__dict__ for k, b in self.base.items()}}

    def restore(self, saved):
        for name in ("learn_until", "last_sample", "hour", "clean", "alerts", "ok_minutes", "interval", "acc"):
            setattr(self, name, saved[name])
        for k, d in saved["base"].items():
            if k in self.base:
                self.base[k].__dict__.update(d)

    def save_state(self):
        if self.state_path:
            save_atomic(self.state_path, json.dumps(self.state(), separators=(",", ":")))

    def load_state(self):
        if not self.state_path:
            return
        if os.path.exists(self.state_path):
            with open(self.state_path) as f:
                self.restore(json.load(f))
            return
        old = os.path.splitext(self.state_path)[0].removesuffix("_state") + "_baseline.json"
        if os.path.exists(old):     # saved by an earlier version: hourly baselines only
            with open(old) as f:
                saved = json.load(f)
            self.learn_until, self.old_baseline = saved.pop("learn_until", None), True
            for k, d in saved.items():
                if k in self.base:
                    self.base[k].__dict__.update(d)

    def sample_time(self):
        """Local time of the current readings: what every detector goes by."""
        given = getattr(self.hal, "sample_time", None)
        return given() if given else self.hal.now() + getattr(self.hal, "utc_offset_s", 0)

    # --- valve ---
    @property
    def shut(self):
        """Whether water is really stopped: a failed close leaves it running, a failed open not."""
        return self.has_valve and self.valve["closed"] != self.valve["fault"]

    @property
    def flat_paused(self):
        return self.flat_checks and self.shut

    @property
    def tank_paused(self):
        return self.tank_checks and self.shut

    def drive_valve(self):
        try:
            return self.hal.set_valve(self.valve["closed"]) is not False
        except Exception as e:                  # a broken driver must not stop the node
            self.note(f"valve: {e!r}")
            return False

    def set_valve(self, closed, why):
        """Close or open the valve and report it. What is wanted is saved first, so a power cut
        in between cannot lose it."""
        if not self.has_valve:
            return
        self.valve_changed = self.hal.now()
        if self.valve["closed"] == closed and not self.valve["fault"]:
            if why == "gateway":                    # now an operator's order holds it: the light stays red
                self.valve["why"] = why
            self.save_open()
            return
        self.valve = {"closed": closed, "why": why, "fault": False}
        self.save_open()
        if self.drive_valve():
            self.emit("ALERT", a="VALVE_CLOSED" if closed else "VALVE_OPENED")
            if not closed:
                self.after_reopen()
        else:
            self.valve["fault"] = True
            self.save_open()
            self.emit("ALERT", a="VALVE_FAULT")

    def open_valve(self):
        """Reopen at the node itself (a local request, edge/valve.py). With the gateway's
        VALVE_OPEN order, the only ways a valve ever reopens."""
        self.set_valve(False, "local")

    def after_reopen(self):
        """What closed the valve starts fresh, and can close it again once confirmed again."""

    def note(self, text):
        log = getattr(self.hal, "log", None)
        if log:
            log(f"{self.id} {text}")

    # --- messaging ---
    def emit(self, kind, **payload):
        sim_t = self.hal.now()
        slot = getattr(self.hal, "slot", None)
        utc_fn = getattr(self.hal, "utc_now", None)
        utc_s, rtc_ok = utc_fn() if utc_fn else (sim_t, True)
        self.store.add(
            {"n": self.id, "t": sim_t, "ts_utc": round(utc_s), "ts_iso": iso_utc(utc_s), "rtc_ok": bool(rtc_ok), "k": kind, **payload}
        )
        if kind == "SUM" and slot:      # summaries start in this node's own slot (TDMA)
            self.store.next_try[self.store.seq] = sim_t + slot[0] * 60 / slot[1] + SLOT_GUARD_S

    def alert(self, kind, **payload):
        """Called every minute the condition holds; sends once per episode."""
        if kind in RECOVERABLE:
            self.set_open(self.open | {kind})      # saved before the alert is queued
        new = kind not in self.alerts
        self.alerts[kind] = self.sample_time()
        if new:
            self.emit("ALERT", a=kind, **payload)
            if policy.level(kind) == policy.ALERT:
                self.buzz(kind)                    # the local alarm never waits for the gateway
                self.set_valve(True, kind)

    def buzz(self, code):
        buzzer = getattr(self.hal, "buzzer", None)
        if buzzer:
            buzzer(code)

    def set_open(self, problems):
        if problems != self.open:
            self.open = problems
            self.save_open()

    def save_open(self):
        if self.open_path:
            save_atomic(self.open_path, json.dumps({
                "open": sorted(self.open), "remote": self.remote, "ended": self.remote_ended,
                "missed": self.missed, "done": self.commands_done, "valve": self.valve,
                "valve_changed": self.valve_changed, "led_order": self.led_order, "led_changed": self.led_changed}))

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

    # --- radio ---
    def heard_gateway(self, now, boot=None):
        back = (self.heard_at is None or now - self.heard_at > HEARD_S
                or (boot is not None and self.gateway_boot not in (None, boot)))
        self.heard_at = now
        if boot is not None:
            self.gateway_boot = boot
        if back:
            self.store.flush(now, self.index)

    def receive(self, now):
        messages, _ = self.link.receive()       # not ours or forged: already left out
        for m in messages:
            if m["n"] == "GATEWAY":
                if m["k"] == "BEACON" and m["gt"] > self.last_beacon_gt:    # an old one: replayed
                    self.last_beacon_gt = m["gt"]
                    self.heard_gateway(now, boot=m["s"])
            elif m["k"] == "ACK":
                self.heard_gateway(now)
                self.store.ack(m["s"])
            elif m["k"] == "CMD":
                self.heard_gateway(now)
                self.command(now, m)

    def command(self, now, m):
        """A cross-node problem from the gateway. Act once, save, then confirm; the gateway
        repeats it until the confirmation arrives."""
        done = self.commands_done
        if m["s"] not in done and not (len(done) >= REMEMBER_COMMANDS and m["s"] < min(done)):
            self.apply_command(m["a"], m["t"], m.get("over"))
            self.commands_done = (done + [m["s"]])[-REMEMBER_COMMANDS:]
            self.save_open()
        self.link.send({"k": "GOT", "n": self.id, "s": m["s"], "t": now},
                       admit=lambda s: self.airtime.take(now, s, True))

    def apply_command(self, code, t, over):
        hook = getattr(self.hal, "on_gateway_alert", lambda code, active: None)

        def end(name):
            if self.remote.pop(name, None) is not None:
                hook(name, False)

        if code in VALVE_ORDERS:
            if not over and t >= self.valve_changed - ORDER_SLACK_S:
                if VALVE_ORDERS[code]:
                    self.buzz(code)
                if self.has_valve:
                    self.set_valve(VALVE_ORDERS[code], "gateway")
                    self.valve_changed = max(self.valve_changed, t)
                else:
                    self.emit("ALERT", a="VALVE_FAULT")     # there is none to move
        elif code in LED_ORDERS:
            if not over and t >= self.led_changed - ORDER_SLACK_S:
                self.led_order, self.led_changed = LED_ORDERS[code], t
                if self.led_order:
                    self.red_from_gateway(code, t)
        elif code in COMMAND_ENDS:
            for name in COMMAND_ENDS[code]:
                self.remote_ended[name] = max(t, self.remote_ended.get(name, t))
                if name in self.remote and self.remote[name] <= t:
                    end(name)
        elif over:                      # it began and ended while it could not be delivered
            self.missed = (self.missed + [[code, t]])[-20:]
        elif t > self.remote_ended.get(code, -1e18):
            if code in SUPPLY_STATES:
                if any(self.remote.get(other, t) > t for other in SUPPLY_STATES):
                    return              # a newer supply state is already in force
                for other in SUPPLY_STATES - {code}:
                    end(other)
            self.remote[code] = t
            hook(code, True)
            if policy.level(code) == policy.ALERT:
                self.red_from_gateway(code, t)

    def red_from_gateway(self, code, t):
        """A red condition from the gateway (planning/valve_spec.md, section 7): buzzer, and close
        the valve. Not if it dates from before the valve was last reopened: that was decided
        knowing less. Its end never reopens the valve."""
        self.buzz(code)
        if t >= self.valve_changed - ORDER_SLACK_S:
            self.set_valve(True, code)
            self.valve_changed = max(self.valve_changed, t)     # an older reopen signal cannot undo this

    def transmit(self, now):
        """Send what is due, set the light, and return when to call again (None: nothing waits)."""
        heard = self.heard_at is not None and now - self.heard_at <= HEARD_S
        for m in self.store.due(now, heard):
            wire = {k: v for k, v in m.items() if k not in ("ts_utc", "ts_iso", "rtc_ok")}
            urgent = self.store.urgent(m)
            if not self.link.send(wire, admit=lambda s: self.airtime.take(now, s, urgent)):     # transmit-time limit reached
                self.store.next_try[m["s"]] = now + 60
        slot = getattr(self.hal, "slot", None)
        slot_wait = slot[0] * 60 / slot[1] + SLOT_GUARD_S if slot else 0    # a summary is not sent before then
        stuck = any(now - m["t"] > STUCK_S + (slot_wait if m["k"] == "SUM" else 0) for m in self.store.queue)
        if stuck != self.stuck:
            self.stuck = stuck
            self.note("messages are waiting for the gateway" if stuck else "waiting messages delivered")
        self.show_light(now)
        return self.store.wake(now)

    def active(self):
        """Every problem in force now, for its level."""
        return set(self.alerts) | self.open | set(self.remote)

    def signal(self, now):
        """(colour, blinks) for the LED: policy.py and planning/valve_spec.md."""
        if self.reading_at is None:
            self.reading_at = now           # from here a node that gets no reading at all counts as stale
        link = getattr(self.hal, "link_broken", None)
        clock = getattr(self.hal, "utc_now", None)
        return policy.signal(
            self.active(),
            red=self.led_order or (self.valve["closed"] and self.valve["why"] == "gateway"),
            delivery=self.stuck or bool(link and link()),
            sensors=self.missing or now - self.reading_at > STALE_S or bool(clock and not clock()[1])
            or bool(getattr(self.hal, "clock_warning", False))
            or (self.has_valve and self.valve["fault"]))

    def show_light(self, now):
        colour, blinks = self.signal(now)
        pattern = getattr(self.hal, "led_pattern", None)    # a hal without blinking shows the colour
        if pattern:
            pattern(colour, blinks)
        else:
            self.hal.led(colour)

    def radio_poll(self):
        """Call when a packet arrives, and at the time this returns. Never touches the sensors."""
        now = self.hal.now()
        self.receive(now)
        return self.transmit(now)

    # --- main loop, called once per minute ---
    def step(self):
        now, td, r = self.hal.now(), self.sample_time(), self.hal.read()
        if td != self.reading_t:
            self.reading_t, self.reading_at = td, now
        self.missing = any(not _finite(r.get(k)) for k in self.expected)
        self.receive(now)
        self.observe(td, r)
        summary = self.interval
        self.summarise(now, r)
        # Save every 15 minutes of readings, with each summary, and whenever something changed,
        # so a power cut loses at most 15 minutes of learning and repeats no summary
        as_now = (int(td // SUMMARY_S), sorted(self.alerts), sorted(self.open), dict(self.valve))
        if as_now != self.saved_as or summary != self.interval:
            self.saved_as = as_now
            self.save_state()
        return self.transmit(now)           # alerts raised just now go in this same call

    def observe(self, td, r, checks=True):
        """Take one reading into the detectors: learn from it on the first day, check it after."""
        if self.old_baseline:               # an old file counts only if it is on the same clock
            self.old_baseline = False
            if self.learn_until is None or td < self.learn_until - LEARN_S - HOUR:
                self.learn_until, self.base = None, {k: Baseline() for k in self.sensors}
        if self.learn_until is None:        # very first start: learn for one day from now
            self.learn_until = td + LEARN_S
        hour = int(td % DAY // HOUR)
        if hour != self.hour:               # once an hour: update the baseline
            for b in self.base.values():
                b.blend(self.hour, self.clean)
            self.hour, self.clean = hour, True
        learning = td < self.learn_until
        paused = self.paused_sensors()      # nothing is learned from a pipe that is shut off
        for k in self.sensors:
            v = r.get(k)
            if k not in paused and v is not None and not math.isnan(v):
                (self.base[k].add if learning else self.base[k].collect)(*((hour, v) if learning else (v,)))
        if checks:
            (self.check_while_learning if learning else self.check)(td, hour, r)
        self.alerts = {k: seen for k, seen in self.alerts.items() if td - seen < ALERT_CLEAR_S}
        # ponytail: minutes before an alert fires (up to 3 h for continuous flow) still count
        # as clean; at EWMA_A = 1/14 that moves the baseline a few percent at most
        self.clean = self.clean and not self.alerts and not self.shut
        self.last_sample = td

    def prime(self, r):
        """Learn from a recorded reading of the first day: no checks, no messages, no valve.
        False, and nothing taken in, once the first day is over."""
        td = self.sample_time()
        if self.learn_until is not None and td >= self.learn_until:
            return False
        self.observe(td, r, checks=False)
        self.accumulate(r)
        self.acc = {}
        return True

    def paused_sensors(self):
        return ()

    def check_while_learning(self, td, hour, r):
        """Checks that need nothing learned."""

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
        self.flat_checks, self.tank_checks = mode in ("flat", "combined"), mode in ("tank", "combined")
        self.expected = tuple(dict.fromkeys(
            (("flow_lps", "pressure_m") if self.flat_checks else ())
            + (("level_m", "inflow_lps", "outflow_lps", "ph", "tds_ppm", "turbidity_ntu", "temp_c")
               if self.tank_checks else ())))
        super().__init__(node_id, hal)
        self.rearm = set()          # water-quality problems still there when the valve was reopened
        self._p_sum, self._p_cnt = 0.0, 0

        if mode in ("flat", "combined"):
            self.night_run = self.high_run = self.cont_run = self.p_leak_run = 0
            self.cmon = StatisticalZeroFlowDetector()
            self.flow_returned = False
        if mode in ("tank", "combined"):
            self.qmon = QualityMonitor(
                QUALITY_MODEL, temporal_model=TemporalModel(), window=3,       # Module 2: keeps history, one per node confirm_n=QUALITY_MIN, clear_n=QUALITY_CLEAR_MIN
            )
            self.supply_run = self.low_run = self.ok_run = 0
            self.low_flagged = "SUPPLY_LOW" in self.open
            self.min_level = None
            self._day1_levels = []
            self.prev_level = None
        self.start()

    # --- saved state ---
    def state(self):
        saved = {**super().state(), "p_sum": self._p_sum, "p_cnt": self._p_cnt, "rearm": sorted(self.rearm)}
        if self.flat_checks:
            c = self.cmon
            saved["flat"] = {"night_run": self.night_run, "high_run": self.high_run, "cont_run": self.cont_run,
                             "p_leak_run": self.p_leak_run, "flow_returned": self.flow_returned}
            saved["consumption"] = {
                "base": c.base.__dict__, "hour": c.hour, "clean": c.clean, "active": c.active, "last_t": c.last_t,
                "high_run": c.high_run, "night_run": c.night_run, "zero_run": c.zero_run,
                "day_mean_6h": c.day_mean_6h, "holding": sorted(c.holding),
                "win_6h": [round(v, 5) for v in c.win_6h]}     # the 6-hour check needs the whole window
        if self.tank_checks:
            q, t = self.qmon, self.qmon.temporal
            saved["tank"] = {"supply_run": self.supply_run, "low_run": self.low_run, "ok_run": self.ok_run,
                             "low_flagged": self.low_flagged, "min_level": self.min_level,
                             "prev_level": self.prev_level,
                             # the one list of raw readings: only on the first day, then dropped
                             "day1_levels": [round(v, 3) for v in self._day1_levels]}
            saved["quality"] = {
                "buf": list(q.buf), "limit_state": q.limit_state, "class_state": q.class_state,
                "t_bad_run": q.t_bad_run, "t_good_run": q.t_good_run, "temporal_active": q.temporal_active,
                "temporal": {k: getattr(t, k) for k in ("ewma_short", "ewma_long", "last_ts", "warmup_seen")}}
        return saved

    def restore(self, saved):
        super().restore(saved)
        self._p_sum, self._p_cnt, self.rearm = saved["p_sum"], saved["p_cnt"], set(saved["rearm"])
        if self.flat_checks and "flat" in saved:
            self.__dict__.update(saved["flat"])
            c, d = self.cmon, dict(saved["consumption"])
            c.base.__dict__.update(d.pop("base"))
            c.holding = set(d.pop("holding"))
            c.win_6h.clear()
            c.win_6h.extend(d.pop("win_6h"))
            c.__dict__.update(d)
        if self.tank_checks and "tank" in saved:
            tank = dict(saved["tank"])
            self._day1_levels = tank.pop("day1_levels")
            self.__dict__.update(tank)
            q, d = self.qmon, dict(saved["quality"])
            q.temporal.__dict__.update(d.pop("temporal"))
            q.buf.extend(tuple(row) for row in d.pop("buf"))
            q.__dict__.update(d)

    # --- valve ---
    def paused_sensors(self):
        return ((("flow_lps", "pressure_m") if self.flat_paused else ())
                + (("outflow_lps",) if self.tank_paused else ()))

    def after_reopen(self):
        if self.flat_checks:
            self.night_run = self.cont_run = self.high_run = self.p_leak_run = 0
            self.ok_minutes.clear()
            for kind in FLAT_LEAK_RULES | CONSUMPTION_RULES | {"PROLONGED_ZERO_FLOW"}:
                self.alerts.pop(kind, None)
        if self.tank_checks:
            self.supply_run = 0
            self.alerts.pop("ABNORMAL_SUPPLY", None)
            # still-bad water raises no new event by itself: count it again from now
            q = self.qmon
            self.rearm = set()
            for track, name in ((q.limit_state, "QUALITY_LIMIT_VIOLATION"), (q.class_state, f"QUALITY_{q.active_class}")):
                if track[2] is not None:
                    track[0] = 0
                    self.rearm.add(name)
                    self.alerts.pop(name, None)

    def active(self):
        codes = super().active()
        if self.tank_checks:                 # a quality problem lasts until the water is good again
            q = self.qmon
            codes |= ({"QUALITY_LIMIT_VIOLATION"} if q.limit_active else set())
            codes |= ({f"QUALITY_{q.active_class}"} if q.active_class else set())
            codes |= ({"QUALITY_TEMPORAL_ANOMALY"} if q.temporal_active else set())
        return codes

    def check(self, now, hour, r):
        if self.flat_checks:
            self._check_flat(now, hour, r)
        if self.tank_checks:
            self._check_quality(now, r, r.get("temp_c"))
            self._check_tank(now, hour, r)

    def check_while_learning(self, now, hour, r):
        if self.tank_checks:        # the drinking-water limits need nothing learned. Without a
            self._check_quality(now, r, None)       # temperature, only they run

    def _check_flat(self, now, hour, r):
        """Flat-side leak & consumption checks. Skips missing/NaN streams safely."""
        flow = r.get("flow_lps")
        # With the valve shut there is no flow to judge: nothing is raised and nothing counts as
        # recovered. A missing reading breaks every "minutes in a row" count too
        if self.flat_paused or not _finite(flow):
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
        self.cmon.learn_s = self.learn_until
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

    def _check_quality(self, now, r, temp_c):
        """Tank-side water quality. temp_c None: only the drinking-water limits are checked."""
        ph, tds, turb = r.get("ph"), r.get("tds_ppm"), r.get("turbidity_ntu")
        if _finite(ph) and _finite(tds) and _finite(turb):
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
            self._raise_again(ph, tds, turb)

    def _raise_again(self, ph, tds, turb):
        """After a reopening: a problem that is confirmed again is reported, and acted on, again."""
        q = self.qmon
        for track, name in ((q.limit_state, "QUALITY_LIMIT_VIOLATION"), (q.class_state, f"QUALITY_{q.active_class}")):
            if name in self.rearm and track[2] is not None and track[0] >= QUALITY_MIN:
                self.rearm.discard(name)
                extra = {}
                if name == "QUALITY_LIMIT_VIOLATION":
                    extra = {"turb": round(turb, 2), "viol": violation_mask(check_standards(ph, tds, turb)[1])}
                self.alerts.pop(name, None)
                self.alert(name, ph=round(ph, 2), tds=round(tds), **extra)
        if not q.limit_active:
            self.rearm.discard("QUALITY_LIMIT_VIOLATION")
        if not q.active_class:
            self.rearm -= {"QUALITY_CHEMICAL", "QUALITY_RUNOFF"}

    def _check_tank(self, now, hour, r):
        """Tank-side outflow and level checks. Skips missing/NaN streams safely."""
        outflow = None if self.tank_paused else r.get("outflow_lps")    # shut: nothing flows out
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
        now = self.sample_time()
        if self.flat_checks:
            flow = 0.0 if self.flat_paused else r.get("flow_lps")
            if now < self.learn_until and _finite(flow) and not self.flat_paused:
                self.cmon.learn_s = self.learn_until
                self.cmon.step(now, flow)
            if _finite(flow):
                self.acc["vol_l"] = self.acc.get("vol_l", 0.0) + flow * 60
            pres = None if self.flat_paused else r.get("pressure_m")
            if _finite(pres):
                self.acc["p_min"] = min(self.acc.get("p_min", 1e9), pres)
                self._p_sum += pres
                self._p_cnt += 1
                self.acc["p_mean"] = self._p_sum / self._p_cnt
        if self.tank_checks:
            lvl = r.get("level_m")
            if _finite(lvl):
                if self.learn_until - LEARN_S + 1800 <= now < self.learn_until:
                    self._day1_levels.append(lvl)
                elif now >= self.learn_until and self.min_level is None and self._day1_levels:
                    s = sorted(self._day1_levels)
                    self.min_level = s[int(0.10 * (len(s) - 1))]
                    self._day1_levels.clear()
                self.prev_level = lvl
                self.acc["level_m"] = lvl
            outflow = 0.0 if self.tank_paused else r.get("outflow_lps")
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
    from hal.packet import sim_key
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
        def send(self, m, cad=False): pass
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
    assert nb[:11] == na[:11] and nb[7] == 60 and 60 - 15 <= nb[11] <= 60     # at most 15 minutes are lost

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

    # Delivery. A node on a fake radio that records what is sent and when
    class RadioHAL(FakeHAL):
        def __init__(self, path=None):
            super().__init__(path, lambda t: 0.0)
            self.rx, self.tx, self.buzzed, self.hooks = [], [], [], []
        def receive(self):
            msgs, self.rx = self.rx, []
            return msgs
        def send(self, raw, cad=False):
            self.tx.append((self.t, unpack(raw, sim_key)))
        def buzzer(self, code): self.buzzed.append(code)
        def on_gateway_alert(self, code, active): self.hooks.append((code, active))

    def beacon(hal, boot=1, key="GATEWAY"):
        hal.rx.append(pack({"k": "BEACON", "n": "GATEWAY", "s": boot, "gt": hal.t}, sim_key(key)))

    def tick(node, until, beacons=False, step=0.1):
        while node.hal.t < until - 1e-9:
            if beacons and round(node.hal.t * 10) % (BEACON_S * 10) == 0:
                beacon(node.hal)
            node.radio_poll()
            node.hal.t = round(node.hal.t + step, 1)

    sent_at = lambda hal, kind: [t for t, m in hal.tx if m["k"] == kind]
    hal = RadioHAL()
    n = FlatNode("B-101", hal)
    tick(n, 20, beacons=True)
    n.alert("LEAK_NIGHT_FLOW", flow=0.1)
    assert hal.buzzed == ["LEAK_NIGHT_FLOW"], "local alarm at once, no gateway needed"
    start = hal.t
    tick(n, start + 40, beacons=True)               # gateway there, but every reply is lost
    tries = sent_at(hal, "ALERT")
    gaps = [b - a for a, b in zip(tries, tries[1:])]
    assert tries[0] == start and 1 <= gaps[0] <= 2.1 and max(gaps) <= 8.1, gaps

    # Gateway away: occasional tries only. Back: stored alerts at once, by node number, then
    # summaries one at a time, oldest first
    for i in range(3):
        n.emit("SUM", i=i, vol_l=1.0)
    away = hal.t
    tick(n, away + 300)
    quiet = [t for t in sent_at(hal, "ALERT") if t > away + 20]
    assert 8 <= len(quiet) <= 12 and not sent_at(hal, "SUM"), (len(quiet), sent_at(hal, "SUM"))
    back = hal.t
    hal.tx.clear()
    beacon(hal, boot=2)
    tick(n, back + n.index * FLUSH_STEP_S + 0.2)
    assert sent_at(hal, "ALERT") and sent_at(hal, "ALERT")[0] <= back + n.index * FLUSH_STEP_S + 0.1
    ack = lambda m: hal.rx.append(pack({"k": "ACK", "n": "B-101", "s": m["s"], "gt": hal.t}, sim_key("B-101")))
    ack(hal.tx[0][1])
    order = []
    while n.store.queue and hal.t < back + 20:
        seen = len(hal.tx)
        n.radio_poll()
        for _, m in hal.tx[seen:]:
            assert m["k"] == "SUM" and len(hal.tx) - seen == 1, "one summary at a time"
            order.append(m["i"])
            ack(m)
        hal.t = round(hal.t + 0.1, 1)
    assert order == [0, 1, 2] and not n.store.queue, (order, n.store.queue)

    # A forged beacon or an old one recorded earlier does not count as the gateway
    n.heard_at = None
    beacon(hal, key="B-102")
    hal.rx.append(pack({"k": "BEACON", "n": "GATEWAY", "s": 2, "gt": back - 100}, sim_key("GATEWAY")))
    n.radio_poll()
    assert n.heard_at is None

    # Gateway commands: acted on once, confirmed every time, kept across a reboot; a forged one is
    # ignored; one already over raises no alarm; an old "ended" does not cancel a newer problem
    path = os.path.join(tmp, "B-402.jsonl")
    hal = RadioHAL(path)
    n = FlatNode("B-402", hal)

    def command(number, code, t, over=0, key="B-402"):
        hal.rx.append(pack({"k": "CMD", "n": "B-402", "s": number, "t": t, "a": code, "over": over}, sim_key(key)))
        n.radio_poll()

    command(10, "PIPE_LEAK", 100, key="B-101")
    assert not n.remote and not hal.tx
    command(11, "PIPE_LEAK", 100)
    command(11, "PIPE_LEAK", 100)                    # repeated: the first GOT was lost
    assert hal.hooks == [("PIPE_LEAK", True)] and hal.buzzed == ["PIPE_LEAK"] and len(sent_at(hal, "GOT")) == 2
    n = FlatNode("B-402", hal)                       # power cut
    command(11, "PIPE_LEAK", 100)
    assert n.remote == {"PIPE_LEAK": 100} and len(hal.hooks) == 1
    command(12, "PIPE_LEAK_CLEARED", 90)
    assert n.remote == {"PIPE_LEAK": 100}
    command(13, "PIPE_LEAK_CLEARED", 200)
    command(14, "PIPE_LEAK", 150)                    # late: already ended
    command(15, "SUPPLY_EVENT", 300, over=1)
    assert not n.remote and n.missed == [["SUPPLY_EVENT", 300]] and hal.hooks[-1] == ("PIPE_LEAK", False)
    command(16, "SUPPLY_UNCERTAIN", 400)
    command(17, "LEAK_TANK_DRAIN", 410)
    assert list(n.remote) == ["LEAK_TANK_DRAIN"]

    # Real clock: UTC Unix seconds, India +5:30 offset (20:00 UTC = 01:30 IST -> hour 1)
    utc_midnight = 1_764_000_000 // DAY * DAY
    hal_utc = FakeHAL(None, use)
    hal_utc.t, hal_utc.utc_offset_s = utc_midnight + 20 * HOUR, 19800
    n_utc = FlatNode("B-202", hal_utc)
    n_utc.step()
    assert n_utc.hour == 1, n_utc.hour
    assert sum(n_utc.base["flow_lps"].n) == 1, "first reading must be learned, not checked"
    print("node self-check passed")
