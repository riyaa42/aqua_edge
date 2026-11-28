"""SX1278 433 MHz LoRa on a Raspberry Pi's GPIO pins: the node's point-to-point radio link to the
gateway. Plain LoRa (no LoRaWAN): the bytes given to send() are the whole LoRa payload.

    SX1278     the chip: configure, send, listen before talk (CAD), receive
    LoRaLink   the link the node's hardware layer uses: send() and receive() that never let a
               radio fault stop the program, a receive thread, and recovery of a faulty radio

This file only carries bytes. Signing, addressing, acknowledgements and retries are in
packet.py and edge/aqua_edge.py. Frequency, spreading factor, preamble and power come from
lora433.py. Wiring and setup: hal/README.md, "Running on the Pi". Needs spidev and gpiozero
on the Pi only.

    python3 -m hal.lora_pi --radio-test   # on the Pi: is the module wired right? hear another one?
    python3 -m hal.lora_pi --check        # self-check on any computer, no hardware needed
"""
import argparse
import random
import threading
import time

from hal.common import (BW_BITS, CR_BITS, MAX_PAYLOAD, RECOVER_S, SYNC_WORD, TEST_PACKET, RadioLink,
                        check_payload, listen_and_print)
from hal.lora433 import FREQ_HZ, PREAMBLE, SF, TX_DBM, airtime_s

# Same listen-before-talk rule as the simulation (simulation/live/radio.py)
CAD_TRIES, CAD_BACKOFF_S = 5, (0.1, 1.0)
RX_POLL_S = 0.01                      # how often the receive thread checks the radio
RX_WAIT_S = airtime_s(MAX_PAYLOAD) + 0.05   # longest wait for a packet that is already coming in
TX_TIMEOUT_S, CAD_TIMEOUT_S = 2.0, 1.0
HEALTH_S = 5                          # how often the link checks the radio is still set up

# Pins (BCM numbers). SPI0 is fixed: MOSI 10, MISO 9, SCK 11, NSS = CE0 (8).
RESET_PIN = 22
DIO0_PIN = 25                         # optional wire: wakes the receive thread as soon as a packet is in

# SX1278 registers and modes (SX1276/77/78/79 datasheet, section 6.4: same registers for the whole family)
REG_FIFO, REG_OPMODE, REG_FRF, REG_PA = 0x00, 0x01, 0x06, 0x09
REG_FIFO_PTR, REG_TX_BASE, REG_RX_BASE, REG_RX_CURRENT = 0x0D, 0x0E, 0x0F, 0x10
REG_IRQ, REG_RX_BYTES, REG_MODEM_STAT, REG_MODEM1, REG_MODEM2, REG_PREAMBLE = 0x12, 0x13, 0x18, 0x1D, 0x1E, 0x20
REG_PAYLOAD_LEN, REG_MODEM3, REG_SYNC, REG_DIO_MAP1, REG_VERSION = 0x22, 0x26, 0x39, 0x40, 0x42
SLEEP, STANDBY, TX, RX_CONTINUOUS, CAD = 0x88, 0x89, 0x8B, 0x8D, 0x8F   # 0x80: LoRa mode bit, 0x08: low-frequency band (SX1278, below 525 MHz)
IRQ_RX_DONE, IRQ_CRC_ERROR, IRQ_TX_DONE, IRQ_CAD_DONE, IRQ_CAD_BUSY = 0x40, 0x20, 0x08, 0x04, 0x01
MODEM_RECEIVING = 0x01 | 0x02 | 0x08  # RegModemStat: signal detected, synchronised, header valid
CHIP_VERSION = 0x12


class RadioError(OSError):
    """The radio is missing or not answering as it should."""


class SX1278:
    """Minimal LoRa driver: send, listen-before-talk (CAD) and receive, over SPI. Thread-safe.
    Errors are OSError (SPI faults, RadioError) or TimeoutError; configure() sets the chip up again.
    ponytail: reads the IRQ register over SPI; DIO0, if wired, only wakes the receive thread sooner.

    spi, reset: for tests; leave out on the Pi."""

    def __init__(self, spi=None, reset=None, sleep=time.sleep, clock=time.monotonic):
        self.sleep, self.clock = sleep, clock
        self.lock, self.pending = threading.RLock(), []
        self.rx_event, self.dio0 = threading.Event(), None
        if spi is None:
            import spidev
            from gpiozero import DigitalInputDevice, OutputDevice
            reset = OutputDevice(RESET_PIN, initial_value=True)
            self.dio0 = DigitalInputDevice(DIO0_PIN, pull_up=False)   # pulled down: silent if not wired
            self.dio0.when_activated = self.rx_event.set
            spi = spidev.SpiDev()
            spi.open(0, 0)
            spi.max_speed_hz, spi.mode = 5_000_000, 0
        self.spi, self.reset = spi, reset
        self.version = None
        self.configure()

    def configure(self):
        """Reset the chip, apply every setting and start receiving."""
        with self.lock:
            self.reset.off(); self.sleep(0.01); self.reset.on(); self.sleep(0.01)   # reset pulse
            version = self.read(REG_VERSION)
            if version != CHIP_VERSION:
                raise RadioError(f"no SX1278 found (version register {version:#04x}, expected {CHIP_VERSION:#04x}): "
                                 "check the wiring and that SPI is enabled (raspi-config)")
            self.write(REG_OPMODE, 0x00)               # sleep in FSK mode: LoRa mode can only be set while asleep
            self.write(REG_OPMODE, SLEEP)
            frf = FREQ_HZ * 2 ** 19 // 32_000_000
            for i, shift in enumerate((16, 8, 0)):
                self.write(REG_FRF + i, (frf >> shift) & 0xFF)
            self.write(REG_TX_BASE, 0x00)
            self.write(REG_RX_BASE, 0x00)
            self.write(REG_PA, 0x80 | (TX_DBM - 2))                       # PA_BOOST pin, the one SX1278 boards (e.g. Ra-02) use
            self.write(REG_MODEM1, BW_BITS | CR_BITS)                     # explicit header
            self.write(REG_MODEM2, SF << 4 | 0x04)                        # CRC on
            self.write(REG_MODEM3, 0x04 | (0x08 if SF >= 11 else 0))      # automatic gain; low data rate fix at SF11+
            self.write(REG_PREAMBLE, PREAMBLE >> 8)
            self.write(REG_PREAMBLE + 1, PREAMBLE & 0xFF)
            self.write(REG_SYNC, SYNC_WORD)
            self.write(REG_DIO_MAP1, 0x00)                                # DIO0 = "packet received" while receiving
            self.write(REG_IRQ, 0xFF)
            self.write(REG_OPMODE, RX_CONTINUOUS)
            self.version = version

    def read(self, reg):
        return self.spi.xfer2([reg & 0x7F, 0])[1]

    def write(self, reg, value):
        self.spi.xfer2([reg | 0x80, value])

    def healthy(self):
        """True if the chip answers and is still set up and receiving (a chip that reset itself,
        for example after a power dip, is not)."""
        with self.lock:
            return (self.read(REG_VERSION) == CHIP_VERSION and self.read(REG_SYNC) == SYNC_WORD
                    and self.read(REG_OPMODE) == RX_CONTINUOUS)

    def _wait(self, flag, timeout_s):
        """Wait for an IRQ flag and return all the flags."""
        end = self.clock() + timeout_s
        while True:
            flags = self.read(REG_IRQ)
            if flags & flag:
                break
            if self.clock() > end:
                raise TimeoutError(f"radio did not finish (IRQ flag {flag:#04x})")
            self.sleep(0.001)
        if self.read(REG_VERSION) != CHIP_VERSION:  # a dead SPI line reads all ones: every flag "set"
            raise RadioError("radio stopped answering")
        return flags

    def _take_rx(self):
        """Move a received packet out of the radio's buffer before anything overwrites it."""
        flags = self.read(REG_IRQ)
        if flags & IRQ_RX_DONE:
            if not flags & IRQ_CRC_ERROR:
                n = self.read(REG_RX_BYTES)
                self.write(REG_FIFO_PTR, self.read(REG_RX_CURRENT))
                self.pending.append(bytes(self.spi.xfer2([REG_FIFO] + [0] * n)[1:]))
            self.write(REG_IRQ, IRQ_RX_DONE | IRQ_CRC_ERROR)

    def _let_reception_finish(self):
        """The radio cannot send or listen for activity without dropping a packet that is coming
        in (often our ACK). Wait for it, at most the longest packet's time. True: still coming in."""
        end = self.clock() + RX_WAIT_S
        while self.read(REG_MODEM_STAT) & MODEM_RECEIVING and not self.read(REG_IRQ) & IRQ_RX_DONE:
            if self.clock() > end:
                return True
            self.sleep(0.002)
        return False

    def _leave_rx(self):
        """Go to standby without losing a packet that has just finished."""
        self._take_rx()
        self.write(REG_OPMODE, STANDBY)
        self._take_rx()                 # one may have finished between the two lines above
        self.write(REG_IRQ, 0xFF)

    def send(self, data):
        check_payload(data)
        with self.lock:
            self._let_reception_finish()
            self._leave_rx()
            self.write(REG_FIFO_PTR, 0x00)
            self.spi.xfer2([REG_FIFO | 0x80] + list(data))
            self.write(REG_PAYLOAD_LEN, len(data))
            self.write(REG_OPMODE, TX)
            self._wait(IRQ_TX_DONE, TX_TIMEOUT_S)
            self.write(REG_IRQ, 0xFF)
            self.write(REG_OPMODE, RX_CONTINUOUS)

    def cad(self):
        """Listen before talk: True if someone else is sending on our channel right now."""
        with self.lock:
            if self._let_reception_finish():
                return True
            self._leave_rx()
            self.write(REG_OPMODE, CAD)
            busy = bool(self._wait(IRQ_CAD_DONE, CAD_TIMEOUT_S) & IRQ_CAD_BUSY)
            self.write(REG_IRQ, 0xFF)
            self.write(REG_OPMODE, RX_CONTINUOUS)
            return busy

    def poll(self):
        """Packets (bytes) received since the last call. Packets with a bad CRC are dropped."""
        with self.lock:
            self._take_rx()
            got, self.pending = self.pending, []
            return got

    def close(self):
        with self.lock:
            self.write(REG_OPMODE, SLEEP)
            self.spi.close()


class LoRaLink(RadioLink):
    """The point-to-point link to the gateway, as raw bytes. A radio fault is logged and the radio
    set up again; it never reaches the caller, so the node keeps running its checks and local
    alerts. Nothing is lost by a failed send: the node keeps each message until it is
    acknowledged, and the gateway repeats a command until it is confirmed.

    radio: an SX1278 (or anything with send, poll and cad; configure and healthy are optional)."""

    def __init__(self, radio, sleep=time.sleep, log=print, listen=True, clock=time.monotonic):
        super().__init__(log, clock)
        self.radio, self.sleep, self.next_health = radio, sleep, 0.0
        if listen:
            self.start_listening()

    def send(self, raw, cad=True):
        """Send one packet now. cad: listen first, back off while the channel is busy, then send
        anyway (an alert must not be stuck). False if the radio failed and nothing was sent."""
        if self.broken and not self._recover():
            return False
        try:
            if cad:
                for _ in range(CAD_TRIES):
                    if not self.radio.cad():
                        break
                    self.sleep(random.uniform(*CAD_BACKOFF_S))
            self.radio.send(raw)
        except OSError as e:
            self._failed(e)
            return False
        return True

    def service(self):
        """Collect incoming packets, check the radio now and then, and recover it if needed."""
        if self.broken and not self._recover():
            return
        try:
            self.collect(self.radio.poll())     # first, so packets are kept even if the check fails
            if self.clock() >= self.next_health:
                self.next_health = self.clock() + HEALTH_S
                healthy = getattr(self.radio, "healthy", None)
                if healthy and not healthy():
                    self._failed(RadioError("radio is no longer set up and receiving"))
        except OSError as e:
            self._failed(e)

    def _failed(self, error):
        self._mark_broken(error)
        self._recover()                         # at once: setting the chip up again is quick

    def _restore(self):
        getattr(self.radio, "configure", lambda: None)()

    def _idle(self):
        wake = getattr(self.radio, "rx_event", None) or self.stopped
        wake.wait(RX_POLL_S)                    # DIO0 ends this early; otherwise poll
        if wake is not self.stopped:
            wake.clear()

    def _shut(self):
        try:
            getattr(self.radio, "close", lambda: None)()
        except OSError as e:
            self.log(f"radio error while closing: {e}")


def radio_test():
    """To test two modules: start one, then the other within 10 s. Each sends once as it starts, so
    only the one started first prints the other's packet; swap the order to test the other way."""
    r = SX1278()
    print(f"SX1278 found (version {r.version:#04x}), {FREQ_HZ / 1e6:.4f} MHz, SF{SF}")
    print("channel busy" if r.cad() else "channel free (CAD)")
    r.send(TEST_PACKET)
    listen_and_print(r.poll)
    r.close()


def check():
    class FakeChip:
        """Register-level stand-in for the SX1278 and its reset line."""

        def __init__(self):
            self.t, self.sent, self.resets, self.cad_busy, self.tx_hangs = 0.0, [], 0, False, False
            self.dead = None            # None, "error" (SPI raises) or "ones" (reads all ones)
            self.on_standby = self.on_sleep = None
            self.power_on()

        def power_on(self):
            self.regs, self.fifo = {REG_VERSION: CHIP_VERSION, REG_OPMODE: 0x09, REG_SYNC: 0x12}, bytearray(256)

        def off(self): pass

        def on(self):
            self.resets += 1
            self.power_on()

        def sleep(self, s):
            self.t += s
            if self.on_sleep:
                self.on_sleep()

        def clock(self):
            return self.t

        def deliver(self, packet, crc_ok=True):
            self.fifo[0:len(packet)] = packet
            self.regs.update({REG_RX_CURRENT: 0, REG_RX_BYTES: len(packet), REG_MODEM_STAT: 0})
            self.regs[REG_IRQ] = self.regs.get(REG_IRQ, 0) | IRQ_RX_DONE | (0 if crc_ok else IRQ_CRC_ERROR)

        def xfer2(self, data):
            if self.dead == "error":
                raise OSError("SPI transfer failed")
            if self.dead == "ones":
                return [0xFF] * len(data)
            reg, writing = data[0] & 0x7F, data[0] & 0x80
            ptr = self.regs.get(REG_FIFO_PTR, 0)
            if reg == REG_FIFO:
                if writing:
                    self.fifo[ptr:ptr + len(data) - 1] = bytes(data[1:])
                    return [0] * len(data)
                return [0] + list(self.fifo[ptr:ptr + len(data) - 1])
            if not writing:
                return [0, self.regs.get(reg, 0)]
            value = data[1]
            if reg == REG_IRQ:                              # writing a 1 clears that flag
                self.regs[reg] = self.regs.get(reg, 0) & ~value
                return [0, 0]
            self.regs[reg] = value
            if reg == REG_OPMODE:
                flags = self.regs.get(REG_IRQ, 0)
                if value == STANDBY and self.on_standby:
                    hook, self.on_standby = self.on_standby, None
                    hook()
                elif value == TX and not self.tx_hangs:
                    self.sent.append(bytes(self.fifo[:self.regs[REG_PAYLOAD_LEN]]))
                    self.regs[REG_IRQ] = flags | IRQ_TX_DONE
                elif value == CAD:
                    self.regs[REG_IRQ] = flags | IRQ_CAD_DONE | (IRQ_CAD_BUSY if self.cad_busy else 0)
            return [0, 0]

        def close(self): pass

    def radio_on(chip):
        return SX1278(spi=chip, reset=chip, sleep=chip.sleep, clock=chip.clock)

    def link_on(chip, radio):
        log = []
        return LoRaLink(radio, sleep=chip.sleep, log=log.append, listen=False, clock=chip.clock), log

    # 1. Settings: 433.175 MHz, 10 dBm on PA_BOOST, 125 kHz, 4/5, SF7, CRC, 8-symbol preamble
    chip = FakeChip()
    radio = radio_on(chip)
    frf = chip.regs[REG_FRF] << 16 | chip.regs[REG_FRF + 1] << 8 | chip.regs[REG_FRF + 2]
    assert abs(frf * 32_000_000 / 2 ** 19 - FREQ_HZ) < 62, frf
    assert (chip.regs[REG_PA], chip.regs[REG_MODEM1], chip.regs[REG_MODEM2], chip.regs[REG_MODEM3]) == (0x88, 0x72, 0x74, 0x04)
    assert (chip.regs[REG_PREAMBLE], chip.regs[REG_PREAMBLE + 1], chip.regs[REG_SYNC]) == (0, PREAMBLE, SYNC_WORD)
    assert radio.healthy()

    # 2. Send, receive, bad CRC, channel activity
    radio.send(b"to the gateway")
    assert chip.sent == [b"to the gateway"] and chip.regs[REG_OPMODE] == RX_CONTINUOUS
    chip.deliver(b"from the gateway")
    assert radio.poll() == [b"from the gateway"] and radio.poll() == []
    chip.deliver(b"garbled", crc_ok=False)
    assert radio.poll() == []
    assert radio.cad() is False
    chip.cad_busy = True
    assert radio.cad() is True and chip.regs[REG_OPMODE] == RX_CONTINUOUS
    chip.cad_busy = False
    for bad in (b"", bytes(MAX_PAYLOAD + 1)):
        try:
            radio.send(bad)
            raise AssertionError("bad length accepted")
        except ValueError:
            pass

    # 3. A packet coming in is waited for and kept, before a send and before CAD
    for action in (lambda: radio.send(b"x"), radio.cad):
        chip.regs[REG_MODEM_STAT], until = MODEM_RECEIVING, chip.t + 0.1

        def arrive():
            if chip.t >= until:
                chip.on_sleep = None
                chip.deliver(b"ACK on its way")
        chip.on_sleep = arrive
        assert not action() and radio.poll() == [b"ACK on its way"] and chip.t >= until
    chip.regs[REG_MODEM_STAT] = MODEM_RECEIVING             # never ends: bounded wait, channel busy
    t0 = chip.t
    assert radio.cad() is True and RX_WAIT_S <= chip.t - t0 < RX_WAIT_S + 0.1
    chip.regs[REG_MODEM_STAT] = 0

    # 4. A packet that finishes just as the radio leaves receive mode is not lost
    chip.on_standby = lambda: chip.deliver(b"last moment")
    radio.send(b"y")
    assert radio.poll() == [b"last moment"]

    # 5. Faults never reach the caller; the radio is set up again
    link, log = link_on(chip, radio)
    chip.tx_hangs, resets = True, chip.resets
    assert link.send(b"stuck", cad=False) is False and chip.resets == resets + 1 and not link.broken
    chip.tx_hangs = False
    assert link.send(b"works again") is True and chip.sent[-1] == b"works again"
    for fault in ("error", "ones"):                         # SPI raising; dead SPI line reading all ones
        chip.t += RECOVER_S
        chip.dead = fault
        assert link.send(b"lost for now", cad=False) is False and link.broken, fault
        assert link.receive() == [] and link.send(b"still down") is False
        chip.dead, chip.t = None, chip.t + RECOVER_S
        assert link.send(b"back") is True and not link.broken, fault
    n = len(log)
    chip.dead = "error"
    for _ in range(100):                                    # stays faulty: retries and log are limited
        link.receive()
    assert len(log) - n <= 3, log[n:]
    chip.dead, chip.t = None, chip.t + HEALTH_S + RECOVER_S

    # 6. A radio that reset itself (power dip) is noticed and set up again
    link.receive()
    chip.power_on()
    assert not radio.healthy()
    chip.t += HEALTH_S + RECOVER_S
    link.receive()
    assert radio.healthy() and not link.broken
    chip.deliver(b"heard after recovery")
    assert [p for _, p in link.receive()] == [b"heard after recovery"]

    # 7. The receive thread survives any error and keeps delivering
    class Flaky:
        def __init__(self): self.errors = [OSError("SPI glitch"), ValueError("unexpected")]
        def poll(self):
            if self.errors:
                raise self.errors.pop(0)
            return [b"still listening"]
        def send(self, data): pass
        def cad(self): return False

    link = LoRaLink(Flaky(), log=lambda m: None)
    end, got = time.monotonic() + 5, []
    while not got and time.monotonic() < end:
        time.sleep(0.05)
        with link.lock:
            got = list(link.inbox)
    link.close()
    assert got and got[0][1] == b"still listening" and not link.thread.is_alive()
    print("lora_pi self-check passed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="SX1278 433 MHz LoRa on a Raspberry Pi.")
    which = ap.add_mutually_exclusive_group(required=True)
    which.add_argument("--radio-test", action="store_true", help="check the LoRa module: send one packet, listen 10 s")
    which.add_argument("--check", action="store_true", help="self-check without hardware")
    a = ap.parse_args()
    check() if a.check else radio_test()
