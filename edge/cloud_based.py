"""Node entry point, cloud-based solution (the control case, planning/baselines.md, Baseline A): the
Pi only streams its raw sensor readings and does what the server says with its valve, light and
buzzer. Every detector and every decision is on the server (gateway/cloud_host.py).

    uv run prod-cloud-edge B-203 --keys aqua_keys.json --csv sensors.csv --sd /home/pi/aqua --slot 5 20
    python3 -m edge.cloud_based B-203 --keys aqua_keys.json --sd ./b203 \\
        --radio udp --gateway 127.0.0.1:1700 --actuators sim --prime-s 86400               # no hardware

No detection code is loaded here, and nothing here judges a reading. What the node does by itself:

- keeps every reading until the server confirms it, and sends them in order, one at a time (the
  newest 7 days are kept);
- shows what it is told. Only it can know that its messages are not getting through, so then the
  light is yellow with one blink, unless it is already red;
- **with no server, it decides nothing**: the valve stays as it is, whatever the readings;
- reopens its valve at once when asked at the node (edge/valve.py), and tells the server.

It reports its valve only after moving it, or failing to. Not tested on real hardware.
"""
import json
import math
import os

from edge.loop import _wait_with_radio, node_parser, open_board, run, stop_on_signals
from edge.store import ORDER_SLACK_S, STUCK_S, Store, save_atomic
from hal.link import MessageLink
from hal.lora433 import HEARD_S, AirtimeBudget
from hal.packet import DEV_HAND, DEV_PRIME, DEV_SHUT, LAMPS, READING_FIELDS, SET_CLOSE, SET_OPEN_ORDER
from hal.pi_hal import RX_POLL_S

SAMPLE_S = 60
MAX_SAMPLES = 7 * 86400 // SAMPLE_S     # kept while unsent: 7 days, as long as an edge node keeps summaries
ORDER_MAX_AGE_S = 900       # an operator's reopen older than this is not carried out (as gateway/core.py)
SLOT_GUARD_S = 0.2
STREAM_POLL_S = 0.02        # network radio: one message waits for each ACK, so look often
FLOAT32_MAX = 3.4e38


class SampleStore(Store):
    """The same queue on the SD card, but strictly in order: the server's detectors count readings
    "in a row", so one message at a time, oldest first, each retried as quickly as an alert."""

    limit = MAX_SAMPLES

    @staticmethod
    def droppable(m):
        return "a" not in m and not m.get("dev", 0) & DEV_HAND      # readings, never valve reports

    @staticmethod
    def urgent(m):
        return True

    def eligible(self):
        return self.queue[:1]


class ThinNode:
    def __init__(self, name, board):
        self.id, self.hal = name, board
        self.store = SampleStore(board.storage_path, seed=sum(ord(c) for c in name))
        keys = {name: board.key(name), "GATEWAY": board.key("GATEWAY")}
        self.link = MessageLink(board, keys.get)
        self.airtime = AirtimeBudget()
        self.heard_at, self.gateway_boot, self.last_beacon_gt = None, None, -1
        self.stuck = False
        slot = board.slot
        self.index = slot[0] if slot else 0
        beside = board.storage_path and os.path.splitext(board.storage_path)[0]
        self.state_path = beside and beside + "_state.json"
        # valve: what was last asked for. vrev, set: the newest valve decision and SET carried out.
        # hand_at: when it was last opened here. prime_until: readings before this only teach.
        self.saved = {"valve": False, "fault": False, "vrev": 0, "set": 0, "hand_at": -1e18,
                      "lamp": None, "last_sample": None, "prime_until": None}
        if self.state_path and os.path.exists(self.state_path):
            with open(self.state_path) as f:
                self.saved.update(json.load(f))
        failed = not self._drive(self.saved["valve"])       # after a power cut: as it was
        if failed != self.saved["fault"]:
            self.saved["fault"] = failed
            self.save_state()
            if failed:
                self.report("VALVE_FAULT")

    def save_state(self):
        if self.state_path:
            save_atomic(self.state_path, json.dumps(self.saved))

    def note(self, text):
        self.hal.log(f"{self.id} {text}")

    def _mono(self):
        return self.hal.monotonic()

    @property
    def shut(self):
        return self.hal.actuators.valve_closed

    # --- what is sent ---
    def _queue(self, payload):
        waiting = bool(self.store.queue)
        self.store.add({"n": self.id, "t": self.hal.now(), "k": "RAW", **payload}, mono_now=self._mono())
        slot, mono = self.hal.slot, self._mono()
        if SampleStore.droppable(payload):
            if slot and not waiting:        # readings start in this node's own slot (TDMA)
                self.store.next_try[self.store.seq] = mono + slot[0] * 60 / slot[1] + SLOT_GUARD_S
        elif self.store.queue:              # a valve report does not wait for a slot
            first = self.store.queue[0]["s"]
            self.store.next_try[first] = min(self.store.next_try.get(first, mono), mono)

    def report(self, code=None, hand=False):
        dev = (DEV_SHUT if self.shut else 0) | (DEV_HAND if hand else 0)
        self._queue({"st": int(round(self.hal.sample_time())), "dev": dev, **({"a": code} if code else {})})

    def step(self, send=True):
        """One reading: queue it as it is."""
        td, readings = self.hal.sample_time(), self.hal.read()
        fields = {READING_FIELDS[k]: v for k, v in readings.items()
                  if k in READING_FIELDS and not (math.isfinite(v) and abs(v) > FLOAT32_MAX)}
        until = self.saved["prime_until"]
        dev = (DEV_SHUT if self.shut else 0) | (DEV_PRIME if until is not None and td < until else 0)
        self._queue({"st": int(round(td)), "dev": dev, **fields})
        self.saved["last_sample"] = td          # a restart carries on after this reading
        self.save_state()
        return self.transmit() if send else None

    # --- what is received ---
    def radio_poll(self):
        """Call when a packet arrives, and at the time this returns."""
        self.receive()
        return self.transmit()

    def receive(self):
        mono = self._mono()
        messages, _ = self.link.receive()       # not ours or forged: already left out
        for m in messages:
            if m["n"] == "GATEWAY":
                if m["k"] == "BEACON" and m["gt"] > self.last_beacon_gt:
                    self.last_beacon_gt = m["gt"]
                    self._heard(mono, boot=m["s"])
            elif m["k"] == "ACK":
                self._heard(mono)
                self.store.ack(m["s"])
            elif m["k"] == "SET":
                self._heard(mono)
                self.apply(m)
                self.link.send({"k": "GOT", "n": self.id, "s": m["s"], "t": self.hal.now()},
                               admit=lambda s: self.airtime.take(mono, s, True))

    def _heard(self, mono, boot=None):
        back = (self.heard_at is None or mono - self.heard_at > HEARD_S
                or (boot is not None and self.gateway_boot not in (None, boot)))
        self.heard_at = mono
        if boot is not None:
            self.gateway_boot = boot
        if back:
            self.store.flush(mono, self.index)

    def apply(self, m):
        """Show what the server wants now. Only a newer SET counts (an old one may be replayed), and
        the valve moves only for a new valve decision: vrev 0 means the server has made none."""
        if m["s"] <= self.saved["set"]:
            return
        self.saved["set"] = m["s"]
        self.saved["lamp"] = m["lamp"]
        if m["vrev"] and m["vrev"] != self.saved["vrev"]:
            self.saved["vrev"] = m["vrev"]
            self._obey(m["valve"], m["vt"])
        self.save_state()
        if m.get("beep"):
            self.hal.buzzer("server")
        self.show()

    def _obey(self, code, decided):
        close = code == SET_CLOSE
        if close and decided < self.saved["hand_at"] - ORDER_SLACK_S:
            self.note("valve: the order to close is older than the opening by hand: ignored")
        elif code == SET_OPEN_ORDER and self.hal.now() - decided > ORDER_MAX_AGE_S:
            self.note("valve: the order to open is too old: ignored")
        elif close != self.saved["valve"] or self.saved["fault"]:
            self.saved["valve"], self.saved["fault"] = close, False
            self.save_state()                   # saved first: a power cut cannot lose what is wanted
            self.saved["fault"] = not self._drive(close)
            self.report("VALVE_FAULT" if self.saved["fault"] else "VALVE_CLOSED" if close else "VALVE_OPENED")

    def _drive(self, closed):
        try:
            return self.hal.set_valve(closed) is not False
        except Exception as e:                  # a broken driver must not stop the node
            self.note(f"valve: {e!r}")
            return False

    def open_valve(self):
        """Reopen by hand, at the node (edge/valve.py). At once; the server is told."""
        self.saved["hand_at"] = self.hal.now()
        moved = self.saved["valve"] or self.saved["fault"]
        code = None
        if moved:
            self.saved["valve"], self.saved["fault"] = False, False
            self.save_state()
            self.saved["fault"] = not self._drive(False)
            code = "VALVE_FAULT" if self.saved["fault"] else "VALVE_OPENED"
        self.save_state()
        self.report(code, hand=True)

    # --- light and delivery ---
    def show(self):
        lamp = self.saved["lamp"]
        colour, blinks = LAMPS[lamp] if lamp is not None and lamp < len(LAMPS) else ("green", 0)
        if colour != "red":
            if self.stuck or self.hal.link_broken():
                colour, blinks = "yellow", 1
            elif self.hal.clock_warning and colour == "green":
                colour, blinks = "yellow", 2
        self.hal.led_pattern(colour, blinks)

    def transmit(self):
        mono = self._mono()
        heard = self.heard_at is not None and mono - self.heard_at <= HEARD_S
        for m in self.store.due(mono, heard):
            if not self.link.send(m, admit=lambda s: self.airtime.take(mono, s, True)):
                self.store.next_try[m["s"]] = mono + 5      # radio fault or transmit-time limit
        for m in self.store.queue:
            self.store.queued_at.setdefault(m["s"], mono)
        slot = self.hal.slot
        allowed = STUCK_S + (slot[0] * 60 / slot[1] + SLOT_GUARD_S if slot else 0)
        stuck = any(mono - self.store.queued_at[m["s"]] > allowed for m in self.store.queue)
        if stuck != self.stuck:
            self.stuck = stuck
            self.note("messages are waiting for the server" if stuck else "waiting messages delivered")
        self.show()
        return self.store.wake(mono)


def prime(node, board):
    """Queue the rest of the first day at once, marked "learn only". True if a reading beyond it
    has been read and still waits to be used."""
    while board.advance():
        if board.sample_time() >= node.saved["prime_until"]:
            return True
        node.step(send=False)
    return False


def main(argv=None):
    ap = node_parser("Run an Aqua Edge node (cloud-based solution: streams readings, decides nothing).")
    a = ap.parse_args(argv)
    board, _, resume_after, radio = open_board(a, ap, suffix=".cloud")
    node = ThinNode(a.name, board)

    stop = stop_on_signals()
    poll_s = STREAM_POLL_S if a.radio == "udp" else RX_POLL_S
    print(f"{a.name} running: radio {a.radio}{f' to {a.gateway}' if a.radio == 'udp' else ''}, "
          f"actuators {a.actuators}, streaming to the server")
    try:
        first_taken = False
        if a.prime_s and resume_after is None and board.advance():
            node.saved["prime_until"] = board.sample_time() + a.prime_s
            node.step(send=False)
            first_taken = prime(node, board)
        run(node, board, a.step_s, stop, a.sd, poll_s, first_taken)
        if not stop.is_set() and not a.exit_at_end:
            print(f"{a.name}: end of the sensor data; still sending and doing what the server says")
            _wait_with_radio(node, board, None, stop, a.sd, poll_s)
    finally:
        node.save_state()
        getattr(radio, "close", lambda: None)()         # the valve is left as it is
        print(f"{a.name} stopped")


if __name__ == "__main__":
    main()
