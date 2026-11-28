"""The node's board: what the node code (the `hal` list at the top of aqua_edge.py) needs besides
the radio itself: clocks, time slot, keys, storage path, and the LEDs, buzzer and valve
(actuators.py). The radio comes from hal/pi_hal.py (or hal/udp.py without radio hardware) and the
sensor readings from edge/sensors/. Wiring: hal/README.md, "Running on the Pi".
"""
import time

from edge.actuators import GpioActuators, SimActuators
from hal.packet import peek, unpack

SAMPLE_S = 60                         # one reading a minute
CLOCK_WARN_S = 60                     # this far from the gateway's time is a clock warning
FLOW_SENSORS = ("flow_lps", "outflow_lps")    # the water that goes through this node's valve


def seconds_until(now, at_s):
    """Seconds to wait from `now` until second `at_s` of a minute (this minute's, or the next one's
    if that second has already gone)."""
    return (at_s - now % 60) % 60


class NodeBoard:
    """Two clocks. now() is the real time (UTC, set from the gateway), for messages and retries.
    sample_time() is the time of the newest readings, which every detector goes by: replayed data
    keeps its own time of day. step_s: set when stepping faster than once a minute, so sending
    does not wait for the time slot.

    radio: send(raw, cad) -> bool and receive() -> [(arrival time, raw)], as from pi_hal.open_radio().
    get_readings: () -> (time_s, readings dict), or None at the end, as from edge.sensors.sensor_reader().
    gpio: the Pi's pins; otherwise the LEDs, buzzer and valve are only simulated."""

    def __init__(self, name, radio, get_readings, storage_path, keys, slot=None, step_s=None, gpio=True):
        self.name, self.radio, self.get_readings, self.keys, self.slot = name, radio, get_readings, keys, slot
        self.sensor_t, self.readings = 0.0, {}      # the newest sample
        self.storage_path, self.step_s = storage_path, step_s
        self.clock_fix = 0.0          # gateway time minus our clock, from the last verified ACK
        self.last_ack_gt = -1         # the newest gateway time used: an older ACK is a replay
        self.sleep, self.log = time.sleep, print
        self.actuators = GpioActuators() if gpio else SimActuators()

    # --- sensors ---
    def advance(self):
        """Take the next readings. False when there are no more. A failed read is a minute with
        every reading missing; it never stops the node."""
        try:
            sample = self.get_readings()
        except Exception as e:
            self.log(f"{self.name} sensors: {e!r}")
            sample = (self.sensor_t + SAMPLE_S, {})
        if sample is None:
            return False
        self.sensor_t, self.readings = sample
        return True

    def read(self):
        if not self.actuators.valve_closed:
            return self.readings
        # recorded data cannot react to the valve, so shut means no flow through it
        return {k: 0.0 if k in FLOW_SENSORS else v for k, v in self.readings.items()}

    def sample_time(self):
        return self.sensor_t

    def now(self):
        return time.time() + self.clock_fix

    def monotonic(self):
        if "now" in self.__dict__:
            return self.now()
        return time.monotonic()

    def key(self, name):
        return self.keys[name]

    # --- radio ---
    def send(self, raw, cad=False):
        if not self.radio.send(raw, cad):              # alerts (cad): listen first, back off, then send anyway
            return False                               # radio fault: the node keeps the message and retries
        m = unpack(raw, self.key) or peek(raw)
        self.log(f"{self.now():.0f} {self.name} sent {m['k']} #{m['s']} {m.get('a', '')}")
        return True

    def receive(self):
        got = self.radio.receive()
        for arrived, raw in got:
            m = unpack(raw, lambda n: self.keys.get(n) if n in (self.name, "GATEWAY") else None)
            if m and m["k"] == "ACK" and m["gt"] > self.last_ack_gt:
                # trust the gateway time only from a genuine ACK, newer than the last one used
                self.last_ack_gt, self.clock_fix = m["gt"], m["gt"] - arrived
        return [raw for _, raw in got]

    # --- LEDs, buzzer and valve ---
    @property
    def colour(self):
        return self.actuators.colour

    def buzzer(self, code):
        """Sound the local alarm (independent of the radio and the gateway)."""
        self.actuators.beep()
        self.log(f"{self.now():.0f} {self.name} BUZZER {code}")

    def led(self, colour):
        self.led_pattern(colour, 0)

    def led_pattern(self, colour, blinks):
        """Show a colour and blink pattern. Unchanged, it is left running. The buzzer goes with red:
        it sounds, intermittently, for as long as the light is red, also after a restart."""
        if (colour, blinks) == (self.actuators.colour, self.actuators.blinks):
            return
        was_red = self.actuators.colour == "red"
        self.actuators.light(colour, blinks)
        if colour != "red":
            self.actuators.quiet()
        elif not was_red:
            self.actuators.beep()
        code = {("yellow", 1): " Y1", ("yellow", 2): " Y2", ("red", 1): " R1", ("red", 2): " R2"}.get((colour, blinks), "")
        self.log(f"{self.now():.0f} {self.name} LED {colour}{code}")

    # --- node health ---
    def link_broken(self):
        return bool(getattr(self.radio, "broken", False))

    @property
    def clock_warning(self):
        """The computer's clock is far from the gateway's (corrected here, but it should be set)."""
        return abs(self.clock_fix) > CLOCK_WARN_S

    def set_valve(self, closed):
        ok = self.actuators.valve(closed)
        self.log(f"{self.now():.0f} {self.name} VALVE {'closed' if closed else 'open'}" + ("" if ok else " FAILED"))
        return ok
