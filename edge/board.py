"""The Raspberry Pi node's board: what the node code (the `hal` list at the top of aqua_edge.py)
needs besides the radio itself: clock, time slot, keys, storage path, LEDs and buzzer. The radio
comes from hal/pi_hal.py and the sensor readings from a source such as bogo/csv_replay.py.
Wiring: hal/README.md, "Running on the Pi".
"""
import time

from hal.packet import peek, unpack

UTC_OFFSET_S = 19800                  # India: local time is UTC + 5 h 30 min

# Pins (BCM numbers). The radio's pins are in hal/lora_pi.py.
LED_PINS = {"green": 17, "yellow": 27, "red": 5}
BUZZER_PIN = 18


def seconds_until(now, at_s):
    """Seconds to wait from `now` until second `at_s` of a minute (this minute's, or the next one's
    if that second has already gone)."""
    return (at_s - now % 60) % 60


class NodeBoard:
    """step_s=None: real time (UTC clock, India hours, wait for the time slot).
    step_s set: fast mode for demos: the clock is the sensor source's time (local, starts at midnight).

    radio: send(raw, cad) -> bool and receive() -> [(arrival time, raw)], as from pi_hal.open_radio().
    sensors: advance() -> bool, read() -> dict, and .t, as bogo.csv_replay.CSVReplay."""

    def __init__(self, name, radio, sensors, storage_path, keys, slot=None, step_s=None, gpio=True):
        self.name, self.radio, self.sensors, self.keys, self.slot = name, radio, sensors, keys, slot
        self.storage_path, self.step_s = storage_path, step_s
        self.utc_offset_s = 0 if step_s is not None else UTC_OFFSET_S
        self.clock_fix = 0.0          # gateway time minus our clock, from the last verified ACK
        self.last_ack_gt = -1         # the newest gateway time used: an older ACK is a replay
        self.sleep, self.log = time.sleep, print
        self.colour, self.leds, self.buzzer_dev = None, {}, None
        if gpio:
            from gpiozero import LED, Buzzer
            self.leds = {c: LED(p) for c, p in LED_PINS.items()}
            self.buzzer_dev = Buzzer(BUZZER_PIN)

    # --- sensors ---
    def advance(self):
        return self.sensors.advance()

    def read(self):
        return self.sensors.read()

    def now(self):
        return self.sensors.t if self.step_s is not None else time.time() + self.clock_fix

    def key(self, name):
        return self.keys[name]

    # --- radio ---
    def send(self, raw, at_s=None, cad=False):
        if not cad and at_s is not None and self.step_s is None:
            # ponytail: blocks the step until our slot; fine at one step a minute
            self.sleep(seconds_until(self.now(), at_s))
        if not self.radio.send(raw, cad):              # alerts (cad): listen first, back off, then send anyway
            return                                     # radio fault: the node keeps the message and retries
        m = unpack(raw, self.key) or peek(raw)
        self.log(f"{self.now():.0f} {self.name} sent {m['k']} #{m['s']} {m.get('a', '')}")

    def receive(self):
        got = self.radio.receive()
        for arrived, raw in got:
            m = unpack(raw, lambda n: self.keys.get(n) if n in (self.name, "GATEWAY") else None)
            if m and m["k"] == "ACK" and self.step_s is None and m["gt"] > self.last_ack_gt:
                # trust the gateway time only from a genuine ACK, newer than the last one used
                self.last_ack_gt, self.clock_fix = m["gt"], m["gt"] - arrived
        return [raw for _, raw in got]

    # --- LEDs and buzzer ---
    def buzzer(self, code):
        """Sound local alarm for an edge-detected anomaly (independent of LoRa/Internet)."""
        if self.buzzer_dev:
            self.buzzer_dev.beep(on_time=0.2, off_time=1.8)
        self.log(f"{self.now():.0f} {self.name} BUZZER {code}")

    def led(self, colour):
        if colour == self.colour:
            return
        self.colour = colour
        for c, dev in self.leds.items():
            dev.value = c == colour
        if self.buzzer_dev and colour != "red":
            self.buzzer_dev.off()
        self.log(f"{self.now():.0f} {self.name} LED {colour}")
