"""Unit tests for Gateway analytics: incident lifecycle, root-cause correlation,
risk scoring, maintenance priority ranking, delivery latency, and JSON snapshot export.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FIRMWARE_DIR = os.path.dirname(HERE)
for _sub in ("edge_node", "gateway", "communication"):
    _p = os.path.join(FIRMWARE_DIR, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

from aqua_edge import SUMMARY_S
from gateway import Gateway, SILENT_S
from packet import pack, sim_key


FLATS = ["B-401", "B-402", "B-301", "B-201", "B-101", "B-G01"]
TANK = "B-TANK"


class TestHAL:
    key = staticmethod(sim_key)

    def __init__(self, seen_path):
        self.t = 0
        self.inbox = []
        self.sent = []
        self.seen_path = seen_path

    def now(self):
        return self.t

    def utc_now(self):
        return (float(self.t), True)

    def receive(self):
        out = list(self.inbox)
        self.inbox.clear()
        return out

    def send(self, pkt):
        self.sent.append(pkt)


def _make_gw():
    td = tempfile.mkdtemp(prefix="gw_analytics_")
    hal = TestHAL(os.path.join(td, "seen.json"))
    gw = Gateway(hal, FLATS, TANK)
    return gw, hal, td


def _send(gw, hal, msg, rx=None):
    if rx is not None:
        hal.t = rx
    hal.inbox.append(pack(msg, sim_key(msg["n"])))
    gw.step()


def test_lifecycle_and_autoclear():
    gw, hal, _ = _make_gw()
    # 1. Quality alerts open, then auto-clear on QUALITY_CLEARED & QUALITY_TEMPORAL_CLEARED
    _send(gw, hal, {"k": "ALERT", "n": TANK, "t": 1000, "s": 1, "a": "QUALITY_CHEMICAL",
                    "ph": 5.2, "tds": 820.0, "turb": 9.1}, rx=1002)
    _send(gw, hal, {"k": "ALERT", "n": TANK, "t": 1005, "s": 2, "a": "QUALITY_LIMIT_VIOLATION",
                    "viol": 7, "ph": 5.2, "tds": 820.0, "turb": 9.1}, rx=1006)
    _send(gw, hal, {"k": "ALERT", "n": TANK, "t": 1010, "s": 3, "a": "QUALITY_TEMPORAL_ANOMALY",
                    "score": 0.94}, rx=1012)

    snap = gw.snapshot(now=1020)
    assert snap["water_quality"]["class"] == "CHEMICAL"
    assert snap["water_quality"]["compliant"] is False
    assert snap["water_quality"]["temporal_anomaly"] is True
    assert len(snap["active_incidents"]) == 1  # grouped into 1 Tank Contamination incident
    assert sorted(snap["active_incidents"][0]["symptoms"]) == [
        "QUALITY_CHEMICAL", "QUALITY_LIMIT_VIOLATION", "QUALITY_TEMPORAL_ANOMALY"
    ]

    _send(gw, hal, {"k": "ALERT", "n": TANK, "t": 1200, "s": 4, "a": "QUALITY_CLEARED"}, rx=1202)
    snap2 = gw.snapshot(now=1205)
    assert snap2["water_quality"]["class"] == "NORMAL"
    assert snap2["water_quality"]["compliant"] is True
    # Temporal anomaly still active until QUALITY_TEMPORAL_CLEARED
    assert len(snap2["active_incidents"]) == 1
    assert snap2["active_incidents"][0]["category"] == "QUALITY_TEMPORAL_ANOMALY"

    _send(gw, hal, {"k": "ALERT", "n": TANK, "t": 1210, "s": 5, "a": "QUALITY_TEMPORAL_CLEARED"}, rx=1212)
    assert gw.snapshot(now=1215)["active_incidents"] == []

    # 2. NODE_SILENT -> NODE_BACK auto-clear
    _send(gw, hal, {"k": "SUM", "n": "B-401", "t": 2000, "s": 1, "i": 2, "vol_l": 10.0}, rx=2002)
    hal.t = 2000 + SILENT_S + 60
    gw.step()
    assert any(it["category"] == "NODE_SILENT" and it["target"] == "B-401"
               for it in gw.snapshot()["active_incidents"])
    _send(gw, hal, {"k": "SUM", "n": "B-401", "t": hal.t - 1, "s": 2, "i": 3, "vol_l": 5.0}, rx=hal.t)
    assert not any(it["category"] == "NODE_SILENT" and it["target"] == "B-401"
                   for it in gw.snapshot()["active_incidents"])


def test_mass_balance_rearm_and_pipe_correlation():
    gw, hal, _ = _make_gw()
    # Two 15-min intervals with high mass-balance loss + pressure drop on floor 4 & 3
    seq = {n: 1 for n in FLATS + [TANK]}
    for blk in (1, 2):
        t = blk * SUMMARY_S
        hal.t = t + 2
        _send(gw, hal, {"k": "SUM", "n": TANK, "t": t, "s": seq[TANK], "i": blk,
                        "vol_l": 1000.0, "in_l": 0.0, "level_m": 2.0})
        seq[TANK] += 1
        for f in FLATS:
            _send(gw, hal, {"k": "SUM", "n": f, "t": t, "s": seq[f], "i": blk, "vol_l": 50.0})
            seq[f] += 1
        if blk == 1:
            _send(gw, hal, {"k": "ALERT", "n": TANK, "t": t + 15, "s": seq[TANK],
                            "a": "ABNORMAL_SUPPLY", "flow": 2.0}, rx=t + 17)
            seq[TANK] += 1

    # Pressure drop arriving while pipe_alerted is active is correlated as secondary symptom
    _send(gw, hal, {"k": "ALERT", "n": "B-401", "t": 2 * SUMMARY_S + 10, "s": seq["B-401"],
                    "a": "LEAK_PRESSURE_DROP", "p_min": 5.0}, rx=2 * SUMMARY_S + 12)
    seq["B-401"] += 1

    snap = gw.snapshot(now=2 * SUMMARY_S + 30)
    assert gw.pipe_alerted is True
    pipe_incs = [it for it in snap["active_incidents"] if it["category"] == "PIPE_LEAK"]
    assert len(pipe_incs) == 1
    assert pipe_incs[0]["target"] == "B-RISER"
    # Correlated secondary symptoms grouped into the single PIPE_LEAK incident
    assert "B-401:FLAT_LEAK" in pipe_incs[0]["symptoms"]
    assert f"{TANK}:ABNORMAL_SUPPLY" in pipe_incs[0]["symptoms"]
    assert snap["leakage"]["last_balance_pct"] == 70.0

    # Two balanced intervals re-arm pipe_alerted and clear PIPE_LEAK
    for blk in (3, 4):
        t = blk * SUMMARY_S
        hal.t = t + 2
        _send(gw, hal, {"k": "SUM", "n": TANK, "t": t, "s": seq[TANK], "i": blk,
                        "vol_l": 300.0, "in_l": 300.0, "level_m": 2.1})
        seq[TANK] += 1
        for f in FLATS:
            _send(gw, hal, {"k": "SUM", "n": f, "t": t, "s": seq[f], "i": blk, "vol_l": 50.0})
            seq[f] += 1
    assert gw.pipe_alerted is False
    assert not any(it["category"] == "PIPE_LEAK" for it in gw.snapshot()["active_incidents"])


def test_risk_ranking_and_floor_impact():
    gw, hal, _ = _make_gw()
    t = 10000
    _send(gw, hal, {"k": "ALERT", "n": TANK, "t": t, "s": 1, "a": "QUALITY_CHEMICAL",
                    "ph": 4.9, "tds": 900.0, "turb": 12.0}, rx=t + 1)
    _send(gw, hal, {"k": "ALERT", "n": "B-201", "t": t, "s": 1, "a": "LEAK_CONTINUOUS_FLOW",
                    "flow": 0.45}, rx=t + 2)
    _send(gw, hal, {"k": "ALERT", "n": "B-101", "t": t, "s": 1, "a": "ABNORMAL_CONSUMPTION",
                    "flow": 0.6}, rx=t + 3)
    gw.decide(t + 5, "PIPE_LEAK", suspected_floor="4", error_pct=35.0)
    gw.decide(t + 6, "SUPPLY_EVENT", inflow_lps=0.0)
    gw.decide(t + 7, "NODE_SILENT", node="B-G01", last_heard_min=200.0)

    queue = gw.snapshot(now=t + 600)["maintenance_queue"]
    cats = [it["category"] for it in queue]
    assert cats.index("QUALITY_CHEMICAL") < cats.index("PIPE_LEAK")
    assert cats.index("PIPE_LEAK") < cats.index("SUPPLY_EVENT")
    assert cats.index("PIPE_LEAK") < cats.index("FLAT_LEAK")
    assert cats.index("FLAT_LEAK") < cats.index("ABNORMAL_CONSUMPTION")
    assert cats.index("ABNORMAL_CONSUMPTION") < cats.index("NODE_SILENT")
    assert [it["rank"] for it in queue] == list(range(1, len(queue) + 1))

    # Population impact check: B-D4 affects more downstream flats than B-D1
    gw_d4, hal4, _ = _make_gw()
    gw_d4.decide(1000, "PIPE_LEAK", suspected_floor="4", error_pct=30.0)
    gw_d1, hal1, _ = _make_gw()
    gw_d1.decide(1000, "PIPE_LEAK", suspected_floor="1", error_pct=30.0)
    q4 = gw_d4.snapshot(now=1600)["maintenance_queue"][0]
    q1 = gw_d1.snapshot(now=1600)["maintenance_queue"][0]
    assert q4["affected_flats"] > q1["affected_flats"]
    assert q4["risk_score"] > q1["risk_score"]


def test_latency_outage_recovery_and_json_snapshot():
    gw, hal, _ = _make_gw()
    # 1 online alert (2 s delay) + 1 buffered alert recovered after 3.5h (12600 s) outage
    _send(gw, hal, {"k": "ALERT", "n": "B-401", "t": 1000, "s": 1, "a": "ABNORMAL_CONSUMPTION",
                    "flow": 0.5}, rx=1002)
    _send(gw, hal, {"k": "ALERT", "n": "B-401", "t": 5000, "s": 2, "a": "LEAK_NIGHT_FLOW",
                    "flow": 0.25}, rx=5000 + 12600)

    snap = gw.snapshot(now=17610)
    nh = snap["network_health"]
    assert nh["online_alerts"] == 1
    assert nh["offline_alerts"] == 1
    assert nh["buffered_recovered"] == 1
    assert nh["max_delay_s"] == 12600.0
    assert nh["mean_delay_s"] == 6301.0

    # Verify full snapshot is cleanly JSON-serializable
    encoded = json.dumps(snap)
    decoded = json.loads(encoded)
    assert decoded["building_status"] in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "NORMAL")
    assert set(decoded["nodes"].keys()) == set(FLATS + [TANK])


def test_review_edge_cases():
    # 1. Correlation window: old unrelated ABNORMAL_SUPPLY (>30m ago) must NOT merge into new PIPE_LEAK
    gw, hal, _ = _make_gw()
    _send(gw, hal, {"k": "ALERT", "n": "B-301", "t": 1000, "s": 1, "a": "ABNORMAL_SUPPLY", "flow": 0.0}, rx=1002)
    gw.decide(5000, "PIPE_LEAK", suspected_floor="4", error_pct=25.0)
    incs = gw.snapshot(now=5010)["active_incidents"]
    pipe_inc = [it for it in incs if it["category"] == "PIPE_LEAK"][0]
    assert "B-301:ABNORMAL_SUPPLY" not in pipe_inc["symptoms"]
    assert any(it["category"] == "ABNORMAL_SUPPLY" and it["target"] == "B-301" for it in incs)

    # 2. Auto-resolution: NODE_BACK clears NODE_SILENT without clearing FLAT_LEAK on same node
    gw2, hal2, _ = _make_gw()
    _send(gw2, hal2, {"k": "ALERT", "n": "B-401", "t": 1000, "s": 1, "a": "LEAK_CONTINUOUS_FLOW", "flow": 0.3}, rx=1002)
    hal2.t = 1000 + SILENT_S + 60
    gw2.step()
    assert any(it["category"] == "NODE_SILENT" and it["target"] == "B-401" for it in gw2.snapshot()["active_incidents"])
    _send(gw2, hal2, {"k": "SUM", "n": "B-401", "t": hal2.t - 1, "s": 2, "i": 1, "vol_l": 5.0}, rx=hal2.t)
    active2 = gw2.snapshot()["active_incidents"]
    assert not any(it["category"] == "NODE_SILENT" and it["target"] == "B-401" for it in active2)
    assert any(it["category"] == "FLAT_LEAK" and it["target"] == "B-401" for it in active2)

    # 3. Risk calibration: long-lived low-severity events cannot outrank contamination
    gw3, hal3, _ = _make_gw()
    gw3.decide(1000, "NODE_SILENT", node=TANK, last_heard_min=1440.0)
    _send(gw3, hal3, {"k": "ALERT", "n": "B-101", "t": 1000, "s": 1, "a": "ABNORMAL_CONSUMPTION", "flow": 1.0}, rx=1002)
    _send(gw3, hal3, {"k": "ALERT", "n": TANK, "t": 86400, "s": 1, "a": "QUALITY_TEMPORAL_ANOMALY", "score": 0.9}, rx=86402)
    q3 = gw3.snapshot(now=87400)["maintenance_queue"]
    cats3 = [it["category"] for it in q3]
    assert cats3.index("QUALITY_TEMPORAL_ANOMALY") < cats3.index("ABNORMAL_CONSUMPTION")
    assert cats3.index("QUALITY_TEMPORAL_ANOMALY") < cats3.index("NODE_SILENT")

    # 4. Incident history: resolved incidents remain in incident_history after leaving active queue
    _send(gw3, hal3, {"k": "ALERT", "n": TANK, "t": 87500, "s": 2, "a": "QUALITY_TEMPORAL_CLEARED"}, rx=87502)
    snap3 = gw3.snapshot(now=87505)
    assert not any(it["category"] == "QUALITY_TEMPORAL_ANOMALY" for it in snap3["active_incidents"])
    assert any(it["category"] == "QUALITY_TEMPORAL_ANOMALY" and it["resolution_reason"] == "QUALITY_TEMPORAL_CLEARED"
               for it in snap3["incident_history"])

    # 5. Delivery latency: negative rx - t (clock skew) must NOT pollute delays_s or online_alerts
    gw4, hal4, _ = _make_gw()
    _send(gw4, hal4, {"k": "ALERT", "n": "B-401", "t": 5000, "s": 1, "a": "ABNORMAL_CONSUMPTION", "flow": 0.4}, rx=4000)
    nh4 = gw4.snapshot(now=5010)["network_health"]
    assert nh4["skewed_packets"] == 1
    assert nh4["online_alerts"] == 0
    assert nh4["mean_delay_s"] == 0.0
    assert nh4["max_delay_s"] == 0.0

    # 6. Empty snapshot JSON-serializable & non-mutating
    gw_empty, _, _ = _make_gw()
    s_a = gw_empty.snapshot()
    s_b = gw_empty.snapshot()
    assert s_a == s_b
    assert s_a["building_status"] == "NORMAL"
    assert s_a["incident_history"] == []
    assert json.loads(json.dumps(s_a)) == s_a


class _Node:
    """Numbers and sends messages the way real nodes do."""

    def __init__(self, gw, hal):
        self.gw, self.hal, self.seq = gw, hal, {}

    def __call__(self, node, t, kind="ALERT", rx=None, **fields):
        self.seq[node] = self.seq.get(node, 0) + 1
        msg = {"k": kind, "n": node, "s": self.seq[node], "t": t, **fields}
        _send(self.gw, self.hal, msg, rx=t + 1 if rx is None else rx)
        return msg

    def block(self, i, flat_litres, rx=None):
        """All summaries of block i: the tank lets out 600 L, each flat uses flat_litres."""
        t = (i + 1) * SUMMARY_S
        for n in [TANK] + FLATS:
            self(n, t, "SUM", rx=rx, i=i, vol_l=600.0 if n == TANK else flat_litres)


TIGHT, LEAK = 100.0, 50.0          # litres per flat: all water accounted for / half of it missing


def _open(gw):
    """Open problems, leaving out NODE_SILENT (these short tests leave most nodes unheard)."""
    return {key for key in gw.analytics.incidents if key[1] != "NODE_SILENT"}


def test_recovery_clears_only_what_is_older():
    gw, hal, _ = _make_gw()
    send = _Node(gw, hal)
    cases = [("B-401", "LEAK_CONTINUOUS_FLOW", "FLAT_LEAK", "FLAT_LEAK_CLEARED"),
             ("B-301", "ABNORMAL_CONSUMPTION", "ABNORMAL_CONSUMPTION", "CONSUMPTION_CLEARED"),
             ("B-201", "PROLONGED_ZERO_FLOW", "PROLONGED_ZERO_FLOW", "ZERO_FLOW_CLEARED"),
             (TANK, "ABNORMAL_SUPPLY", "ABNORMAL_SUPPLY", "SUPPLY_FLOW_CLEARED")]
    for node, alert, _, _ in cases:
        send(node, 1000, a=alert)
    assert _open(gw) == {(node, cat) for node, _, cat, _ in cases}
    for node, _, cat, cleared in cases:
        send(node, 2000, a=cleared)
        assert (node, cat) not in _open(gw), cleared
    assert len(gw.snapshot()["incident_history"]) == 4
    assert not any(e.get("alert", "").endswith("_CLEARED") for e in gw.events)
    # an alert from before the recovery, delivered late, does not reopen the problem
    send("B-401", 1500, rx=3000, a="LEAK_NIGHT_FLOW")
    assert _open(gw) == set() and gw.flat_leak_t == 1000
    # a newer alert does, and an older recovery does not end it
    send("B-401", 4000, a="LEAK_NIGHT_FLOW")
    send("B-401", 3500, rx=4100, a="FLAT_LEAK_CLEARED")
    assert _open(gw) == {("B-401", "FLAT_LEAK")}


def test_supply_recovery_and_clock_ok():
    gw, hal, _ = _make_gw()
    send = _Node(gw, hal)
    send(TANK, 1000, "FLAG", f="SUPPLY_LOW", level=0.4, inflow=0.0)
    assert gw.snapshot()["supply"]["state"] == "SUPPLY_EVENT"
    send(TANK, 2000, "FLAG", f="SUPPLY_RECOVERED", level=1.8, inflow=0.4)
    assert gw.snapshot()["supply"]["state"] == "NORMAL" and _open(gw) == set()
    send(TANK, 1500, "FLAG", rx=2500, f="SUPPLY_LOW", level=0.4, inflow=0.0)    # old flag, late
    assert gw.snapshot()["supply"]["state"] == "NORMAL" and _open(gw) == set()
    # low and recovered arriving together after an outage: still recorded, and closed
    hal.t = 9000
    for s, (t, flag) in enumerate([(5000, "SUPPLY_LOW"), (6000, "SUPPLY_RECOVERED")], 100):
        hal.inbox.append(pack({"k": "FLAG", "n": TANK, "s": s, "t": t, "f": flag, "inflow": 0.0}, sim_key(TANK)))
    gw.step()
    supply = [e["event"] for e in gw.events if e["event"].startswith("SUPPLY")]
    assert supply == ["SUPPLY_EVENT", "SUPPLY_RECOVERED"] * 2, gw.events
    assert _open(gw) == set()
    # clock: an old stored message proves nothing; a fresh on-time one clears the skew
    send("B-101", 10300, "SUM", rx=10000, i=11, vol_l=1.0)
    send("B-101", 9000, "SUM", rx=10100, i=9, vol_l=1.0)
    assert "B-101" in gw.skewed
    send("B-101", 10200, "SUM", rx=10201, i=10, vol_l=1.0)
    assert gw.skewed == set() and ("B-101", "CLOCK_SKEW") not in _open(gw)


def test_replay_is_no_sign_of_life():
    gw, hal, _ = _make_gw()
    old = pack(_Node(gw, hal)("B-401", 100, "SUM", i=0, vol_l=1.0), sim_key("B-401"))
    sent = len(hal.sent)
    for t in range(600, SILENT_S + 900, 600):
        hal.t, hal.inbox = t, [old]
        gw.step()
    assert "B-401" in gw.silent and len(gw.log) == 1
    assert len(hal.sent) > sent, "a repeat is still acknowledged: it may be a real retry"


def _pipe_events(gw):
    return [(e["event"], e["interval"]) for e in gw.events if e["event"].startswith("PIPE_LEAK")]


def test_pipe_blocks_must_be_adjacent():
    gw, hal, _ = _make_gw()
    send = _Node(gw, hal)
    send.block(1, LEAK)
    send.block(40, LEAK)                                    # far apart: two single blocks
    assert _pipe_events(gw) == []
    send.block(41, LEAK)
    assert _pipe_events(gw) == [("PIPE_LEAK", 41)] and gw.pipe_since == 40 * SUMMARY_S
    send.block(43, TIGHT)
    send.block(45, TIGHT)                                   # not next to each other: no re-arm
    assert gw.pipe_alerted
    send.block(46, TIGHT)
    assert not gw.pipe_alerted
    # an earlier leak that only becomes known now: reported once, current state untouched
    for i in (20, 21, 22):
        send.block(i, LEAK, rx=47 * SUMMARY_S + i)
    assert _pipe_events(gw)[1:] == [("PIPE_LEAK_PAST", 21)] and not gw.pipe_alerted

    # newest blocks first, then the earlier ones of the same leak: one alert, earlier start
    gw, hal, _ = _make_gw()
    send = _Node(gw, hal)
    for i in (7, 8):
        send.block(i, LEAK)
    for i in (5, 6):
        send.block(i, LEAK, rx=10 * SUMMARY_S + i)
    assert _pipe_events(gw) == [("PIPE_LEAK", 8)] and gw.pipe_since == 5 * SUMMARY_S
    # late tight blocks from before the leak do not re-arm the active alert
    for i in (1, 2):
        send.block(i, TIGHT, rx=11 * SUMMARY_S + i)
    assert gw.pipe_alerted


def test_possible_cause_follows_newest_alert():
    gw, hal, _ = _make_gw()
    send = _Node(gw, hal)
    label = lambda: gw.analytics.incidents[("B-401", "FLAT_LEAK")]["evidence"].get("possible_cause")
    send.block(1, LEAK)
    send.block(2, LEAK)
    send("B-401", 3 * SUMMARY_S + 20, a="LEAK_PRESSURE_DROP")
    assert label() == "PIPE_LEAK"
    send.block(3, TIGHT)
    send.block(4, TIGHT)
    send("B-401", 6 * SUMMARY_S, a="LEAK_PRESSURE_DROP")
    assert label() is None


def test_restart_restores_state():
    gw, hal, td = _make_gw()
    hal.storage_path = os.path.join(td, "GATEWAY.jsonl")
    gw = Gateway(hal, FLATS, TANK)
    send = _Node(gw, hal)
    send.block(1, LEAK)
    leak = send("B-401", 2 * SUMMARY_S + 20, a="LEAK_NIGHT_FLOW", flow=0.1)
    queue = gw.snapshot(now=3000)["maintenance_queue"]
    hal.t = 20000                                           # gateway was off for hours
    send.gw = gw = Gateway(hal, FLATS, TANK)
    assert gw.snapshot(now=3000)["maintenance_queue"] == queue
    gw.step()
    assert gw.silent == {} and gw.events == [], "its own downtime must not make nodes silent"
    _send(gw, hal, leak)                                    # same packet again: ignored
    assert gw.log == [] and gw.events == []
    send.block(2, LEAK, rx=20001)                           # the leaking pair spans the restart
    assert _pipe_events(gw) == [("PIPE_LEAK", 2)]
    send.gw = gw = Gateway(hal, FLATS, TANK)
    assert gw.pipe_alerted and gw.pipe_since == SUMMARY_S
    send("B-401", 20100, a="LEAK_NIGHT_FLOW")
    assert gw.events[-1]["event"] == "REPEAT_ALERT" and gw.events[-1]["count"] == 2
    send("B-401", 20200, a="FLAT_LEAK_CLEARED")
    assert ("B-401", "FLAT_LEAK") not in _open(gw)

    # power cut during the save: no acknowledgement, so the node still has the message
    msg = {"k": "ALERT", "n": "B-301", "s": 900, "t": 20300, "a": "LEAK_NIGHT_FLOW"}
    acked, real_replace = len(hal.sent), os.replace

    def power_cut(*_):
        raise OSError("power cut")

    os.replace = power_cut
    try:
        _send(gw, hal, msg, rx=20301)
        raise AssertionError("the save should have failed")
    except OSError:
        pass
    finally:
        os.replace = real_replace
    assert len(hal.sent) == acked
    gw = Gateway(hal, FLATS, TANK)
    _send(gw, hal, msg, rx=20400)                           # the node sends it again
    assert ("B-301", "FLAT_LEAK") in _open(gw) and len(hal.sent) == acked + 1


def test_old_seen_file_is_still_read():
    gw, hal, td = _make_gw()
    with open(os.path.join(td, "GATEWAY_seen.json"), "w") as f:
        json.dump({"B-401": {"floor": 5, "above": [8]}}, f)
    hal.storage_path = os.path.join(td, "GATEWAY.jsonl")
    gw = Gateway(hal, FLATS, TANK)
    assert gw._is_seen_and_record("B-401", 8) and not gw._is_seen_and_record("B-401", 6)


def main():
    test_lifecycle_and_autoclear()
    test_mass_balance_rearm_and_pipe_correlation()
    test_risk_ranking_and_floor_impact()
    test_latency_outage_recovery_and_json_snapshot()
    test_review_edge_cases()
    test_recovery_clears_only_what_is_older()
    test_supply_recovery_and_clock_ok()
    test_replay_is_no_sign_of_life()
    test_pipe_blocks_must_be_adjacent()
    test_possible_cause_follows_newest_alert()
    test_restart_restores_state()
    test_old_seen_file_is_still_read()
    print("GATEWAY ANALYTICS TESTS PASSED")


if __name__ == "__main__":
    main()
