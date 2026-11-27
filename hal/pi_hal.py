"""Hardware layer for a real Aqua Edge node: Raspberry Pi (Zero 2 W, or a Pi 4 for testing) with an
SX1278 LoRa module (433 MHz, e.g. Ra-02) on SPI. Same interface as the simulated layers (top of edge/aqua_edge.py).

The sensors are simulated: readings come row by row from a CSV with one column per node and
sensor ("B-203:flow_lps"), the format simulation/webots/make_scenario.py writes. Radio, clock,
SD card, LEDs and buzzer are real. Wiring and setup: hal/README.md, "Running on the Pi".

    python3 -m hal.pi_hal B-203 --keys aqua_keys.json --sd /home/pi/aqua --slot 5 20   # real time
    python3 -m hal.pi_hal B-203 --keys aqua_keys.json --sd /home/pi/aqua --step-s 1     # fast demo
    python3 -m hal.pi_hal --radio-test      # is the LoRa module wired right?
    python3 -m hal.pi_hal --check           # self-check on any computer, no hardware needed
"""
import argparse
import csv
import os
import random
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(HERE)

from edge.aqua_edge import FlatNode, TankNode  # noqa: E402
from hal.lora433 import FREQ_HZ, PREAMBLE, SF, TX_DBM  # noqa: E402
from hal.packet import load_keys, peek, unpack  # noqa: E402

DEFAULT_CSV = os.path.join(_ROOT_DIR, "simulation", "webots", "data", "sensors.csv")
UTC_OFFSET_S = 19800                  # India: local time is UTC + 5 h 30 min

# Radio settings. The gateway's ESP8266 + SX1278 bridge must use exactly the same ones.
BW_BITS, CR_BITS = 0x70, 0x02         # bandwidth 125 kHz, coding rate 4/5 (FREQ_HZ/SF/PREAMBLE/TX_DBM from lora433)
SYNC_WORD = 0x12                      # private LoRa network (LoRaWAN uses 0x34)

# Same listen-before-talk rule as the simulation (simulation/live/radio.py)
CAD_TRIES, CAD_BACKOFF_S = 5, (0.1, 1.0)
RX_POLL_S = 0.01                      # how often the receive thread checks the radio

# Pins (BCM numbers). SPI0 is fixed: MOSI 10, MISO 9, SCK 11, NSS = CE0 (8).
RESET_PIN = 22
LED_PINS = {"green": 17, "yellow": 27, "red": 5}
BUZZER_PIN = 18

# SX1278 registers and modes (SX1276/77/78/79 datasheet, section 6.4: same registers for the whole family)
REG_FIFO, REG_OPMODE, REG_FRF, REG_PA, REG_LNA = 0x00, 0x01, 0x06, 0x09, 0x0C
REG_FIFO_PTR, REG_TX_BASE, REG_RX_BASE, REG_RX_CURRENT = 0x0D, 0x0E, 0x0F, 0x10
REG_IRQ, REG_RX_BYTES, REG_MODEM1, REG_MODEM2, REG_PREAMBLE = 0x12, 0x13, 0x1D, 0x1E, 0x20
REG_PAYLOAD_LEN, REG_MODEM3, REG_SYNC, REG_VERSION = 0x22, 0x26, 0x39, 0x42
SLEEP, STANDBY, TX, RX_CONTINUOUS, CAD = 0x88, 0x89, 0x8B, 0x8D, 0x8F   # 0x80: LoRa mode bit, 0x08: low-frequency band (SX1278, below 525 MHz)
IRQ_RX_DONE, IRQ_CRC_ERROR, IRQ_TX_DONE, IRQ_CAD_DONE, IRQ_CAD_BUSY = 0x40, 0x20, 0x08, 0x04, 0x01


class SX1278:
    """Minimal LoRa driver: send, listen-before-talk (CAD) and receive, over SPI.
    ponytail: polls the IRQ register over SPI instead of wiring DIO0 to an interrupt; wire DIO0
    and use gpiozero's when_activated if packets are ever missed."""

    def __init__(self):
        import spidev
        from gpiozero import OutputDevice
        self.lock, self.pending = threading.Lock(), []
        self.reset = OutputDevice(RESET_PIN, initial_value=True)
        self.reset.off(); time.sleep(0.01); self.reset.on(); time.sleep(0.01)   # reset pulse
        self.spi = spidev.SpiDev()
        self.spi.open(0, 0)
        self.spi.max_speed_hz, self.spi.mode = 5_000_000, 0
        version = self.read(REG_VERSION)
        if version != 0x12:
            raise RuntimeError(f"no SX1278 found (version register {version:#04x}, expected 0x12): "
                               "check the wiring and that SPI is enabled (raspi-config)")
        self.write(REG_OPMODE, 0x00)               # sleep in FSK mode: LoRa mode can only be set while asleep
        self.write(REG_OPMODE, SLEEP)
        frf = FREQ_HZ * 2 ** 19 // 32_000_000
        for i, shift in enumerate((16, 8, 0)):
            self.write(REG_FRF + i, (frf >> shift) & 0xFF)
        self.write(REG_TX_BASE, 0x00)
        self.write(REG_RX_BASE, 0x00)
        self.write(REG_LNA, self.read(REG_LNA) | 0x03)               # receiver boost
        self.write(REG_PA, 0x80 | (TX_DBM - 2))                       # PA_BOOST pin, the one SX1278 boards (e.g. Ra-02) use
        self.write(REG_MODEM1, BW_BITS | CR_BITS)                     # explicit header
        self.write(REG_MODEM2, SF << 4 | 0x04)                        # CRC on
        self.write(REG_MODEM3, 0x04 | (0x08 if SF >= 11 else 0))      # automatic gain; low data rate fix at SF11+
        self.write(REG_PREAMBLE, PREAMBLE >> 8)
        self.write(REG_PREAMBLE + 1, PREAMBLE & 0xFF)
        self.write(REG_SYNC, SYNC_WORD)
        self.write(REG_OPMODE, RX_CONTINUOUS)
        self.version = version

    def read(self, reg):
        return self.spi.xfer2([reg & 0x7F, 0])[1]

    def write(self, reg, value):
        self.spi.xfer2([reg | 0x80, value])

    def _wait(self, flag, timeout_s):
        end = time.time() + timeout_s
        while not self.read(REG_IRQ) & flag:
            if time.time() > end:
                self.write(REG_OPMODE, RX_CONTINUOUS)
                raise TimeoutError(f"radio did not finish (IRQ flag {flag:#04x})")
            time.sleep(0.001)

    def _take_rx(self):
        """Move a received packet out of the radio's buffer before anything overwrites it."""
        flags = self.read(REG_IRQ)
        if flags & IRQ_RX_DONE:
            if not flags & IRQ_CRC_ERROR:
                n = self.read(REG_RX_BYTES)
                self.write(REG_FIFO_PTR, self.read(REG_RX_CURRENT))
                self.pending.append(bytes(self.spi.xfer2([REG_FIFO] + [0] * n)[1:]))
            self.write(REG_IRQ, IRQ_RX_DONE | IRQ_CRC_ERROR)

    def send(self, data):
        with self.lock:
            self._take_rx()
            self.write(REG_OPMODE, STANDBY)
            self.write(REG_FIFO_PTR, 0x00)
            self.spi.xfer2([REG_FIFO | 0x80] + list(data))
            self.write(REG_PAYLOAD_LEN, len(data))
            self.write(REG_IRQ, 0xFF)
            self.write(REG_OPMODE, TX)
            self._wait(IRQ_TX_DONE, 2.0)
            self.write(REG_IRQ, 0xFF)
            self.write(REG_OPMODE, RX_CONTINUOUS)

    def cad(self):
        """Listen before talk: True if someone else is sending on our channel right now."""
        with self.lock:
            self._take_rx()
            self.write(REG_OPMODE, STANDBY)
            self.write(REG_IRQ, 0xFF)
            self.write(REG_OPMODE, CAD)
            self._wait(IRQ_CAD_DONE, 1.0)
            busy = bool(self.read(REG_IRQ) & IRQ_CAD_BUSY)
            self.write(REG_IRQ, 0xFF)
            self.write(REG_OPMODE, RX_CONTINUOUS)
            return busy

    def poll(self):
        with self.lock:
            self._take_rx()
            got, self.pending = self.pending, []
            return got


def seconds_until(now, at_s):
    """Seconds to wait from `now` until second `at_s` of a minute (this minute's, or the next one's
    if that second has already gone)."""
    return (at_s - now % 60) % 60


class PiHAL:
    """step_s=None: real time (UTC clock, India hours, wait for the time slot).
    step_s set: fast mode for demos: the clock is the CSV time (local, starts at midnight)."""

    def __init__(self, name, radio, csv_path, storage_path, keys, slot=None, step_s=None,
                 gpio=True, listen=True):
        self.name, self.radio, self.keys, self.slot = name, radio, keys, slot
        self.storage_path, self.step_s = storage_path, step_s
        self.utc_offset_s = 0 if step_s is not None else UTC_OFFSET_S
        self.clock_fix = 0.0          # gateway time minus our clock, from the last verified ACK
        self.lock, self.inbox = threading.Lock(), []
        self.sleep, self.log = time.sleep, print
        self.colour, self.leds, self.buzzer_dev = None, {}, None
        self.rows = csv.reader(open(csv_path))
        header = next(self.rows)
        prefix = name + ":"
        self.cols = [(i, c[len(prefix):]) for i, c in enumerate(header) if c.startswith(prefix)]
        if not self.cols:
            raise SystemExit(f"no columns for {name} in {csv_path}")
        self.t_csv, self.r = 0.0, {}
        if gpio:
            from gpiozero import LED, Buzzer
            self.leds = {c: LED(p) for c, p in LED_PINS.items()}
            self.buzzer_dev = Buzzer(BUZZER_PIN)
        if listen:
            threading.Thread(target=self._listen, daemon=True).start()

    # --- sensors: one CSV row per step ---
    def advance(self):
        """Load the next row. False at the end of the file."""
        row = next(self.rows, None)
        if row is None:
            return False
        self.t_csv, self.r = float(row[0]), {k: float(row[i]) for i, k in self.cols}
        return True

    def read(self):
        return self.r

    def now(self):
        return self.t_csv if self.step_s is not None else time.time() + self.clock_fix

    def key(self, name):
        return self.keys[name]

    # --- radio ---
    def send(self, raw, at_s=None, cad=False):
        if cad:                                        # alerts: listen first, back off, then send anyway
            for _ in range(CAD_TRIES):
                if not self.radio.cad():
                    break
                self.sleep(random.uniform(*CAD_BACKOFF_S))
        elif at_s is not None and self.step_s is None:
            # ponytail: blocks the step until our slot; fine at one step a minute
            self.sleep(seconds_until(self.now(), at_s))
        self.radio.send(raw)
        m = unpack(raw, self.key) or peek(raw)
        self.log(f"{self.now():.0f} {self.name} sent {m['k']} #{m['s']} {m.get('a', '')}")

    def _drain(self):
        got = self.radio.poll()
        if got:
            with self.lock:
                self.inbox += [(time.time(), p) for p in got]

    def _listen(self):
        while True:
            self._drain()
            time.sleep(RX_POLL_S)

    def receive(self):
        self._drain()
        with self.lock:
            got, self.inbox = self.inbox, []
        for arrived, raw in got:
            m = unpack(raw, lambda n: self.keys.get(n) if n in (self.name, "GATEWAY") else None)
            if m and m["k"] == "ACK" and self.step_s is None:
                self.clock_fix = m["gt"] - arrived      # trust the gateway time only from a signed ACK
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


def _wait_with_radio(node, hal, until_t):
    """Sleep until `until_t` while polling Node.radio_poll() for beacons, ACKs, and urgent retries."""
    while True:
        now = hal.now()
        if now >= until_t:
            break
        wake = node.radio_poll()
        dt = until_t - hal.now()
        if wake is not None:
            dt = min(dt, max(0.0, wake - hal.now()))
        hal.sleep(min(dt, RX_POLL_S) if dt > 0 else 0.0)


def run(node, hal, step_s=None):
    """One firmware step per CSV row: on each new minute (real time), or every step_s seconds."""
    if step_s is None:
        _wait_with_radio(node, hal, hal.now() + (60 - hal.now() % 60))
    while hal.advance():
        node.step()
        if step_s is None:
            _wait_with_radio(node, hal, hal.now() + (60 - hal.now() % 60))
        elif step_s:
            _wait_with_radio(node, hal, hal.now() + step_s)


def make_node(name, hal):
    return TankNode(name, hal) if name == "B-TANK" else FlatNode(name, hal)


def radio_test():
    r = SX1278()
    print(f"SX1278 found (version {r.version:#04x}), {FREQ_HZ / 1e6:.4f} MHz, SF{SF}")
    print("channel busy" if r.cad() else "channel free (CAD)")
    r.send(b"aqua-edge radio test")
    print("sent a test packet; listening 10 s for any packet...")
    end = time.time() + 10
    while time.time() < end:
        for p in r.poll():
            print("received", p.hex())
        time.sleep(RX_POLL_S)


def check():
    from hal.packet import pack, sim_key
    from edge.aqua_edge import DAY, HOUR

    class FakeRadio:
        def __init__(self):
            self.sent, self.air, self.busy_left, self.cads = [], [], 0, 0
        def send(self, data): self.sent.append(data)
        def poll(self): got, self.air = self.air, []; return got
        def cad(self):
            self.cads += 1
            if self.busy_left:
                self.busy_left -= 1
                return True
            return False

    def hal_for(name, step_s, storage=None):
        radio = FakeRadio()
        keys = {name: sim_key(name), "GATEWAY": sim_key("GATEWAY")}
        hal = PiHAL(name, radio, DEFAULT_CSV, storage, keys, slot=(5, 20),
                    step_s=step_s, gpio=False, listen=False)
        hal.log, slept = (lambda *a: None), []
        hal.sleep = slept.append
        return hal, radio, slept

    # 1. Only this node's columns, one row per step
    hal, radio, _ = hal_for("B-203", 0)
    assert hal.advance() and hal.now() == 0 and set(hal.read()) == {"flow_lps", "pressure_m"}
    assert hal.advance() and hal.now() == 60

    # 2. Whole planted scenario, fast mode: the leak in B-203 (day 2, 02:00) goes out on the radio
    hal, radio, _ = hal_for("B-203", 0)
    run(make_node("B-203", hal), hal, step_s=0)
    msgs = [unpack(p, hal.key) for p in radio.sent]
    alerts = sorted({(m["t"], m["a"]) for m in msgs if m["k"] == "ALERT"})
    assert alerts and alerts[0][1] == "LEAK_NIGHT_FLOW", alerts
    assert DAY + 2 * HOUR <= alerts[0][0] <= DAY + 3 * HOUR, alerts[0]
    assert radio.cads >= 1, "alerts listen before sending"

    # 3. A signed ACK deletes the message and sets the clock; a forged ACK does neither
    import tempfile
    hal, radio, _ = hal_for("B-104", None, os.path.join(tempfile.mkdtemp(), "B-104.jsonl"))
    node = make_node("B-104", hal)
    node.emit("SUM", i=0, vol_l=1.0, p_min=10.0)
    s = node.store.queue[0]["s"]
    ack = {"k": "ACK", "n": "B-104", "s": s, "gt": round(time.time()) + 3600}   # gateway an hour ahead
    radio.air = [pack(ack, b"attacker-guessed")]
    hal.advance(); node.step()
    assert hal.clock_fix == 0 and any(m["s"] == s for m in node.store.queue), "forged ACK ignored"
    radio.air = [pack(ack, sim_key("B-104"))]
    hal.advance(); node.step()
    assert 3590 < hal.clock_fix < 3610, hal.clock_fix
    assert all(m["s"] != s for m in node.store.queue), "real ACK deletes the message"

    # 4. Time slot wait
    assert abs(seconds_until(123, 12.2) - 9.2) < 1e-9          # second 3 -> wait for second 12.2
    assert abs(seconds_until(150, 12.2) - 42.2) < 1e-9         # second 30: slot gone, next minute's

    # 5. Busy channel: back off 5 times, then send anyway (an alert must not be stuck)
    hal, radio, slept = hal_for("B-203", 0)
    radio.busy_left = 99
    hal.advance()
    hal.send(pack({"k": "ALERT", "n": "B-203", "s": 1, "t": 0, "a": "LEAK_NIGHT_FLOW"}, sim_key("B-203")), cad=True)
    assert radio.cads == CAD_TRIES and len(slept) == CAD_TRIES and len(radio.sent) == 1
    print("pi_hal self-check passed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Run an Aqua Edge node on a Raspberry Pi with an SX1278 radio.")
    ap.add_argument("name", nargs="?", help="node name, e.g. B-203 or B-TANK")
    ap.add_argument("--csv", default=DEFAULT_CSV, help="sensor readings, one row per minute")
    ap.add_argument("--keys", help="key file (see load_keys in packet.py)")
    ap.add_argument("--sd", default=os.path.join(HERE, "sd"), help="folder for the queue and baseline")
    ap.add_argument("--slot", nargs=2, type=int, metavar=("INDEX", "COUNT"), help="time slot, e.g. 5 20")
    ap.add_argument("--step-s", type=float, help="fast mode: one step every this many seconds, CSV time")
    ap.add_argument("--radio-test", action="store_true", help="check the LoRa module and exit")
    ap.add_argument("--check", action="store_true", help="self-check without hardware")
    a = ap.parse_args()
    if a.check:
        check()
    elif a.radio_test:
        radio_test()
    else:
        if not a.name or not a.keys:
            ap.error("a node name and --keys are needed to run a node")
        os.makedirs(a.sd, exist_ok=True)
        keys = load_keys(a.keys)
        if a.name not in keys:
            ap.error(f"{a.name} has no key in {a.keys}")
        hal = PiHAL(a.name, SX1278(), a.csv, os.path.join(a.sd, f"{a.name}.jsonl"), keys,
                    slot=tuple(a.slot) if a.slot else None, step_s=a.step_s)
        run(make_node(a.name, hal), hal, a.step_s)
