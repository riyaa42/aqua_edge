"""Regression tests for the Communication Reliability Audit remediation (Priorities 1-6).

    python3 -m tests.test_comms_reliability
"""
import json
import socket
import struct
import tempfile
import time

from comms.gateway import GatewayComms
from edge.aqua_edge import FlatNode, STALE_S, STUCK_S
from edge.board import NodeBoard
from edge.edge_based import _wait_with_radio
from gateway.core import MAX_EVENTS, MAX_LOG, Gateway
from gateway.dashboard_link import DashboardFeed
from gateway.topology import Topology
from hal.esp8266_passthrough import (
    INFO_FORMAT,
    PROTOCOL,
    SENT,
    SENT_OK,
    SENT_RADIO_FAULT,
    SETTINGS,
    ESPLink,
    frame,
    unframe,
)
from hal.link import MessageLink
from hal.lora433 import DUTY_WINDOW_S, AirtimeBudget
from hal.lora_pi import LoRaLink
from hal.packet import pack, sim_key, unpack
from hal.udp import UdpHub, UdpRadio


class FakeGatewayHal:
    def __init__(self, start=1_700_000_000.0):
        self.t = start
        self.inbox = []
        self.sent = []
        self.send_result = True
        self.raise_on_send = None
        self.send_delay_s = 0.0
        self.storage_path = None

    def now(self):
        return self.t

    def key(self, name):
        return sim_key(name)

    def send(self, raw, cad=False):
        if self.send_delay_s:
            time.sleep(self.send_delay_s)
        if self.raise_on_send is not None:
            raise self.raise_on_send
        if not self.send_result:
            return False
        self.sent.append((raw, cad))
        return True

    def receive(self):
        got, self.inbox = self.inbox, []
        return got


def test_priority1_radio_result_propagation():
    class ToggleRadio:
        def __init__(self):
            self.ok = True
            self.raise_err = None
            self.sent = []

        def send(self, raw, cad=False):
            if self.raise_err is not None:
                raise self.raise_err
            if not self.ok:
                return False
            self.sent.append((raw, cad))
            return True

        def receive(self):
            return []

    radio = ToggleRadio()
    link = MessageLink(radio, sim_key)
    cmd = {"k": "CMD", "n": "B-101", "s": 1, "t": 100, "a": "PIPE_LEAK", "over": 0}

    assert link.send(cmd) is True
    assert len(radio.sent) == 1

    # Admission rejection: radio.send is not called
    assert link.send(cmd, admit=lambda _s: False) is False
    assert len(radio.sent) == 1

    # Underlying radio failure must propagate False
    radio.ok = False
    assert link.send(cmd) is False
    assert len(radio.sent) == 1

    # Exceptions propagate cleanly
    radio.raise_err = OSError("tx fault")
    try:
        link.send(cmd)
    except OSError:
        pass
    else:
        raise AssertionError("expected OSError from radio.send")

    # Gateway.send_commands() must not advance tries or charge airtime on radio failure
    hal = FakeGatewayHal()
    gw = Gateway(hal, Topology.load())
    gw.command(hal.t, "B-101", "PIPE_LEAK", hal.t)
    cmd_num = str(gw.command_number)
    hal.send_result = False
    gw.step()
    assert gw.tries.get(cmd_num, 0) == 0, "failed radio send must not increment tries"
    assert gw.airtime.total == 0.0, "failed radio send must not charge airtime"
    assert cmd_num in gw.outbox, "unconfirmed command remains in outbox"

    # When radio succeeds, tries increments, airtime is charged, yet command stays until GOT
    hal.send_result = True
    hal.t += 61
    gw.step()
    assert gw.tries.get(cmd_num) == 1
    assert gw.airtime.total > 0.0
    assert cmd_num in gw.outbox, "radio TX success != destination ACK (GOT)"

    # Exception during transmit must also refund airtime
    budget = AirtimeBudget()
    assert budget.take(10.0, 0.5, True)
    budget.refund_last()
    assert budget.total == 0.0
    print("ok: priority 1 (radio result propagation & airtime refund)")


def test_priority2_clock_correction_and_monotonic_timers():
    class FakeWallAndMonoRadio:
        def __init__(self):
            self.inbox = []
            self.sent = []
            self.broken = False

        def send(self, raw, cad=False):
            self.sent.append((raw, cad))
            return True

        def receive(self):
            got, self.inbox = self.inbox, []
            return got

    # Case A: Pi clock is 1 hour AHEAD of gateway; first ACK jumps wall clock BACKWARD by 3600s
    with tempfile.TemporaryDirectory() as tmp:
        radio = FakeWallAndMonoRadio()
        keys = {"B-101": sim_key("B-101"), "GATEWAY": sim_key("GATEWAY")}
        row_t = [100_000.0]

        def read_row():
            row_t[0] += 60.0
            return row_t[0], {"flow_lps": 0.01, "pressure_m": 12.0}

        wall_clock = [1_700_003_600.0]  # 1 hour ahead of gateway (1_700_000_000.0)
        mono_clock = [100.0]

        board = NodeBoard("B-101", radio, read_row, f"{tmp}/b101.jsonl", keys, slot=(2, 20), gpio=False)
        board.now = lambda: wall_clock[0] + board.clock_fix
        board.monotonic = lambda: mono_clock[0]
        board.sleep = lambda dt: mono_clock.__setitem__(0, mono_clock[0] + dt)

        node = FlatNode("B-101", board)
        board.advance()
        node.step()
        # Queue a summary before any ACK
        node.emit("SUM", i=1, vol_l=1.0)
        wake_before = node.transmit(board.now())
        assert wake_before is not None and wake_before < 200.0, "wake must be on monotonic clock"

        # First ACK arrives from gateway at wall=1_700_000_005.0 (Pi was 3600s ahead)
        ack = pack({"k": "ACK", "n": "B-101", "s": 999, "gt": 1_700_000_005.0}, sim_key("B-101"))
        radio.inbox.append((wall_clock[0], ack))
        wake_after = node.radio_poll()
        assert abs(board.now() - 1_700_000_005.0) < 1e-6
        assert wake_after is not None and abs(wake_after - wake_before) < 1e-6, "summary wake must not jump by 3600s"

        # Stale & stuck timers use monotonic time, unaffected by -3600s wall jump
        mono_clock[0] += STUCK_S + 10
        node.transmit(board.now())
        assert node.stuck is True, "stuck timer must advance monotonically despite backward wall-clock jump"

        # ACK the queued SUM so stuck (Y1) clears, then verify stale (Y2) triggers on monotonic time
        for m in list(node.store.queue):
            ack_m = pack({"k": "ACK", "n": "B-101", "s": m["s"], "gt": 1_700_000_006.0}, sim_key("B-101"))
            radio.inbox.append((wall_clock[0], ack_m))
        node.radio_poll()
        assert node.stuck is False
        mono_clock[0] += STALE_S + 10
        assert node.signal(mono_clock[0]) == ("yellow", 2), "stale timer must trigger on monotonic elapsed time"

        # _wait_with_radio must not stall for 3600s if clock_fix jumps backward mid-wait
        board.clock_fix = 0.0
        until_t = board.monotonic() + 0.05
        ack2 = pack({"k": "ACK", "n": "B-101", "s": 1000, "gt": 1_700_000_010.0}, sim_key("B-101"))
        radio.inbox.append((wall_clock[0] + 10.0, ack2))
        _wait_with_radio(node, board, until_t, poll_s=0.01)
        assert mono_clock[0] - (until_t - 0.05) < 0.2, "wait loop must use monotonic deadline"

    # Case B: Pi clock is 1 hour BEHIND gateway; airtime window must not prematurely expire on +3600s jump
    budget = AirtimeBudget()
    assert budget.take(100.0, 280.0, False)
    assert not budget.take(101.0, 20.0, False), "exceeds 8% routine budget"
    assert budget.take(100.0 + DUTY_WINDOW_S + 1.0, 20.0, False), "expires after monotonic DUTY_WINDOW_S"
    print("ok: priority 2 (clock correction & monotonic timers)")


def test_priority3_gateway_nonblocking_tx():
    # 1) Async ESPLink with simulated CAD contention (SENT delayed)
    class ContendedSerial:
        def __init__(self):
            self.out = bytearray()
            self.sent_count = 0
            self.pending_sent = []
            self.t = 0.0

        def clock(self):
            return self.t

        def sleep(self, s):
            self.t += s

        def read(self):
            # Deliver delayed SENT frames only when flushed
            got, self.out = bytes(self.out), bytearray()
            return got

        def write(self, data):
            kind, seq, body = unframe(data[:-1])
            if kind == 0x01:  # HELLO
                info = struct.pack(INFO_FORMAT, PROTOCOL, True, 0x12, *SETTINGS)
                self.out += frame(0x81, seq, info)
            elif kind == 0x02:  # SEND (hold SENT to simulate CAD contention)
                self.sent_count += 1
                self.pending_sent.append(seq)

        def deliver_rx(self, raw):
            self.out += frame(0x83, 0, struct.pack(">hb", -70, 32) + raw)

        def finish_send(self, ok=True):
            seq = self.pending_sent.pop(0)
            self.out += frame(SENT, seq, bytes((SENT_OK if ok else SENT_RADIO_FAULT,)))

        def close(self):
            pass

    esp = ContendedSerial()
    link = ESPLink(lambda: esp, listen=False, clock=esp.clock, sleep=esp.sleep)

    class AsyncBoard:
        storage_path = None
        key = staticmethod(sim_key)

        def __init__(self):
            self.t = 1_700_000_000.0
            self.start_send = link.start_send
            self.poll_send = link.poll_send

        def now(self):
            return self.t

        def send(self, raw, cad=False):
            return link.send(raw, cad)

        def receive(self):
            return [raw for _, raw in link.receive()]

    gw = Gateway(AsyncBoard(), Topology.load())
    gw.command(gw.hal.t, "B-101", "PIPE_LEAK", gw.hal.t)
    cmd_num = str(gw.command_number)

    # First step starts async CMD send and returns immediately despite contention
    t0 = time.monotonic()
    gw.step()
    dt1 = time.monotonic() - t0
    assert dt1 < 0.05, f"Gateway.step() blocked for {dt1:.3f}s"
    assert gw._tx_inflight is not None
    assert gw.tries.get(cmd_num, 0) == 0, "tries must not increment while TX is in flight"

    # Incoming packet arrives while TX is still in CAD backoff: processed on next step!
    alert_raw = pack({"k": "ALERT", "n": "B-102", "s": 1, "t": gw.hal.t, "a": "LEAK_NIGHT_FLOW", "flow": 0.05}, sim_key("B-102"))
    esp.deliver_rx(alert_raw)
    t0 = time.monotonic()
    gw.step()
    dt2 = time.monotonic() - t0
    assert dt2 < 0.05
    assert any(m["n"] == "B-102" for m in gw.log), "incoming packet must not be stalled by TX contention"

    # Complete the contended send -> tries increments
    esp.finish_send(ok=True)
    gw.step()
    assert gw.tries.get(cmd_num) == 1

    # 2) Synchronous slow/contended radio: burst of 10 ACKs is time-boxed per step()
    hal = FakeGatewayHal()
    hal.send_delay_s = 0.015
    gw2 = Gateway(hal, Topology.load())
    for idx in range(1, 11):
        node = f"B-10{(idx - 1) % 4 + 1}"
        hal.inbox.append(pack({"k": "SUM", "n": node, "s": idx, "t": hal.t, "i": idx, "vol_l": 1.0}, sim_key(node)))
    t0 = time.monotonic()
    wake = gw2.step()
    dt_sync = time.monotonic() - t0
    assert dt_sync < 0.06, f"synchronous contended step() took {dt_sync:.3f}s"
    assert len(gw2._tx_queue) > 0 and wake == hal.t, "remaining ACKs scheduled immediately without blocking"
    while gw2._tx_queue:
        gw2.step()
    print(f"ok: priority 3 (non-blocking gateway TX: async={dt1*1000:.2f}ms, sync_budget={dt_sync*1000:.1f}ms)")


def test_priority4_bounded_gateway_storage():
    hal = FakeGatewayHal()
    gw = Gateway(hal, Topology.load())

    class MemBroker:
        def __init__(self):
            self.count = 0

        def publish(self, where, data, qos, retain, expiry_s):
            self.count += 1

    class MemShipper:
        timeout_s = 1.0
        connected = True
        last_error = None
        queue = []

        def __init__(self):
            self.events = []

        def enqueue(self, snapshot, new_events=None, created_at_utc=None):
            if new_events:
                self.events.extend(new_events)

        def flush_once(self):
            pass

    broker = MemBroker()
    comms = GatewayComms(gw, broker).attach()
    shipper = MemShipper()
    feed = DashboardFeed(gw, shipper, interval_s=0)

    total = MAX_EVENTS + 500
    for i in range(1, total + 1):
        hal.t += 1.0
        hal.inbox.append(pack({"k": "SUM", "n": "B-101", "s": i, "t": hal.t, "i": i, "vol_l": 1.0}, sim_key("B-101")))
        gw.decide(hal.t, "NEW_ALERT", node="B-101", alert=f"A{i}")
        gw.step()
        feed.update(force=True)
        while not feed.inbox.empty():
            feed._store(feed.inbox.get_nowait())

    # Trigger one final trim
    gw.step()
    assert len(gw.events) <= MAX_EVENTS, f"events grew to {len(gw.events)}"
    assert len(gw.log) <= MAX_LOG, f"log grew to {len(gw.log)}"
    assert len(shipper.events) >= total, f"DashboardFeed lost events: got {len(shipper.events)} < {total}"
    assert comms.decided == gw.events_offset + len(gw.events)
    assert comms.logged == gw.log_offset + len(gw.log)
    print("ok: priority 4 (bounded gateway storage with zero event loss)")


def test_priority5_radio_interface_consistency():
    msg = {"k": "ALERT", "n": "B-203", "s": 1, "t": 60, "a": "LEAK_NIGHT_FLOW", "flow": 0.05}
    raw = pack(msg, sim_key("B-203"))

    # 1) LoRaLink
    class FakeSX:
        def __init__(self):
            self.rx = [raw]
        def send(self, d): pass
        def cad(self): return False
        def poll(self): got, self.rx = self.rx, []; return got
    lora = LoRaLink(FakeSX(), listen=False)
    items, rejected = MessageLink(lora, sim_key).receive_timed()
    assert rejected == 0 and len(items) == 1 and items[0][1] == msg and isinstance(items[0][0], float)

    # 2) ESPLink
    class FakePort:
        def __init__(self):
            self.buf = frame(0x83, 0, struct.pack(">hb", -65, 20) + raw)
        def read(self): got, self.buf = self.buf, b""; return got
        def write(self, data):
            kind, seq, _ = unframe(data[:-1])
            if kind == 0x01:
                self.buf += frame(0x81, seq, struct.pack(INFO_FORMAT, PROTOCOL, True, 0x12, *SETTINGS))
        def close(self): pass
    esp = ESPLink(FakePort, listen=False)
    got, rejected = MessageLink(esp, sim_key).receive()
    assert rejected == 0 and got == [msg]

    # 3) UdpHub + UdpRadio
    hub = UdpHub(port=0)
    node_udp = UdpRadio("127.0.0.1", hub.port)
    try:
        node_udp.receive()  # hello
        end = time.time() + 2
        while not hub.peers and time.time() < end:
            hub.receive()
        assert node_udp.send(raw)
        out = []
        end = time.time() + 2
        while not out and time.time() < end:
            out, rej = MessageLink(hub, sim_key).receive()
            assert rej == 0
        assert out == [msg]

        assert hub.send(raw)
        out2 = []
        end = time.time() + 2
        while not out2 and time.time() < end:
            out2, rej2 = MessageLink(node_udp, sim_key).receive()
            assert rej2 == 0
        assert out2 == [msg]
    finally:
        node_udp.close()
        hub.close()
    print("ok: priority 5 (radio interface consistency across LoRaLink/ESPLink/UdpRadio/UdpHub)")


def test_priority6_udp_reliability():
    # 1) DNS failure clears stale address and preserves failing state until DNS recovers
    logs = []
    now = [0.0]
    radio = UdpRadio("127.0.0.1", 1700, clock=lambda: now[0], log=logs.append)
    try:
        radio._hello()
        assert radio.address is not None and radio.failing is False

        orig_gethost = socket.gethostbyname
        try:
            socket.gethostbyname = lambda _h: (_ for _ in ()).throw(socket.gaierror("dns down"))
            now[0] += 10.0
            radio._hello()
            assert radio.address is None, "failed DNS must clear stale address"
            assert radio.failing is True, "failed DNS must keep failing=True"
        finally:
            socket.gethostbyname = orig_gethost

        now[0] += 10.0
        radio._hello()
        assert radio.address is not None and radio.failing is False, "must recover when DNS works again"
    finally:
        radio.close()

    # 2) Windows-style ConnectionResetError during _drain() must not drop queued packets
    from hal.udp import _drain

    class ResetThenDataSock:
        def __init__(self):
            self.calls = 0

        def recvfrom(self, _bufsize):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionResetError(10054, "WSAECONNRESET")
            if self.calls == 2:
                return b"valid-packet", ("127.0.0.1", 50001)
            raise BlockingIOError()

    drained = _drain(ResetThenDataSock())
    assert len(drained) == 1 and drained[0][1] == b"valid-packet"

    # Genuine OSError still stops draining
    class BrokenSock:
        def recvfrom(self, _bufsize):
            raise OSError("bad fd")

    assert _drain(BrokenSock()) == []
    print("ok: priority 6 (UDP DNS recovery & ConnectionResetError drain)")


def main():
    test_priority1_radio_result_propagation()
    test_priority2_clock_correction_and_monotonic_timers()
    test_priority3_gateway_nonblocking_tx()
    test_priority4_bounded_gateway_storage()
    test_priority5_radio_interface_consistency()
    test_priority6_udp_reliability()
    print("ALL COMMUNICATION RELIABILITY TESTS PASSED")


if __name__ == "__main__":
    main()
