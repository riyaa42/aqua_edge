"""Tests for the HAL's Python code, adding to the self-checks inside hal/: the two openers
(pi_hal, gate_hal), the hardware setup, the radio tests, the shared code in common.py, and a node
and a gateway talking through their openers. The ESP8266 sketch is not tested here. Packet encryption is also
tested in hal/packet.py's self-check.

No hardware and no extra packages: spidev, gpiozero and pyserial are replaced by the fakes below.
So the real libraries are not tested.

    uv run python -m tests.test_hal
"""
import contextlib
import functools
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import types
from unittest import mock

import hal
from hal import common, esp8266_passthrough as esp, gate_hal, lora433, lora_pi as pi, pi_hal
from hal.link import MessageLink
from hal.packet import load_keys, pack, peek, sim_key, unpack

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- fakes ---

class FakeChip:
    """The SX1278's registers, as far as the driver uses them."""

    def __init__(self):
        self.sent, self.on_air, self.reply, self.transmitted = [], None, None, False
        self.power_on()

    def power_on(self):
        self.regs, self.fifo = {pi.REG_VERSION: pi.CHIP_VERSION, pi.REG_SYNC: 0x12}, bytearray(256)

    def deliver(self, packet):
        self.fifo[:len(packet)] = packet
        self.regs.update({pi.REG_RX_CURRENT: 0, pi.REG_RX_BYTES: len(packet)})
        self.regs[pi.REG_IRQ] = self.regs.get(pi.REG_IRQ, 0) | pi.IRQ_RX_DONE

    def xfer2(self, data):
        reg, writing, ptr = data[0] & 0x7F, data[0] & 0x80, self.regs.get(pi.REG_FIFO_PTR, 0)
        if reg == pi.REG_FIFO:
            if writing:
                self.fifo[ptr:ptr + len(data) - 1] = bytes(data[1:])
                return [0] * len(data)
            return [0] + list(self.fifo[ptr:ptr + len(data) - 1])
        if not writing:
            return [0, self.regs.get(reg, 0)]
        value = data[1]
        if reg == pi.REG_IRQ:
            self.regs[reg] = self.regs.get(reg, 0) & ~value
            return [0, 0]
        self.regs[reg] = value
        if reg == pi.REG_OPMODE:
            if value == pi.TX:
                packet = bytes(self.fifo[:self.regs[pi.REG_PAYLOAD_LEN]])
                self.sent.append(packet)
                self.regs[pi.REG_IRQ] = self.regs.get(pi.REG_IRQ, 0) | pi.IRQ_TX_DONE
                self.transmitted = True
                if self.on_air:
                    self.on_air(packet)
            elif value == pi.CAD:
                self.regs[pi.REG_IRQ] = self.regs.get(pi.REG_IRQ, 0) | pi.IRQ_CAD_DONE
            elif value == pi.RX_CONTINUOUS and self.transmitted:
                self.transmitted = False
                if self.reply:
                    reply, self.reply = self.reply, None
                    self.deliver(reply)
        return [0, 0]


@contextlib.contextmanager
def fake_pi_libraries(chip):
    """Stand-ins for spidev and gpiozero. Yields what the driver did with them."""
    used = types.SimpleNamespace(spi=None, reset=None, dio0=None)

    class SpiDev:
        def __init__(self):
            used.spi, self.opened, self.closed = self, None, False
            self.max_speed_hz = self.mode = None

        def open(self, bus, device):
            self.opened = (bus, device)

        def xfer2(self, data):
            assert self.opened is not None and not self.closed, "SPI used while not open"
            return chip.xfer2(data)

        def close(self):
            self.closed = True

    class OutputDevice:
        def __init__(self, pin, initial_value=False):
            used.reset, self.pin, self.initial_value, self.levels = self, pin, initial_value, []

        def on(self):
            self.levels.append(1)
            chip.power_on()

        def off(self):
            self.levels.append(0)

    class DigitalInputDevice:
        def __init__(self, pin, pull_up=True):
            used.dio0, self.pin, self.pull_up, self.when_activated = self, pin, pull_up, None

    with mock.patch.dict(sys.modules, {
            "spidev": types.SimpleNamespace(SpiDev=SpiDev),
            "gpiozero": types.SimpleNamespace(OutputDevice=OutputDevice, DigitalInputDevice=DigitalInputDevice)}):
        yield used


class FakeBoard:
    """An ESP8266 running the sketch, seen from the serial port."""

    def __init__(self):
        self.out, self.packets, self.lock = bytearray(), [], threading.Lock()
        self.unplugged, self.settings, self.result = False, esp.SETTINGS, esp.SENT_OK
        self.on_air = self.reply = None

    def deliver(self, packet):
        with self.lock:
            self.out += esp.frame(esp.RX, 0, b"\xff\xb0\x14" + packet)     # -80 dBm, 5 dB

    def receive_from_host(self, data):
        kind, seq, body = esp.unframe(data[:-1])
        if kind == esp.HELLO:
            info = esp.struct.pack(esp.INFO_FORMAT, esp.PROTOCOL, 1, 0x12, *self.settings)
            with self.lock:
                self.out += esp.frame(esp.INFO, seq, info)
            return
        self.packets.append(body[1:])
        with self.lock:
            self.out += esp.frame(esp.SENT, seq, bytes((self.result,)))
        if self.on_air:
            self.on_air(body[1:])
        if self.reply:
            reply, self.reply = self.reply, None
            self.deliver(reply)


@contextlib.contextmanager
def fake_pyserial(boards):
    """A stand-in for the pyserial module. boards: {path: FakeBoard}. Yields the ports opened."""
    ports = []

    class Serial:
        def __init__(self):
            self.port, self.baudrate, self.timeout, self.write_timeout = None, 9600, None, None
            self.dtr = self.rts = True
            self.exclusive, self.is_open = None, False

        def open(self):
            if self.port not in boards or boards[self.port].unplugged:
                raise OSError(f"could not open port {self.port}")
            self.at_open = (self.baudrate, self.timeout, self.write_timeout, self.dtr, self.rts, self.exclusive)
            self.board, self.is_open = boards[self.port], True
            ports.append(self)

        def _usable(self):
            assert self.is_open, "port used while closed"
            if self.board.unplugged:
                raise OSError("device disconnected")

        @property
        def in_waiting(self):
            self._usable()
            return len(self.board.out)

        def read(self, n):
            self._usable()
            with self.board.lock:
                got, self.board.out = bytes(self.board.out[:n]), self.board.out[n:]
            return got

        def write(self, data):
            self._usable()
            self.board.receive_from_host(bytes(data))

        def close(self):
            self.is_open = False

    with mock.patch.dict(sys.modules, {"serial": types.SimpleNamespace(Serial=Serial)}):
        yield ports


def wait_for(test, seconds=5):
    end = time.monotonic() + seconds
    while not test() and time.monotonic() < end:
        time.sleep(0.01)
    return test()


def collect(radio, count, seconds=5):
    got = []
    wait_for(lambda: got.extend(radio.receive()) or len(got) >= count, seconds)
    return got


def raises(error, call, words=""):
    try:
        call()
    except error as e:
        assert words in str(e), (words, str(e))
        return
    raise AssertionError(f"no {error.__name__}")


def output_of(call):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        call()
    return out.getvalue()


# --- shared code ---

def test_shared_settings():
    # the chip's register bits must mean what the time-on-air calculation assumes
    assert common.BW_BITS >> 4 == {125_000: 7, 250_000: 8, 500_000: 9}[lora433.BW_HZ]
    assert common.BW_BITS & 0x0F == 0 and common.CR_BITS == lora433.CODING_RATE << 1
    assert common.MAX_PAYLOAD == 255
    for good in (b"x", bytes(255)):
        common.check_payload(good)
    for bad in (b"", bytes(256)):
        raises(ValueError, lambda: common.check_payload(bad), "1 to 255")


def test_listen_and_print():
    batches = [[b"\x01\x02"], [], [b"\xff"]]
    t0 = time.monotonic()
    text = output_of(lambda: common.listen_and_print(lambda: batches.pop(0) if batches else [], lambda: " <note>", 0.2))
    assert 0.2 <= time.monotonic() - t0 < 2
    assert "listening 0.2 s" in text and "received 0102 <note>\n" in text and "received ff <note>\n" in text


def test_radio_link_base():
    now, log, order = [100.0], [], []

    class Link(common.RadioLink):
        fail_restore = True
        services = 0

        def service(self):
            self.services += 1
            self.collect([b"from the thread"] if self.services == 1 else [])

        def _restore(self):
            order.append(("restore", now[0]))
            now[0] += 8                                     # a slow attempt
            if self.fail_restore:
                raise OSError("not yet")

        def _shut(self):
            order.append(("shut", self.thread.is_alive()))

    link = Link(log.append, lambda: now[0])
    before = time.time()
    link.collect([b"one", b"two"])
    link.collect([])
    got = link.drain()
    assert [p for _, p in got] == [b"one", b"two"] and all(before <= t <= time.time() for t, _ in got)
    assert link.drain() == [] and link._recover() is True and not order        # nothing is broken
    link._mark_broken("boom")
    assert link.broken and log[-1] == "radio error: boom"
    assert link._recover() is False and len(order) == 1 and "not yet" in log[-1]
    now[0] += common.RECOVER_S - 0.1                        # counted from the end of the slow attempt
    assert link._recover() is False and len(order) == 1
    now[0] += 0.2
    link.fail_restore = False
    assert link._recover() is True and not link.broken and log[-1] == "radio set up again"
    link.start_listening()                                  # uses the default pause between reads
    assert wait_for(lambda: link.services >= 3) and [p for _, p in link.receive()] == [b"from the thread"]
    link.close()
    assert not link.thread.is_alive() and order[-1] == ("shut", False), "stopped before the radio is released"


# --- the Pi ---

def test_pi_hardware_setup_and_opener():
    chip, log = FakeChip(), []
    with fake_pi_libraries(chip) as used:
        radio = pi_hal.open_radio(log=log.append)
        assert used.spi.opened == (0, 0) and (used.spi.max_speed_hz, used.spi.mode) == (5_000_000, 0)
        assert (used.reset.pin, used.reset.initial_value, used.reset.levels) == (22, True, [0, 1])
        assert (used.dio0.pin, used.dio0.pull_up) == (25, False) and callable(used.dio0.when_activated)
        assert radio.thread.is_alive() and radio.radio.healthy()
        assert radio.send(b"up", cad=True) is True and chip.sent == [b"up"]
        chip.deliver(b"down")
        assert [p for _, p in collect(radio, 1)] == [b"down"]
        radio.close()
        assert not radio.thread.is_alive() and used.spi.closed and chip.regs[pi.REG_OPMODE] == pi.SLEEP
    assert not log, log


def test_pi_opener_without_a_radio():
    chip = FakeChip()
    chip.power_on = lambda: None
    chip.regs = {pi.REG_VERSION: 0x00}
    with fake_pi_libraries(chip):
        raises(OSError, pi_hal.open_radio, "no SX1278 found")


def test_pi_close_survives_a_dead_radio():
    chip, log = FakeChip(), []
    with fake_pi_libraries(chip) as used:
        radio = pi_hal.open_radio(log=log.append)
        radio.stopped.set()
        radio.thread.join(2)

        def dead(data):
            raise OSError("SPI gone")
        used.spi.xfer2 = dead
        radio.close()
    assert any("while closing" in line for line in log), log


def test_dio0_wakes_the_receive_thread():
    chip = FakeChip()
    with fake_pi_libraries(chip) as used, mock.patch.object(pi, "RX_POLL_S", 1.5):   # polling made slow
        radio = pi_hal.open_radio(log=lambda m: None)
        time.sleep(0.2)                                     # the thread is now waiting
        chip.deliver(b"urgent")
        time.sleep(0.2)
        with radio.lock:
            assert not radio.inbox, "nothing should read the radio before the next poll"
        used.dio0.when_activated()
        t0 = time.monotonic()
        assert wait_for(lambda: radio.inbox, 1) and time.monotonic() - t0 < 0.5
        radio.stopped.set()
        used.dio0.when_activated()
        radio.close()
        assert not radio.thread.is_alive()


def test_pi_radio_test():
    chip = FakeChip()
    chip.reply = b"\xab\xcd"
    with fake_pi_libraries(chip) as used, \
            mock.patch.object(pi, "listen_and_print", functools.partial(common.listen_and_print, seconds=0.2)):
        text = output_of(pi.radio_test)
    assert "SX1278 found (version 0x12), 433.1750 MHz, SF7" in text and "channel free" in text
    assert "received abcd\n" in text and chip.sent == [common.TEST_PACKET] and used.spi.closed


# --- the gateway ---

def test_serial_port_settings():
    board = FakeBoard()
    with fake_pyserial({"/dev/fake": board}) as ports:
        port = esp.SerialPort("/dev/fake")
        # DTR and RTS are cleared before opening, reads never wait, nobody else may open the port
        assert ports[0].at_open == (esp.BAUD, 0, 2, False, False, True), ports[0].at_open
        assert port.read() == b""
        board.deliver(b"abc")
        assert esp.unframe(port.read()[:-1])[2].endswith(b"abc")
        port.write(esp.frame(esp.HELLO, 3))
        assert esp.unframe(port.read()[:-1])[:2] == (esp.INFO, 3)
        port.close()
        assert not ports[0].is_open
        raises(OSError, lambda: esp.SerialPort("/dev/absent"), "could not open")


def test_serial_port_without_pyserial():
    with mock.patch.dict(sys.modules, {"serial": None}):
        raises(esp.ModemError, lambda: esp.SerialPort("/dev/fake"), "uv sync --extra gateway")


def test_gateway_opener():
    board, log = FakeBoard(), []
    with fake_pyserial({"/dev/ttyUSB0": board}) as ports:
        radio = gate_hal.open_radio(log=log.append)         # the default port
        assert ports[-1].port == esp.DEFAULT_PORT == "/dev/ttyUSB0" and radio.thread.is_alive()
        assert radio.send(b"down") is True and board.packets == [b"down"]
        board.deliver(b"up")
        got = collect(radio, 1)
        assert [p for _, p in got] == [b"up"] and abs(got[0][0] - time.time()) < 5 and radio.signal == (-80, 5.0)
        radio.close()
        assert not radio.thread.is_alive() and not ports[-1].is_open
    assert not log, log


def test_gateway_opener_refusals():
    board = FakeBoard()
    board.settings = (esp.SETTINGS[0] + 1,) + esp.SETTINGS[1:]
    with fake_pyserial({"/dev/fake": board}) as ports:
        raises(OSError, lambda: gate_hal.open_radio("/dev/fake"), "radio settings differ")
        assert not ports[-1].is_open, "the port is released after refusing"
        raises(OSError, lambda: gate_hal.open_radio("/dev/absent"), "could not open")


def test_gateway_unplug_and_reconnect():
    board, log = FakeBoard(), []
    with fake_pyserial({"/dev/fake": board}) as ports, \
            mock.patch.object(common, "RECOVER_S", 0.2), mock.patch.object(esp, "RECOVER_S", 0.2):
        radio = gate_hal.open_radio("/dev/fake", log=log.append)
        board.unplugged = True
        assert radio.send(b"lost") is False and wait_for(lambda: radio.broken) and radio.receive() == []
        assert wait_for(lambda: any("reconnect failed" in line for line in log))
        board.unplugged = False
        assert wait_for(lambda: not radio.broken), log
        assert radio.send(b"again") is True and len(ports) == 2 and not ports[0].is_open
        radio.close()


def test_gateway_radio_test():
    board = FakeBoard()
    board.reply = b"\xca\xfe"
    short = functools.partial(common.listen_and_print, seconds=0.2)
    with fake_pyserial({"/dev/fake": board}) as ports, mock.patch.object(esp, "listen_and_print", short):
        text = output_of(lambda: esp.radio_test("/dev/fake"))
        assert "modem and SX1278 found (version 0x12), 433.1750 MHz, SF7" in text
        assert "received cafe  (RSSI -80 dBm, SNR 5.0 dB)" in text
        assert board.packets == [common.TEST_PACKET] and not ports[-1].is_open
        raises(SystemExit, lambda: esp.radio_test("/dev/absent"), "/dev/absent: could not open")
        board.result = esp.SENT_RADIO_FAULT
        raises(SystemExit, lambda: output_of(lambda: esp.radio_test("/dev/fake")), "was not sent")


# --- both ends ---

class BytesOnly:
    """What a board or gateway server does before MessageLink: drop the arrival times."""

    def __init__(self, radio):
        self.radio, self.send = radio, radio.send

    def receive(self):
        return [packet for _, packet in self.radio.receive()]


def test_node_and_gateway_through_their_openers():
    chip, board = FakeChip(), FakeBoard()
    chip.on_air, board.on_air = board.deliver, chip.deliver        # the air between them
    with fake_pi_libraries(chip), fake_pyserial({"/dev/fake": board}):
        node_radio = pi_hal.open_radio(log=lambda m: None)
        gate_radio = gate_hal.open_radio("/dev/fake", log=lambda m: None)
        keys = {n: sim_key(n) for n in ("B-203", "GATEWAY")}
        node = MessageLink(BytesOnly(node_radio), keys.__getitem__)
        gateway = MessageLink(BytesOnly(gate_radio), sim_key)

        def arrived(link):
            got = []
            wait_for(lambda: got.extend(link.receive()[0]) or got)
            return got

        alert = {"k": "ALERT", "n": "B-203", "s": 4, "t": 7200, "a": "LEAK_NIGHT_FLOW", "flow": 0.05}
        assert node.send(alert) and arrived(gateway) == [alert]
        ack = {"k": "ACK", "n": "B-203", "s": 4, "gt": 7201}
        assert gateway.send(ack) and arrived(node) == [ack]
        node_radio.close()
        gate_radio.close()


def test_readme_gateway_example():
    with open(os.path.join(ROOT, "hal", "README.md"), encoding="utf-8") as f:
        code = re.search(r"```python\n(.*?)```", f.read(), re.S).group(1)
    board = FakeBoard()
    with tempfile.TemporaryDirectory() as folder, fake_pyserial({"/dev/ttyUSB0": board}) as ports:
        path = os.path.join(folder, "keys.json")
        with open(path, "w") as f:
            json.dump({"GATEWAY": sim_key("GATEWAY").hex()}, f)
        scope = {}
        exec(code.replace('"/path/outside/the/repository/keys.json"', repr(path)), scope)
        assert load_keys(path) == {"GATEWAY": sim_key("GATEWAY")}
    assert board.packets == [pack({"k": "BEACON", "n": "GATEWAY", "s": 1, "gt": 0}, sim_key("GATEWAY"))]
    assert scope["messages"] == [] and scope["rejected"] == 0 and not ports[-1].is_open


# --- encryption ---

def test_packets_on_the_air_are_encrypted():
    chip, board = FakeChip(), FakeBoard()
    chip.on_air, board.on_air = board.deliver, chip.deliver
    with fake_pi_libraries(chip), fake_pyserial({"/dev/fake": board}):
        node_radio = pi_hal.open_radio(log=lambda m: None)
        gate_radio = gate_hal.open_radio("/dev/fake", log=lambda m: None)
        key, other = os.urandom(16), os.urandom(16)
        node = MessageLink(BytesOnly(node_radio), {"B-203": key}.__getitem__)
        gateway = MessageLink(BytesOnly(gate_radio), {"B-203": key}.__getitem__)
        summary = {"k": "SUM", "n": "B-203", "s": 11, "t": 900, "i": 1, "vol_l": 123.456}
        command = {"k": "CMD", "n": "B-203", "s": 12, "t": 950, "a": "PIPE_LEAK", "over": 0}
        got = []
        assert node.send(summary) and wait_for(lambda: got.extend(gateway.receive()[0]) or got)
        assert gateway.send(command) and wait_for(lambda: got.extend(node.receive()[0]) or len(got) > 1)
        assert got == [summary, command]
        node_radio.close()
        gate_radio.close()
    up, down = chip.sent[0], board.packets[0]
    assert esp.struct.pack(">f", 123.456) not in up                      # what both radios carried
    for raw, message in ((up, summary), (down, command)):
        assert peek(raw) == {k: message[k] for k in ("k", "n", "s", "t")}    # the header is readable
        assert unpack(raw, lambda n: other) is None                          # the rest needs the key


def test_changed_packets_are_refused_by_the_receiving_end():
    class Air:
        box = []

        def receive(self):
            got, self.box = self.box, []
            return got

    key = os.urandom(16)
    air, alert = Air(), {"k": "ALERT", "n": "B-203", "s": 5, "t": 60, "a": "LEAK_NIGHT_FLOW"}
    link, raw = MessageLink(air, {"B-203": key}.__getitem__), pack(alert, key)
    # new header (another time, counter or kind) on the same encrypted part, and the reverse
    later = pack({**alert, "s": 6, "t": 120}, key)
    air.box = [later[:10] + raw[10:], raw[:10] + later[10:], bytes([2]) + raw[1:], raw[:-1], raw]
    assert link.receive() == ([alert], 4)


# --- the rest ---

def test_package_exports_and_packet_errors():
    for name in ("MessageLink", "pack", "unpack", "peek", "load_keys", "sim_key", "violation_mask",
                 "airtime_s", "retry_wait", "AirtimeBudget"):
        assert callable(getattr(hal, name)), name
    raises(ValueError, lambda: pack({"k": "ALERT", "n": "B-203", "s": 1, "t": 0, "colour": 3}, b"k"),
           "no packet field for ['colour']")


def test_command_lines():
    def run(*args):
        return subprocess.run([sys.executable, "-m", *args], cwd=ROOT, capture_output=True, text=True, timeout=60)

    for module in ("hal.lora_pi", "hal.esp8266_passthrough"):
        for args in ((), ("--check", "--radio-test"), ("--nonsense",)):
            done = run(module, *args)
            assert done.returncode == 2 and "usage:" in done.stderr, (module, args, done.stderr)
        assert "--radio-test" in run(module, "--help").stdout
    assert "--port" in run("hal.esp8266_passthrough", "--help").stdout


def main():
    tests = [f for name, f in globals().items() if name.startswith("test_") and callable(f)]
    for test in tests:
        test()
        print(f"ok: {test.__name__[5:].replace('_', ' ')}")
    print(f"HAL TESTS PASSED ({len(tests)})")


if __name__ == "__main__":
    main()
