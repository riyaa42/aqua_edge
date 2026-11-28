"""What a node can act on: its three LEDs, buzzer and water valve. Real pins on a Raspberry Pi
(GpioActuators, needs gpiozero), or a stand-in that only remembers and logs (SimActuators).
Which code decides what: policy.py and aqua_edge.py. Wiring: hal/README.md, "Running on the Pi".

The valve is driven by one output that is on while the valve should be shut. With a relay and a
normally open valve, losing power opens it until the node is running again; use a latching or
fail-closed valve if that must not happen. Nothing reads back whether the valve really moved.
"""

import itertools

# One blink pattern lasts 2 s, in steps of 0.2 s (planning/valve_spec.md, section 2)
STEP_S = 0.2
PATTERNS = {0: (1,) * 10,                           # solid
            1: (1,) + (0,) * 9,                     # on 200 ms, off 1800 ms
            2: (1, 0, 1) + (0,) * 7}                # on 200, off 200, on 200, off 1400 ms

# Pins (BCM numbers). The radio's pins are in hal/lora_pi.py.
LED_PINS = {"green": 17, "yellow": 27, "red": 5}
BUZZER_PIN = 18
VALVE_PIN = 23


class SimActuators:
    def __init__(self):
        self.colour, self.blinks, self.valve_closed, self.buzzing = None, 0, False, False

    def valve(self, closed):
        self.valve_closed = closed
        return True

    def light(self, colour, blinks=0):
        self.colour, self.blinks = colour, blinks

    def beep(self):
        self.buzzing = True

    def quiet(self):
        self.buzzing = False

    def close(self):
        pass


class GpioActuators(SimActuators):
    """The same, on the Pi's pins. led, relay, buzzer: for tests; leave out on the Pi."""

    def __init__(self, led=None, relay=None, buzzer=None):
        super().__init__()
        if led is None:
            from gpiozero import LED, Buzzer, OutputDevice
            led, relay, buzzer = LED, OutputDevice, Buzzer
        self.leds = {c: led(p) for c, p in LED_PINS.items()}
        self.relay, self.buzzer = relay(VALVE_PIN), buzzer(BUZZER_PIN)

    def valve(self, closed):
        self.relay.on() if closed else self.relay.off()
        return super().valve(closed)

    def light(self, colour, blinks=0):
        super().light(colour, blinks)
        for c, dev in self.leds.items():
            dev.source = None               # stops a pattern that is running
            if c == colour and blinks:
                dev.source_delay = STEP_S   # gpiozero steps through it on its own thread
                dev.source = itertools.cycle(PATTERNS[blinks])
            else:
                dev.value = c == colour

    def beep(self):
        super().beep()
        self.buzzer.beep(on_time=0.2, off_time=1.8)

    def quiet(self):
        super().quiet()
        self.buzzer.off()
