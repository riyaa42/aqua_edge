"""The Pi node put together (edge/edge_based.py, edge/board.py, edge/sensors/ and the HAL's
LoRa link) with a fake radio chip and no GPIO. Runs on any computer.

    uv run python -m tests.test_pi_node
"""
import os
import tempfile
import time

from edge.aqua_edge import DAY, HOUR
from edge.board import NodeBoard, seconds_until
from edge.edge_based import _wait_with_radio, make_node, run
from edge.sensors import sensor_reader
from hal.lora_pi import CAD_TRIES, LoRaLink
from hal.packet import pack, sim_key, unpack


class FakeRadio:
    def __init__(self):
        self.sent, self.air, self.busy_left, self.cads = [], [], 0, 0

    def send(self, data):
        self.sent.append(data)

    def poll(self):
        got, self.air = self.air, []
        return got

    def cad(self):
        self.cads += 1
        if self.busy_left:
            self.busy_left -= 1
            return True
        return False


def board_for(name, step_s, storage=None):
    radio, slept = FakeRadio(), []
    link = LoRaLink(radio, sleep=lambda s: board.sleep(s), log=lambda m: board.log(m), listen=False)
    keys = {name: sim_key(name), "GATEWAY": sim_key("GATEWAY")}
    board = NodeBoard(name, link, sensor_reader(name), storage, keys, slot=(5, 20), step_s=step_s, gpio=False)
    board.log, board.sleep = (lambda *a: None), slept.append
    return board, radio, slept


def check_sensor_reader():
    path = os.path.join(tempfile.mkdtemp(), "data.csv")
    with open(path, "w") as f:
        f.write("t_s,B-101:flow_lps,B-1010:flow_lps,B-101:pressure_m\n0,1,9,2\n60,3,9,4\n")
    first, second = sensor_reader("B-101", path), sensor_reader("B-101", path)
    assert first() == (0.0, {"flow_lps": 1.0, "pressure_m": 2.0}), "only this node's columns"
    assert first() == (60.0, {"flow_lps": 3.0, "pressure_m": 4.0})
    assert first() is None and first() is None, "None at the end, and after it"
    assert second()[0] == 0.0, "each reader keeps its own place"
    for node, where, error in (("B-999", path, ValueError), ("B-101", path + ".missing", OSError)):
        try:
            sensor_reader(node, where)
        except error:
            continue
        raise AssertionError(f"accepted {node} in {where}")
    board, _, _ = board_for("B-203", 0)
    rows = 0
    while board.advance():
        rows += 1
    assert rows == 48 * 60 and board.sample_time() == (rows - 1) * 60 and board.read(), "the whole default dataset"


def main():
    check_sensor_reader()
    # 1. Only this node's columns, one row per step. Detectors get the row's time; messages the real one
    board, radio, _ = board_for("B-203", 0)
    assert board.advance() and board.sample_time() == 0 and set(board.read()) == {"flow_lps", "pressure_m"}
    assert board.advance() and board.sample_time() == 60 and abs(board.now() - time.time()) < 5

    # 2. Whole planted scenario, fast mode: the leak in B-203 (day 2, 02:00) goes out on the radio
    board, radio, _ = board_for("B-203", 0)
    node, raised = make_node("B-203", board), []
    emit = node.emit
    node.emit = lambda kind, **m: (raised.append((board.sample_time(), m["a"])) if kind == "ALERT" else None,
                                   emit(kind, **m))
    run(node, board, step_s=0)
    msgs = [unpack(p, board.key) for p in radio.sent]
    assert {m["a"] for m in msgs if m["k"] == "ALERT"} >= {"LEAK_NIGHT_FLOW", "VALVE_CLOSED"}
    assert node.valve["closed"] and board.actuators.valve_closed and board.colour == "red"
    alerts = [x for x in raised if x[1] != "VALVE_CLOSED"]
    assert alerts and alerts[0][1] == "LEAK_NIGHT_FLOW", alerts
    assert DAY + 2 * HOUR <= alerts[0][0] <= DAY + 3 * HOUR, alerts[0]
    assert radio.cads >= 1, "alerts listen before sending"

    # 3. A signed ACK deletes the message and sets the clock; a forged ACK does neither
    board, radio, _ = board_for("B-104", None, os.path.join(tempfile.mkdtemp(), "B-104.jsonl"))
    node = make_node("B-104", board)
    node.emit("SUM", i=0, vol_l=1.0, p_min=10.0)
    s = node.store.queue[0]["s"]
    ack = {"k": "ACK", "n": "B-104", "s": s, "gt": round(time.time()) + 3600}   # gateway an hour ahead
    radio.air = [pack(ack, b"attacker-guessed")]
    board.advance(); node.step()
    assert board.clock_fix == 0 and any(m["s"] == s for m in node.store.queue), "forged ACK ignored"
    radio.air = [pack(ack, sim_key("B-104"))]
    board.advance(); node.step()
    assert 3590 < board.clock_fix < 3610, board.clock_fix
    assert all(m["s"] != s for m in node.store.queue), "real ACK deletes the message"

    # A recorded ACK sent again later (or any older one) must not move the clock back
    newer = {**ack, "s": s + 1, "gt": ack["gt"] + 100}
    radio.air = [pack(newer, sim_key("B-104"))]
    board.advance(); node.step()
    fixed = board.clock_fix
    assert 3690 < fixed < 3710, fixed
    for replayed in (ack, newer, {**ack, "gt": ack["gt"] - 9000}):
        radio.air = [pack(replayed, sim_key("B-104"))]
        board.advance(); node.step()
        assert board.clock_fix == fixed, ("a replayed ACK moved the clock", replayed)

    # 4. Time slot wait
    assert abs(seconds_until(123, 12.2) - 9.2) < 1e-9          # second 3 -> wait for second 12.2
    assert abs(seconds_until(150, 12.2) - 42.2) < 1e-9         # second 30: slot gone, next minute's

    # 5. Busy channel: back off 5 times, then send anyway (an alert must not be stuck)
    board, radio, slept = board_for("B-203", 0)
    radio.busy_left = 99
    board.advance()
    board.send(pack({"k": "ALERT", "n": "B-203", "s": 1, "t": 0, "a": "LEAK_NIGHT_FLOW"}, sim_key("B-203")), cad=True)
    assert radio.cads == CAD_TRIES and len(slept) == CAD_TRIES and len(radio.sent) == 1

    # 6. A failing radio does not stop the node: checks run, the alert stays queued, then goes out
    board, radio, _ = board_for("B-203", 0, os.path.join(tempfile.mkdtemp(), "B-203.jsonl"))

    def broken(*a):
        raise OSError("SPI transfer failed")
    radio.send, radio.poll, radio.cad = broken, broken, broken
    lines = []
    board.log = lines.append
    node = make_node("B-203", board)
    node.alert("LEAK_NIGHT_FLOW", flow=0.05)
    for _ in range(3):
        board.advance(); node.step()
    assert any(m.get("a") == "LEAK_NIGHT_FLOW" for m in node.store.queue) and board.colour == "red"
    assert not any(" sent " in line for line in lines), "nothing is logged as sent while the radio fails"
    del radio.send, radio.poll, radio.cad              # the radio works again
    board.radio.next_recover = 0.0
    node.store.next_try.clear()
    node.radio_poll()
    assert any(unpack(p, board.key).get("a") == "LEAK_NIGHT_FLOW" for p in radio.sent)
    # 7. A signed ACK sets the real clock, never the time of the readings
    board, radio, _ = board_for("B-104", 0)
    board.advance()
    radio.air = [pack({"k": "ACK", "n": "B-104", "s": 1, "gt": 5000}, sim_key("B-104"))]
    board.radio.service()
    assert len(board.receive()) == 1 and abs(board.now() - 5000) < 5 and board.sample_time() == 0

    # 8. The node reads only its own and the beacon's packets, even if its board holds more keys
    board, radio, _ = board_for("B-104", 0)
    board.keys["B-101"] = sim_key("B-101")
    node = make_node("B-104", board)
    radio.air = [pack({"k": "ACK", "n": "B-101", "s": 1, "gt": 5}, sim_key("B-101"))]
    board.radio.service()
    assert node.link.receive() == ([], 1)

    # 9. Between sensor steps the radio is polled until the next step is due
    class Polled:
        polls = 0

        def radio_poll(self):
            self.polls += 1

    board, _, _ = board_for("B-104", None)
    clock, polled = [0.0], Polled()
    board.now = lambda: clock[0]
    board.sleep = lambda s: clock.__setitem__(0, clock[0] + max(s, 0.001))
    _wait_with_radio(polled, board, 1.0)
    assert clock[0] >= 1.0 and polled.polls >= 50, polled.polls
    print("PI NODE TESTS PASSED")


if __name__ == "__main__":
    main()
