"""What the two radio drivers share: lora_pi.py (the SX1278 on the Pi's pins) and
esp8266_passthrough.py (the gateway's SX1278 behind an ESP8266). Radio settings that are not in
lora433.py, the packet length rule, the radio test's listening loop, and RadioLink: the part of a
link that does not depend on how the radio is reached.
"""
import threading
import time

# Radio settings both ends must use, besides lora433.py. The ESP sketch repeats them in C++ and
# esp8266_passthrough.py checks what it reports.
BW_BITS, CR_BITS = 0x70, 0x02         # bandwidth 125 kHz, coding rate 4/5, as SX1278 register bits
SYNC_WORD = 0x12                      # private LoRa network (LoRaWAN uses 0x34)
MAX_PAYLOAD = 255                     # bytes in one LoRa packet

RECOVER_S = 5                         # a link that stays faulty is restored at most this often
TEST_PACKET = b"aqua-edge radio test"


def check_payload(data):
    if not 0 < len(data) <= MAX_PAYLOAD:
        raise ValueError(f"a LoRa packet holds 1 to {MAX_PAYLOAD} bytes, not {len(data)}")


def listen_and_print(poll, note=lambda: "", seconds=10):
    """The listening half of a radio test. poll() -> packets (bytes) received since the last call;
    note() -> text to add after a packet."""
    print(f"sent a test packet; listening {seconds} s for any packet...")
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        for packet in poll():
            print(f"received {packet.hex()}{note()}")
        time.sleep(0.01)


class RadioLink:
    """A point-to-point link that carries packet bytes. A fault is logged and the link restored; it
    never reaches the caller. Subclasses add send() and:

        service()    read what has arrived (hand it to collect()), and restore the link if needed
        _failed(e)   what to do on a fault: start with _mark_broken(e)
        _restore()   make a broken link work again; raises OSError if it cannot yet
        _idle()      the receive thread's pause between two service() calls
        _shut()      release the radio, after the receive thread has stopped"""

    NAME = "radio"                    # for the log
    RESTORE_FAILED, RESTORED = "radio recovery failed", "radio set up again"
    JOIN_S = 2                        # longest wait for the receive thread to stop

    def __init__(self, log, clock):
        self.log, self.clock = log, clock
        self.lock, self.recovering, self.inbox = threading.Lock(), threading.Lock(), []
        self.broken, self.next_recover = False, 0.0
        self.stopped, self.thread = threading.Event(), None

    def start_listening(self):
        self.thread = threading.Thread(target=self._listen, daemon=True)
        self.thread.start()

    def collect(self, packets):
        if packets:
            with self.lock:
                self.inbox += [(time.time(), p) for p in packets]

    def drain(self):
        """[(arrival time in time.time() seconds, packet bytes)] collected since the last call."""
        with self.lock:
            got, self.inbox = self.inbox, []
        return got

    def receive(self):
        self.service()
        return self.drain()

    def _mark_broken(self, error):
        self.log(f"{self.NAME} error: {error}")
        self.broken = True

    def _recover(self):
        """Restore a broken link, trying at most every RECOVER_S. True when the link works."""
        with self.recovering:
            if not self.broken:
                return True
            if self.clock() < self.next_recover:
                return False
            try:
                self._restore()
            except OSError as e:
                self.log(f"{self.RESTORE_FAILED}, will try again: {e}")
                return False
            finally:                      # counted from the end: a try can take seconds
                self.next_recover = self.clock() + RECOVER_S
            self.broken = False
            self.log(self.RESTORED)
            return True

    def _idle(self):
        self.stopped.wait(0.01)

    def _listen(self):
        while not self.stopped.is_set():
            try:
                self.service()
            except Exception as e:              # whatever happens, this end must not go deaf
                self.log(f"receive thread error: {e!r}")
                self.stopped.wait(1.0)
            self._idle()

    def close(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=self.JOIN_S)
        self._shut()
