"""Pre-experiment audit & fix tests: simulated-time summary aggregation & unmetered_homes mass balance.

    python3 -m tests.test_pre_experiment_audit
"""
import csv
import tempfile

from edge.aqua_edge import FlatNode, SUMMARY_S
from edge.board import NodeBoard
from edge.edge_based import prime, run
from edge.sensors import DATASET, sensor_reader
from gateway.core import Gateway
from gateway.topology import Topology
from hal.packet import pack, sim_key


class FakeRadio:
    broken = False

    def __init__(self):
        self.sent = []
        self.inbox = []

    def send(self, raw, cad=False):
        self.sent.append((raw, cad))
        return True

    def receive(self):
        got, self.inbox = self.inbox, []
        return got


def _replay_24h(node_id, step_s, wall_start, include_boundary=False):
    with tempfile.TemporaryDirectory() as tmp:
        radio = FakeRadio()
        keys = {node_id: sim_key(node_id), "GATEWAY": sim_key("GATEWAY")}
        wall = [wall_start]
        mono = [100.0]
        base_reader = sensor_reader(node_id)
        extra = [(172800.0, {"flow_lps": 0.0, "pressure_m": 10.0})] if include_boundary else []

        def reader():
            s = base_reader()
            if s is not None:
                return s
            return extra.pop(0) if extra else None

        board = NodeBoard(node_id, radio, reader, f"{tmp}/{node_id}.jsonl", keys, step_s=step_s, gpio=False)
        board.now = lambda: wall[0] + board.clock_fix
        board.monotonic = lambda: mono[0]

        def fake_sleep(dt):
            wall[0] += dt
            mono[0] += dt

        board.sleep = fake_sleep
        node = FlatNode(node_id, board)
        first_taken = prime(node, board, 86400)
        assert first_taken is True and board.sample_time() == 86400.0
        assert radio.sent == [] and node.store.queue == [] and not node.valve["closed"]

        run(node, board, step_s=step_s, poll_s=5.0 if step_s >= 60 else 0.5, first_taken=first_taken)
        sums = [m for m in node.store.queue if m["k"] == "SUM"]
        alerts = [m["a"] for m in node.store.queue if m["k"] == "ALERT"]
        return sums, alerts, wall[0] - wall_start


def test_part1_simulated_time_summaries_both_speeds():
    # 1. --step-s 1 and --step-s 60 produce identical aligned 15-min SUM blocks (95 complete in 86400..172740,
    #    96 when boundary row t=172800 is present), even with staggered wall-clock start times (+37s)
    sums_1s_b203, alerts_1s, elapsed_1s = _replay_24h("B-203", step_s=1.0, wall_start=1_700_000_000.0)
    sums_60s_b203, alerts_60s, elapsed_60s = _replay_24h("B-203", step_s=60.0, wall_start=1_700_000_037.0)
    sums_1s_b101, _, _ = _replay_24h("B-101", step_s=1.0, wall_start=1_700_000_041.0)
    sums_boundary, _, _ = _replay_24h("B-203", step_s=1.0, wall_start=1_700_000_000.0, include_boundary=True)

    assert len(sums_1s_b203) == 95 and [m["i"] for m in sums_1s_b203] == list(range(96, 191))
    assert len(sums_60s_b203) == 95 and [m["i"] for m in sums_60s_b203] == list(range(96, 191))
    assert len(sums_1s_b101) == 95 and [m["i"] for m in sums_1s_b101] == list(range(96, 191))
    assert len(sums_boundary) == 96 and [m["i"] for m in sums_boundary] == list(range(96, 192))

    # Volumes match identically between --step-s 1 and --step-s 60, while wall-clock metadata reflects speed
    assert [m["vol_l"] for m in sums_1s_b203] == [m["vol_l"] for m in sums_60s_b203]
    assert 1400.0 <= elapsed_1s <= 1500.0
    assert 86000.0 <= elapsed_60s <= 87000.0
    assert "LEAK_NIGHT_FLOW" in alerts_1s and "LEAK_NIGHT_FLOW" in alerts_60s
    assert sums_1s_b203[0]["t"] >= 1_700_000_000.0, "packet metadata t remains wall-clock time"

    # 2. Partial final interval + restart: 5 rows in block 0, save/restart, 10 more rows + boundary -> exact 15-min sum
    with tempfile.TemporaryDirectory() as tmp:
        rows = [(m * 60.0, {"flow_lps": 0.1, "pressure_m": 10.0}) for m in range(16)]
        idx = [0]

        def part_reader():
            if idx[0] >= len(rows):
                return None
            r = rows[idx[0]]
            idx[0] += 1
            return r

        keys = {"B-102": sim_key("B-102"), "GATEWAY": sim_key("GATEWAY")}
        board1 = NodeBoard("B-102", FakeRadio(), part_reader, f"{tmp}/b102.jsonl", keys, step_s=1.0, gpio=False)
        node1 = FlatNode("B-102", board1)
        for _ in range(5):
            assert board1.advance()
            node1.step()
        node1.save_state()

        board2 = NodeBoard("B-102", FakeRadio(), part_reader, f"{tmp}/b102.jsonl", keys, step_s=1.0, gpio=False)
        node2 = FlatNode("B-102", board2)
        while board2.advance():
            node2.step()
        part_sums = [m for m in node2.store.queue if m["k"] == "SUM"]
        assert len(part_sums) == 1 and part_sums[0]["i"] == 0
        assert abs(part_sums[0]["vol_l"] - (15 * 0.1 * 60.0)) < 1e-3, f"expected 90.0 L, got {part_sums[0]['vol_l']}"
    print("ok: part 1 (95/96 aligned SUMs at step-s=1 & step-s=60, staggered starts, partial interval)")


def _make_gw(tree):
    class GwHal:
        storage_path = None
        key = staticmethod(sim_key)

        def __init__(self):
            self.t = 1_700_000_000.0
            self.inbox = []
            self.sent = []

        def now(self):
            return self.t

        def receive(self):
            got, self.inbox = self.inbox, []
            return got

        def send(self, raw, cad=False):
            self.sent.append(raw)
            return True

    return Gateway(GwHal(), tree), GwHal


def _send_sum(gw, node, seq, block, vol_l, p_min=10.0, p_mean=10.5):
    msg = {"k": "SUM", "n": node, "s": seq, "t": gw.hal.t, "i": block, "vol_l": vol_l, "p_min": p_min, "p_mean": p_mean}
    gw.hal.inbox.append(pack(msg, sim_key(node)))


def test_part2_unmetered_homes_and_mass_balance():
    # 1. Verify sensors.csv unmetered Day-1 variation is too high for naive baseline subtraction
    with open(DATASET, newline="") as f:
        r = csv.reader(f)
        hdr = next(r)
        idx = {name: i for i, name in enumerate(hdr)}
        d1_unmet = [0.0] * 96
        for row in r:
            t = float(row[0])
            if t >= 86400:
                break
            b = int(t // SUMMARY_S)
            sup = float(row[idx["B-TANK:outflow_lps"]]) * 60.0
            met = sum(float(row[idx[f"{n}:flow_lps"]]) * 60.0 for n in ("B-203", "B-101", "B-102"))
            d1_unmet[b] += sup - met
    assert max(d1_unmet) - min(d1_unmet) > 200.0, "Day-1 unmetered variation is large (>200 L/15min)"

    t_unmetered = Topology.load("gateway/physical_4node.json")
    t_metered = Topology({
        **t_unmetered.data,
        "root": {**t_unmetered.data["root"], "unmetered_homes": False},
    })

    # 2. Normal operation with unmetered_homes=true -> no false PIPE_LEAK, mass balance explicitly unavailable
    gw_u, _ = _make_gw(t_unmetered)
    for b in range(1, 5):
        _send_sum(gw_u, "B-TANK", b, b, 1000.0)
        _send_sum(gw_u, "B-203", b, b, 50.0)
        _send_sum(gw_u, "B-101", b, b, 50.0)
        _send_sum(gw_u, "B-102", b, b, 50.0)
        gw_u.step()
    assert not gw_u.pipe_alerted and gw_u.analytics.last_balance_pct is None

    # 3. Fully metered 4-node topology (unmetered_homes=false):
    #    - Normal variation across flats -> no PIPE_LEAK
    #    - Missing telemetry -> block held, not zeroed
    #    - Genuine 2-block riser leak -> PIPE_LEAK with localisation + isolation_valve
    #    - 2 normal blocks -> clears & re-arms
    gw_m, _ = _make_gw(t_metered)
    for b, (v1, v2, v3) in ((1, (100.0, 100.0, 100.0)), (2, (50.0, 180.0, 70.0))):
        _send_sum(gw_m, "B-TANK", b, b, 300.0, p_min=12.0, p_mean=12.5)
        _send_sum(gw_m, "B-203", b, b, v1, p_min=12.0, p_mean=12.5)
        _send_sum(gw_m, "B-101", b, b, v2, p_min=12.0, p_mean=12.5)
        _send_sum(gw_m, "B-102", b, b, v3, p_min=12.0, p_mean=12.5)
        gw_m.step()
    assert not gw_m.pipe_alerted

    # Missing B-102 in block 3
    _send_sum(gw_m, "B-TANK", 3, 3, 300.0)
    _send_sum(gw_m, "B-203", 3, 3, 100.0)
    _send_sum(gw_m, "B-101", 3, 3, 100.0)
    gw_m.step()
    assert 3 not in gw_m.verdicts and not gw_m.pipe_alerted
    _send_sum(gw_m, "B-102", 3, 3, 100.0, p_min=12.0, p_mean=12.5)
    gw_m.step()
    assert 3 in gw_m.verdicts and not gw_m.pipe_alerted

    # Injected riser leak in blocks 4 and 5 (pressure drop on floor "1": B-101, B-102)
    for b in (4, 5):
        _send_sum(gw_m, "B-TANK", b, b, 450.0, p_min=12.0, p_mean=12.5)
        _send_sum(gw_m, "B-203", b, b, 100.0, p_min=11.9, p_mean=12.4)
        _send_sum(gw_m, "B-101", b, b, 100.0, p_min=10.5, p_mean=11.0)
        _send_sum(gw_m, "B-102", b, b, 100.0, p_min=10.4, p_mean=10.9)
        gw_m.step()
    assert gw_m.pipe_alerted is True
    leak_ev = next(e for e in gw_m.events if e["event"] == "PIPE_LEAK")
    assert leak_ev["suspected_floor"] == "1"
    assert leak_ev["isolation_valve"] == "B-TANK"

    # 2 normal blocks clear & re-arm
    for b in (6, 7):
        _send_sum(gw_m, "B-TANK", b, b, 300.0, p_min=12.0, p_mean=12.5)
        _send_sum(gw_m, "B-203", b, b, 100.0, p_min=12.0, p_mean=12.5)
        _send_sum(gw_m, "B-101", b, b, 100.0, p_min=12.0, p_mean=12.5)
        _send_sum(gw_m, "B-102", b, b, 100.0, p_min=12.0, p_mean=12.5)
        gw_m.step()
    assert gw_m.pipe_alerted is False
    print("ok: part 2 (unmetered_homes safe bypass + fully metered mass-balance & localisation)")


def main():
    test_part1_simulated_time_summaries_both_speeds()
    test_part2_unmetered_homes_and_mass_balance()
    print("PRE-EXPERIMENT FIXES VALIDATED")


if __name__ == "__main__":
    main()
