"""The gateway's SX1278 433 MHz LoRa radio: a NodeMCU ESP8266 on USB serial running the sketch
esp8266_lora/esp8266_lora.ino. The ESP drives the radio (listen before talk, receiving, recovering
a faulty radio); this computer only exchanges whole packets with it. The counterpart of lora_pi.py:
same send() and receive() as the Pi's LoRaLink.

    ESPLink      the link: send() and receive() that never let a modem fault stop the program,
                 a receive thread, and reconnecting after an unplug or a fault
    SerialPort   the USB serial port (needs pyserial: uv sync --extra gateway)

This file only carries bytes. Signing, addressing, acknowledgements and retries are in packet.py
and gateway/core.py. The serial protocol is described at the top of the sketch. Wiring, flashing
and setup: hal/README.md, "Gateway modem (ESP8266)".

    python3 -m hal.esp8266_passthrough --radio-test --port /dev/ttyUSB0   # is it working?
    python3 -m hal.esp8266_passthrough --check                            # no hardware needed
"""
import argparse
import binascii
import struct
import threading
import time

from hal.common import (BW_BITS, CR_BITS, MAX_PAYLOAD, RECOVER_S, SYNC_WORD, TEST_PACKET, RadioLink,
                        check_payload, listen_and_print)
from hal.lora433 import FREQ_HZ, PREAMBLE, SF, TX_DBM

# Serial protocol: keep in step with the sketch.
PROTOCOL, BAUD = 1, 115200
HELLO, SEND = 0x01, 0x02                       # to the ESP
INFO, SENT, RX, LOG = 0x81, 0x82, 0x83, 0x84   # from the ESP
SENT_OK, SENT_RADIO_FAULT, SENT_BAD_LENGTH = 0, 1, 2
INFO_FORMAT = ">BBBIBBBHBB"     # protocol, radio ok, chip, frequency, SF, bandwidth, coding rate, preamble, sync, dBm
SETTING_NAMES = ("frequency", "spreading factor", "bandwidth bits", "coding rate bits", "preamble",
                 "sync word", "TX power")
SETTINGS = (FREQ_HZ, SF, BW_BITS, CR_BITS, PREAMBLE, SYNC_WORD, TX_DBM)   # what the sketch must report
MAX_FRAME = 270                 # longest valid frame, with room to spare

DEFAULT_PORT = "/dev/ttyUSB0"
CONNECT_S = 8                   # opening the port can restart a NodeMCU: wait this long for its answer
HELLO_EVERY_S = 0.5
# Longest the ESP can take: waiting for incoming packets, 5 busy channel checks with up to 1 s
# backoff each, then the 2 s send timeout.
SEND_TIMEOUT_S = 12
POLL_S = 0.005


class ModemError(OSError):
    """The modem is missing, not answering, or running the wrong firmware."""


def cobs_encode(data):
    out = bytearray()
    blocks = data.split(b"\0")
    for i, block in enumerate(blocks):
        while len(block) >= 254:
            out.append(0xFF)
            out += block[:254]
            block = block[254:]
            if not block and i == len(blocks) - 1:
                return bytes(out)
        out.append(len(block) + 1)
        out += block
    return bytes(out)


def cobs_decode(data):
    """None if it is broken."""
    out, i = bytearray(), 0
    while i < len(data):
        code = data[i]
        if code == 0 or i + code > len(data):
            return None
        out += data[i + 1:i + code]
        i += code
        if code != 0xFF and i < len(data):
            out.append(0)
    return bytes(out)


def frame(kind, seq, data=b""):
    body = bytes((kind, seq)) + data
    return cobs_encode(body + struct.pack(">H", binascii.crc_hqx(body, 0xFFFF))) + b"\0"


def unframe(chunk):
    """(type, sequence, data) from one frame without its 0x00, or None: noise or damaged."""
    raw = cobs_decode(chunk)
    if raw is None or len(raw) < 4 or binascii.crc_hqx(raw[:-2], 0xFFFF) != struct.unpack(">H", raw[-2:])[0]:
        return None
    return raw[0], raw[1], raw[2:-2]


class SerialPort:
    """The ESP's USB serial port. read() returns what has arrived, without waiting."""

    def __init__(self, path):
        try:
            import serial
        except ImportError:
            raise ModemError("pyserial is not installed: uv sync --extra gateway") from None
        s = serial.Serial()
        s.port, s.baudrate, s.timeout, s.write_timeout = path, BAUD, 0, 2
        s.dtr = s.rts = False               # set before opening, so the board is not held in reset
        s.exclusive = True                  # two programs reading one port would each lose packets
        s.open()
        self.serial = s

    def read(self):
        return self.serial.read(self.serial.in_waiting or 1)

    def write(self, data):
        self.serial.write(data)

    def close(self):
        self.serial.close()


class ESPLink(RadioLink):
    """The link to the nodes through the ESP, as raw bytes. After connecting, a modem fault is logged
    and the port reopened; it never reaches the caller. A failed send loses nothing: the gateway
    repeats a command until it is confirmed, and a node resends until it is acknowledged.

    open_port: returns an open port with read(), write(bytes) and close(); called again to reconnect.
    Raises OSError if no working modem answers at the start."""

    NAME = "modem"
    RESTORE_FAILED, RESTORED = "modem reconnect failed", "modem connected again"
    JOIN_S = CONNECT_S + 2          # the thread may be in the middle of reconnecting

    def __init__(self, open_port, log=print, listen=True, clock=time.monotonic, sleep=time.sleep):
        super().__init__(log, clock)
        self.open_port, self.sleep = open_port, sleep
        self.io = threading.RLock()             # one user of the port at a time
        self.port, self.buffer, self.seq = None, bytearray(), 0
        self.info = self.reply = None           # last (sequence, data) of an INFO and of a SENT
        self.restarted = False
        self.signal = None                      # (RSSI dBm, SNR dB) of the last packet received
        self._inflight = None                   # (seq, deadline) for non-blocking start_send/poll_send
        self._connect()
        if listen:
            self.start_listening()

    # --- the interface, as the Pi's LoRaLink ---
    def send(self, raw, cad=True):
        """Send one packet now. cad: the ESP listens first, backs off while the channel is busy, then
        sends anyway. False if it was not confirmed as sent. Waits for the ESP's answer."""
        check_payload(raw)
        if self.broken and self.thread:
            return False                        # the receive thread is reconnecting: do not wait for it
        with self.io:
            if self.broken and not self._recover():
                return False
            self._check_restart()               # one noticed earlier: check it before sending
            if self.broken:
                return False
            try:
                seq = self._next_seq()
                self.reply = None
                self.port.write(frame(SEND, seq, bytes((bool(cad),)) + bytes(raw)))
                end = self.clock() + SEND_TIMEOUT_S
                while not (self.reply and self.reply[0] == seq):
                    if self.restarted:
                        return False            # it restarted (already logged): this packet is gone
                    if self.clock() > end:
                        raise ModemError("the modem did not answer a send")
                    self._pump()
                    self.sleep(POLL_S)
            except OSError as e:
                self._failed(e)
                return False
            result = self.reply[1][:1]
            if result != bytes((SENT_OK,)):     # the ESP recovers its own radio: the port is fine
                self.log("modem could not send: " + ("radio fault" if result == bytes((SENT_RADIO_FAULT,))
                                                     else "bad packet length"))
                return False
            return True

    def start_send(self, raw, cad=True):
        """Start sending one packet without blocking for the ESP's CAD/TX reply.
        Returns True if handed to the serial port (poll_send() will report True/False),
        False if the link is broken, busy, or writing failed."""
        check_payload(raw)
        if self._inflight is not None:
            return False
        if self.broken and self.thread:
            return False
        if not self.io.acquire(blocking=False):
            return False
        try:
            if self.broken and not self._recover():
                return False
            self._check_restart()
            if self.broken:
                return False
            try:
                seq = self._next_seq()
                self.reply = None
                self.port.write(frame(SEND, seq, bytes((bool(cad),)) + bytes(raw)))
                self._inflight = (seq, self.clock() + SEND_TIMEOUT_S)
                return True
            except OSError as e:
                self._failed(e)
                return False
        finally:
            self.io.release()

    def poll_send(self):
        """Check an in-flight start_send(). Returns None while waiting, True on SENT_OK, False on failure."""
        if self._inflight is None:
            return False
        seq, deadline = self._inflight
        if not self.thread:
            self.service()
        if self.restarted:
            self._inflight = None
            return False
        if self.reply and self.reply[0] == seq:
            self._inflight = None
            result = self.reply[1][:1]
            if result != bytes((SENT_OK,)):
                self.log("modem could not send: " + ("radio fault" if result == bytes((SENT_RADIO_FAULT,))
                                                     else "bad packet length"))
                return False
            return True
        if self.clock() > deadline:
            self._inflight = None
            with self.io:
                self._failed(ModemError("the modem did not answer a send"))
            return False
        return None

    def receive(self):
        """[(arrival time in time.time() seconds, packet bytes)] received since the last call.
        Never waits when the receive thread runs."""
        if not self.thread:
            self.service()
        return self.drain()

    def service(self):
        """Read whatever the ESP has sent, and reconnect if needed."""
        with self.io:
            if self.broken and not self._recover():
                return
            try:
                self._pump()
            except OSError as e:
                self._failed(e)
                return
            self._check_restart()

    # --- inside ---
    def _next_seq(self):
        self.seq = self.seq % 255 + 1           # 1..255: 0 is the INFO sent at boot
        return self.seq

    def _pump(self):
        self.buffer += self.port.read()
        while True:
            end = self.buffer.find(0)
            if end < 0:
                break
            chunk, self.buffer = bytes(self.buffer[:end]), self.buffer[end + 1:]
            message = unframe(chunk)
            if message:
                self._dispatch(*message)
        if len(self.buffer) > MAX_FRAME:        # noise with no end: drop it
            self.buffer.clear()

    def _dispatch(self, kind, seq, data):
        if kind == RX and len(data) > 3:
            rssi, snr = struct.unpack(">hb", data[:3])
            self.signal = (rssi, snr / 4)
            self.collect([data[3:]])
        elif kind == SENT:
            self.reply = (seq, data)
        elif kind == INFO:
            self.info = (seq, data)
            if seq == 0:
                self.restarted = True
        elif kind == LOG:
            self.log("modem: " + data.decode(errors="replace"))

    def _check_info(self, data):
        if data[:1] != bytes((PROTOCOL,)) or len(data) != struct.calcsize(INFO_FORMAT):
            raise ModemError(f"the ESP runs another version of the sketch: flash esp8266_lora.ino (protocol {PROTOCOL})")
        _, radio_ok, chip, *settings = struct.unpack(INFO_FORMAT, data)
        wrong = [f"{n} is {got}, expected {want}" for n, got, want in zip(SETTING_NAMES, settings, SETTINGS)
                 if got != want]
        if wrong:
            raise ModemError("the ESP's radio settings differ (flash the current sketch): " + "; ".join(wrong))
        if not radio_ok:
            raise ModemError(f"the ESP answers but its SX1278 does not (version register {chip:#04x}): "
                             "check the wiring between them")
        return chip

    def _connect(self):
        """Open the port and wait until the ESP answers with the expected firmware and a working radio."""
        self._close_port()
        self.port = self.open_port()
        try:
            self.buffer.clear()
            self.info, seq = None, None
            end, next_hello = self.clock() + CONNECT_S, self.clock()
            while not (self.info and self.info[0] == seq):
                if self.clock() > end:
                    raise ModemError("no answer from the ESP: is esp8266_lora.ino flashed, and is this its port?")
                if self.clock() >= next_hello:
                    seq, next_hello = self._next_seq(), self.clock() + HELLO_EVERY_S
                    self.port.write(frame(HELLO, seq))
                self._pump()
                self.sleep(POLL_S)
            self.version = self._check_info(self.info[1])
            self.restarted = False
        except OSError:
            self._close_port()
            raise

    def _close_port(self):
        port, self.port = self.port, None
        if port:
            try:
                port.close()
            except OSError as e:
                self.log(f"modem error while closing: {e}")

    def _failed(self, error):
        self._mark_broken(error)
        self.next_recover = self.clock() + RECOVER_S      # not at once: an unplugged board needs time
        self._close_port()

    def _restore(self):
        self._connect()

    def _shut(self):
        with self.io:
            self._close_port()

    def _check_restart(self):
        """An unasked-for INFO means the ESP restarted: check it again."""
        if self.restarted:
            self.restarted = False
            self.log("modem restarted")
            try:
                self._check_info(self.info[1])
            except OSError as e:
                self._failed(e)

    def _idle(self):
        self.stopped.wait(POLL_S)


def radio_test(port):
    """Pairs with the Pi's --radio-test: start one, then the other within 10 s. Only the one started
    first prints the other's packet; swap the order to test the other way."""
    try:
        link = ESPLink(lambda: SerialPort(port), listen=False)
    except OSError as e:
        raise SystemExit(f"{port}: {e}")
    print(f"modem and SX1278 found (version {link.version:#04x}), {FREQ_HZ / 1e6:.4f} MHz, SF{SF}")
    if not link.send(TEST_PACKET):
        raise SystemExit("the test packet was not sent")
    # the signal figures are those of the newest packet
    listen_and_print(lambda: [p for _, p in link.receive()],
                     lambda: f"  (RSSI {link.signal[0]} dBm, SNR {link.signal[1]} dB)")
    link.close()


def check():
    class FakeESP:
        """Stand-in for the serial port with the sketch behind it."""

        def __init__(self):
            self.t, self.out, self.packets, self.opens = 0.0, bytearray(), [], 0
            self.radio_ok, self.result, self.mute, self.dead = True, SENT_OK, False, False
            self.restart_on_send, self.protocol, self.settings, self.noise = False, PROTOCOL, SETTINGS, True

        def clock(self):
            return self.t

        def sleep(self, s):
            self.t += s

        def open(self):
            if self.dead:
                raise OSError("no such port")
            self.opens += 1
            self.out = bytearray()
            if self.noise:                                  # boot loader garbage, then the boot INFO
                self.out += b"\x8e\x13 ets Jan  8 2013\0" + self.info(0)
            return self

        def info(self, seq):
            data = struct.pack(INFO_FORMAT, self.protocol, self.radio_ok, 0x12, *self.settings)
            return frame(INFO, seq, data)

        def deliver(self, packet, rssi=-71, snr=9.25):
            self.out += frame(RX, 0, struct.pack(">hb", rssi, int(snr * 4)) + packet)

        def read(self):
            if self.dead:
                raise OSError("device disconnected")
            got, self.out = bytes(self.out[:7]), self.out[7:]    # in pieces, as a serial port does
            return got

        def write(self, data):
            if self.dead:
                raise OSError("device disconnected")
            assert data.endswith(b"\0") and data.count(0) == 1
            kind, seq, body = unframe(data[:-1])
            if self.mute:
                return
            if kind == HELLO:
                self.out += self.info(seq)
            elif self.restart_on_send:
                self.out += self.info(0)
            else:
                self.packets.append((body[0], body[1:]))
                self.out += frame(SENT, seq, bytes((self.result,)))

        def close(self):
            pass

    log = []

    def link_on(esp):
        return ESPLink(esp.open, log=log.append, listen=False, clock=esp.clock, sleep=esp.sleep)

    def drain(link, n=200):
        return [p for _ in range(n) for _, p in link.receive()]

    # 1. Framing: known CRC, every length, zeros, the 254-byte block edge; damage and noise dropped
    assert binascii.crc_hqx(b"123456789", 0xFFFF) == 0x29B1
    assert cobs_encode(b"\x11\x22\x00\x33") == b"\x03\x11\x22\x02\x33"
    for n in range(0, 600):
        for data in (bytes(n), bytes([7]) * n, bytes(i % 256 for i in range(n))):
            coded = cobs_encode(data)
            assert 0 not in coded and cobs_decode(coded) == data, n
    good = frame(RX, 0, bytes(range(40)))[:-1]
    assert unframe(good) == (RX, 0, bytes(range(40)))
    for i in range(len(good)):
        assert unframe(good[:i] + bytes([good[i] ^ 0x01 or 0x03]) + good[i + 1:]) is None, i
    assert unframe(good[:-3]) is None and unframe(b"") is None and unframe(b"ets Jan 8") is None

    # 2. Connecting through boot noise; sending; receiving whole packets, in order
    esp = FakeESP()
    link = link_on(esp)
    assert link.version == 0x12 and link.send(b"ack\0\0", cad=True) and link.send(b"x" * MAX_PAYLOAD, cad=False)
    assert esp.packets == [(1, b"ack\0\0"), (0, b"x" * MAX_PAYLOAD)]
    esp.deliver(b"alert\0one")
    esp.out += b"line noise\0" + b"\x55" * 400 + b"\0"
    esp.deliver(b"y" * MAX_PAYLOAD, rssi=-120, snr=-7.5)
    assert drain(link) == [b"alert\0one", b"y" * MAX_PAYLOAD] and link.signal == (-120, -7.5)
    esp.out += b"\x55" * 2000                               # noise that never ends is not kept
    drain(link, 400)
    assert not esp.out and len(link.buffer) <= MAX_FRAME
    esp.out += b"\0"
    esp.deliver(b"while sending")                           # arrives just before the answer to a send
    assert link.send(b"z") and drain(link, 1) == [b"while sending"]
    for bad in (b"", bytes(MAX_PAYLOAD + 1)):
        try:
            link.send(bad)
            raise AssertionError("bad length accepted")
        except ValueError:
            pass

    # 3. Wrong firmware, wrong settings, dead radio or silence at the start: refused
    def refused(change, words):
        e2 = FakeESP()
        change(e2)
        try:
            link_on(e2)
        except OSError as e:
            assert words in str(e), (words, e)
        else:
            raise AssertionError("accepted: " + words)

    refused(lambda e: setattr(e, "protocol", PROTOCOL + 1), "another version")
    for i, name in enumerate(SETTING_NAMES):
        refused(lambda e: setattr(e, "settings", SETTINGS[:i] + (SETTINGS[i] + 1,) + SETTINGS[i + 1:]), name + " is")
    refused(lambda e: setattr(e, "radio_ok", False), "its SX1278 does not")
    refused(lambda e: (setattr(e, "mute", True), setattr(e, "noise", False)), "no answer")
    e2 = FakeESP()
    e2.mute = True                                          # only the unasked-for boot INFO: not enough
    try:
        link_on(e2)
        raise AssertionError("accepted without an answer")
    except OSError:
        pass

    # 4. A radio fault on the ESP is reported; the port stays open
    esp.result = SENT_RADIO_FAULT
    opens = esp.opens
    assert link.send(b"no") is False and not link.broken and esp.opens == opens
    esp.result = SENT_OK

    # 5. No answer, or unplugged: False, never an exception; reopened at most every RECOVER_S
    for fault in ("mute", "dead"):
        setattr(esp, fault, True)
        t0 = esp.t
        assert link.send(b"lost") is False and link.broken, fault
        assert esp.t - t0 <= SEND_TIMEOUT_S + 1
        setattr(esp, fault, False)                          # healthy again at once: still left alone for a while
        opens = esp.opens
        assert link.send(b"too soon") is False and esp.opens == opens
        setattr(esp, fault, True)
        assert link.send(b"still down") is False and drain(link, 50) == []
        n = len(log)
        for _ in range(100):
            link.receive()
        assert len(log) - n <= 2, log[n:]
        setattr(esp, fault, False)
        esp.t += RECOVER_S + 1
        assert link.send(b"back") is True and not link.broken, fault
    esp.dead = True                                         # unplugged while only listening
    assert link.receive() == [] and link.broken
    esp.dead, esp.t = False, esp.t + RECOVER_S + 1
    esp.deliver(b"lost with the old connection")
    link.receive()
    esp.deliver(b"heard again")
    assert drain(link) == [b"heard again"]

    # 6. A restart in the middle of a send ends it at once
    esp.restart_on_send = True
    t0 = esp.t
    assert link.send(b"gone") is False and esp.t - t0 < 1 and not link.broken
    esp.restart_on_send = False
    assert link.send(b"next") is True
    esp.radio_ok = False                                    # restarted and found no radio
    esp.out += esp.info(0)
    drain(link, 20)
    assert link.broken
    esp.radio_ok, esp.t = True, esp.t + RECOVER_S + 1
    assert link.send(b"radio back") is True

    # 7. The receive thread delivers, notices a restart with wrong settings, and survives errors
    esp = FakeESP()
    log.clear()
    link = ESPLink(esp.open, log=log.append)
    real_read, errors = esp.read, [ValueError("unexpected")]

    def flaky_read():
        if errors:
            raise errors.pop()
        return real_read()

    def wait_for(test):
        end = time.monotonic() + 5
        while not test() and time.monotonic() < end:
            time.sleep(0.01)
        return test()

    esp.read = flaky_read
    esp.deliver(b"by the thread")
    assert wait_for(lambda: link.inbox) and link.receive()[0][1] == b"by the thread"
    esp.settings = (SETTINGS[0] + 1,) + SETTINGS[1:]
    esp.out += esp.info(0)
    assert wait_for(lambda: link.broken), log
    t0 = time.monotonic()
    assert link.send(b"x") is False and link.receive() == [] and time.monotonic() - t0 < 0.5
    held, release = threading.Event(), threading.Event()

    def reconnecting():                                     # the thread keeps the port while it reconnects
        with link.io:
            held.set()
            release.wait(3)

    threading.Thread(target=reconnecting, daemon=True).start()
    assert held.wait(8)
    t0 = time.monotonic()
    assert link.send(b"x") is False and link.receive() == [] and time.monotonic() - t0 < 0.5
    release.set()
    link.close()
    assert not link.thread.is_alive()
    print("esp8266_passthrough self-check passed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="SX1278 433 MHz LoRa through an ESP8266 on USB serial.")
    which = ap.add_mutually_exclusive_group(required=True)
    which.add_argument("--radio-test", action="store_true", help="check the modem: send one packet, listen 10 s")
    which.add_argument("--check", action="store_true", help="self-check without hardware")
    ap.add_argument("--port", default=DEFAULT_PORT, help=f"the ESP's serial port (default {DEFAULT_PORT})")
    a = ap.parse_args()
    check() if a.check else radio_test(a.port)
