"""The cloud-based control case (planning/baselines.md, Baseline A) against the edge-based solution,
on the same data, clock and radio (tests/cloud_world.py); then what differs when the deciding is
done elsewhere: a lost link, lost packets, restarts, a failing valve and reopening. At the end,
the real programs with the real dashboard server on this computer.

    python3 -m tests.test_cloud_baseline
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from edge import cloud_based
from edge.aqua_edge import STALE_S, TRIAL_QUIET
from edge.board import NodeBoard
from edge.cloud_based import SampleStore, ThinNode
from edge.edge_based import make_node
from gateway.core import REOPEN_ORDER_S
from gateway.topology import Topology
from hal.packet import LAMPS, SET_CLOSE, SET_OPEN_ORDER, pack, sim_key, unpack
from tests.cloud_world import PUMP_S, START, World
from tests.test_dashboard_link import free_port, wait
from tests.test_edge_node import free_udp_port
from tests.test_gateway_edge import GATEWAY_TOKEN, ROOT, Dashboard

LEAK = "B-203"                      # the flat with the planted night leak
SOME = ["B-TANK", "B-101", "B-102", "B-201", LEAK]


def folder():
    return tempfile.mkdtemp(prefix="aqua-cloud-")


def lamp(world, name):
    board = world.boards[name]
    return board.colour, board.actuators.blinks


def until(world, test, limit=1500):
    for _ in range(limit):
        if test():
            return
        assert world.minute(), "the data ran out"
    raise AssertionError("it never happened")


def leak_closes_at():
    """When (minute of the live day) the edge-based node shuts the leaking flat's valve."""
    world = World("edge", folder(), [LEAK])
    until(world, lambda: world.nodes[LEAK].valve["closed"])
    return world.moves(LEAK)[0][0] // 60


CLOSES = None


# --- the control and the solution, side by side ---
def check_thin_node_has_no_detectors():
    code = ("import sys, edge.cloud_based; "
            "print([m for m in sys.modules if m.split('.')[0] in ('detection', 'numpy') "
            "or m in ('edge.aqua_edge', 'edge.edge_based', 'gateway.core')])")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]", out
    assert cloud_based.ORDER_MAX_AGE_S == REOPEN_ORDER_S


def same_but_later(edge, cloud):
    """Lists of (..., time): the same, except that cloud's may be up to PUMP_S later."""
    assert [x[:-1] for x in edge] == [x[:-1] for x in cloud], (edge, cloud)
    late = [c[-1] - e[-1] for e, c in zip(edge, cloud)]
    assert all(0 <= d <= PUMP_S for d in late), late


def compare(names, csv=None):
    """Both solutions on the same data with a perfect link: the same alerts, decisions, valve moves
    and lights at the same times, and the same snapshot. Two expected differences. In the minute a
    valve closes, an edge-based node leaves that reading out of its 15-minute summary, and the
    server counts it, as it was measured. A valve move the gateway orders takes one more trip, so
    it may come one look at the radio (PUMP_S) later. -> (edge world, cloud world)"""
    worlds = {}
    for mode in ("edge", "cloud"):
        worlds[mode] = World(mode, folder(), names, **({"csv": csv} if csv else {}))
        worlds[mode].run()
    edge, cloud = worlds["edge"], worlds["cloud"]
    reports = lambda w: [m for m in w.messages() if m[3].startswith("VALVE_")]
    others = lambda w: [m for m in w.messages() if not m[3].startswith("VALVE_")]
    assert others(cloud) == others(edge), sorted(set(others(cloud)) ^ set(others(edge)))
    same_but_later(sorted((n, c, at) for n, at, _, c in reports(edge)), sorted((n, c, at) for n, at, _, c in reports(cloud)))
    decided = lambda w, valve: [(e, n, a, at) for at, e, n, a in w.decisions() if e.startswith("VALVE_") == valve]
    assert decided(cloud, False) == decided(edge, False), sorted(set(decided(cloud, False)) ^ set(decided(edge, False)))
    same_but_later(sorted(decided(edge, True)), sorted(decided(cloud, True)))
    same_but_later(sorted((n, s, at) for at, n, s in edge.valves), sorted((n, s, at) for at, n, s in cloud.valves))
    assert cloud.lights == edge.lights, {n: (edge.lights[n], cloud.lights[n]) for n in edge.lights
                                         if edge.lights[n] != cloud.lights[n]}
    se, sc = edge.sums(), cloud.sums()
    assert set(se) == set(sc)
    closing = {(name, (START + at) // 900) for at, name, state in edge.valves}
    differ = {k for k in se if any(abs(se[k].get(f, 0) - sc[k].get(f, 0)) > 2e-4 for f in set(se[k]) | set(sc[k]))}
    assert differ <= closing, differ - closing
    a, b = edge.gateway.snapshot(), cloud.gateway.snapshot()
    assert a.keys() == b.keys()
    # network_health holds message delays, and valves the times of the reports above
    assert [k for k in a if a[k] != b[k] and k not in ("network_health", "valves", "nodes")] == []
    states = lambda s: {n: (v.get("light"), v.get("last_sum", {}).get("level_m")) for n, v in s["nodes"].items()}
    assert states(a) == states(b)
    assert cloud.host.refused == 0, "live traffic fits the radio's limit"
    return edge, cloud


def check_same_results_as_edge():
    edge, cloud = compare(SOME)
    names = {code for _, _, _, code in edge.messages()}
    assert any(n.startswith("LEAK_") for n in names) and any(n.startswith("QUALITY_") for n in names), names
    assert any(kind == "SUPPLY_EVENT" for _, kind, _, _ in edge.decisions()), "a cross-node case too"
    assert edge.valves and "VALVE_CLOSED" in names
    sent = lambda w: sum(size for (who, _), (_, size) in w.air.traffic.items() if who != "GATEWAY")
    assert sent(cloud) > 10 * sent(edge)


def pipe_leak_data():
    """A day to learn from, then six hours in which the tank gives out half as much again as the
    two flats use, with the pressure falling on one of them."""
    path = os.path.join(folder(), "pipe.csv")
    columns = ["flow_lps", "pressure_m"]
    tank = ["level_m", "inflow_lps", "outflow_lps", "ph", "tds_ppm", "turbidity_ntu", "temp_c"]
    with open(path, "w") as f:
        f.write(",".join(["t_s"] + [f"{n}:{c}" for n in ("B-101", "B-102") for c in columns]
                         + [f"B-TANK:{c}" for c in tank]) + "\n")
        for minute in range(1440 + 360):
            leaking = minute >= 1440 + 60
            flows = [0.05 if (minute + 7 * i) % 20 < 10 else 0.0 for i in range(2)]
            row = [60 * minute]
            for i, flow in enumerate(flows):
                row += [flow, 9.2 if leaking and i == 0 else 10.0 + (minute % 3) / 100]
            out = round(sum(flows) * (1.5 if leaking else 1.0), 5)
            row += [1.5, out, out, 7.2, 300.0, 1.0, 25.0]
            f.write(",".join(map(str, row)) + "\n")
    return path


def check_same_results_for_a_pipe_leak():
    edge, cloud = compare(["B-101", "B-102"], pipe_leak_data())
    kinds = [kind for _, kind, _, _ in edge.decisions()]
    assert "PIPE_LEAK" in kinds and "VALVE_CLOSED" in kinds, kinds
    assert edge.valves and cloud.gateway.command_number and not cloud.gateway.outbox, "the gateway's orders arrived"


# --- the link ---
def check_no_link_no_decisions():
    world = World("cloud", folder(), [LEAK])
    node, brain = world.nodes[LEAK], world.host.devices[LEAK].brain
    until(world, lambda: world.t - START >= (CLOSES - 45) * 60)
    world.air.cut.add(LEAK)
    for _ in range(90):
        world.minute()
    assert not node.shut and not world.moves(LEAK), "with no server the node closes nothing"
    assert lamp(world, LEAK) == ("yellow", 1) and len(node.store.queue) == 90
    assert not brain.valve["closed"]
    world.air.cut.clear()
    until(world, lambda: node.shut, 30)
    closed_late = world.moves(LEAK)[0][0] // 60
    assert closed_late >= CLOSES + 40, (closed_late, CLOSES)
    until(world, lambda: not node.store.queue or len(node.store.queue) <= 1, 30)
    for _ in range(3):
        world.minute()
    leak = [(t // 60, code) for n, t, _, code in world.messages("ALERT") if n == LEAK and code.startswith("LEAK_")]
    assert leak and leak[0][0] == CLOSES, f"found late, dated when it happened: {leak} {CLOSES}"
    health = world.gateway.snapshot()["network_health"]
    assert health["offline_alerts"] >= 1 and health["bad_packets"] == 0
    assert lamp(world, LEAK)[0] == "red"

    edge = World("edge", folder(), [LEAK])
    until(edge, lambda: edge.t - START >= (CLOSES - 45) * 60)
    edge.air.cut.add(LEAK)
    for _ in range(90):
        edge.minute()
    assert edge.nodes[LEAK].shut and edge.moves(LEAK)[0][0] // 60 == CLOSES, "the edge node needs no link"


def check_lost_packets():
    clean = World("cloud", folder(), [LEAK, "B-TANK"])
    lossy = World("cloud", folder(), [LEAK, "B-TANK"])
    import random
    rng = random.Random(7)
    lossy.air.lose = lambda direction, header: header["k"] != "BEACON" and rng.random() < 0.3
    clean.run(700)
    lossy.run(700)
    lossy.air.lose = None
    for _ in range(10):
        clean.minute(read=False)
        lossy.minute(read=False)
    codes = lambda w: [(n, t, c) for n, t, _, c in w.messages("ALERT", "FLAG") if not c.startswith("VALVE_")]
    assert codes(lossy) == codes(clean), set(codes(lossy)) ^ set(codes(clean))
    for name in lossy.nodes:
        assert [s for _, s in lossy.moves(name)] == [s for _, s in clean.moves(name)], name
    assert clean.moves(LEAK)
    assert len(lossy.log) == len({(m["n"], m["s"]) for m in lossy.log}), "nothing counted twice"
    for name, node in lossy.nodes.items():
        assert not node.store.queue and node.shut == clean.nodes[name].shut


# --- restarts and lost state ---
def check_restarts_change_nothing():
    whole = World("cloud", folder(), [LEAK])
    broken = World("cloud", folder(), [LEAK])
    whole.run(CLOSES + 60)
    broken.run(CLOSES - 10)                 # the leak's run of readings is half counted
    broken.start_gateway()                  # the server dies and starts again
    broken.run(40)
    assert broken.nodes[LEAK].shut
    broken.start_node(LEAK)                 # and the node: its valve stays shut
    assert broken.nodes[LEAK].shut
    broken.start_gateway()
    broken.run(30)
    assert broken.messages() == whole.messages(), set(broken.messages()) ^ set(whole.messages())
    assert broken.moves(LEAK) and all(state == "closed" for _, state in broken.moves(LEAK)), "no restart opened it"


def check_server_that_lost_everything_opens_nothing():
    root = folder()
    world = World("cloud", root, [LEAK])
    until(world, lambda: world.nodes[LEAK].shut)
    for _ in range(3):
        world.minute()
    shutil.rmtree(os.path.join(root, "brains"))
    os.remove(os.path.join(root, "gateway_state.json"))
    world.start_gateway()
    for _ in range(20):
        world.minute()
    brain = world.host.devices[LEAK].brain
    assert world.nodes[LEAK].shut and not brain.valve["closed"] and brain.shut, "it knows the pipe is shut"
    world.gateway.order(LEAK, "VALVE_OPEN")         # a person can still open it
    for _ in range(3):
        world.minute()
    assert not world.nodes[LEAK].shut


# --- the valve and orders ---
def check_operator_orders_and_old_ones():
    world = World("cloud", folder(), [LEAK])
    node = world.nodes[LEAK]
    until(world, lambda: node.shut)
    world.minute()
    assert world.gateway.analytics.valves[LEAK]["state"] == "closed"

    world.gateway.order(LEAK, "VALVE_OPEN")
    for _ in range(3):
        world.minute()
    assert not node.shut and world.gateway.analytics.valves[LEAK]["state"] == "open" and not world.gateway.outbox
    assert lamp(world, LEAK)[0] == "red", "the leak alert still stands"

    world.gateway.order(LEAK, "VALVE_CLOSE")
    for _ in range(3):
        world.minute()
    assert node.shut
    world.air.cut.add(LEAK)
    for _ in range(3):                               # the server notices after 90 s
        world.minute()
    world.gateway.order(LEAK, "VALVE_OPEN")
    for _ in range(REOPEN_ORDER_S // 60 + 3):
        world.minute()
    assert any(e["event"] == "ORDER_EXPIRED" and e["node"] == LEAK for e in world.events) and not world.gateway.outbox
    world.air.cut.clear()
    for _ in range(30):
        world.minute()
    assert node.shut, "an order that expired while the node was away is not carried out later"

    # the node's own guard, for a SET that was waiting: an old order to open is not carried out
    saved = node.saved
    node.apply({"s": saved["set"] + 1, "vrev": saved["vrev"] + 1, "valve": SET_OPEN_ORDER,
                "vt": int(world.t) - REOPEN_ORDER_S - 60, "lamp": LAMPS.index(("red", 2))})
    assert node.shut


def check_old_or_replayed_set_moves_nothing():
    world = World("cloud", folder(), [LEAK])
    node = world.nodes[LEAK]
    until(world, lambda: node.shut)
    old = dict(node.saved)
    node.open_valve()                                # by hand, at the node
    assert not node.shut
    node.apply({"s": old["set"] + 1000, "vrev": old["vrev"] + 1, "valve": SET_CLOSE, "vt": int(world.t) - 300,
                "lamp": LAMPS.index(("red", 2))})
    assert not node.shut, "decided before the person opened it"
    for _ in range(5):
        world.minute()
    brain = world.host.devices[LEAK].brain
    assert not node.shut and not brain.valve["closed"] and brain.hand_changed > -1e18
    assert world.gateway.analytics.valves[LEAK]["state"] == "open"
    node.apply({"s": node.saved["set"], "vrev": old["vrev"] + 9, "valve": SET_CLOSE, "vt": int(world.t),
                "lamp": 0})
    assert not node.shut, "a SET number already seen"
    node.apply({"s": node.saved["set"] + 1, "vrev": 0, "valve": SET_CLOSE, "vt": int(world.t), "lamp": 0})
    assert not node.shut, "vrev 0: the server has decided nothing"


def check_failing_valve():
    world = World("cloud", folder(), [LEAK])
    node, board = world.nodes[LEAK], world.boards[LEAK]
    working = board.actuators.valve
    board.actuators.valve = lambda closed: False
    until(world, lambda: node.saved["fault"])
    for _ in range(3):
        world.minute()
    brain = world.host.devices[LEAK].brain
    codes = [c for n, _, _, c in world.messages("ALERT") if n == LEAK]
    assert "VALVE_FAULT" in codes and "VALVE_CLOSED" not in codes, codes
    assert brain.valve["fault"] and not brain.shut and not node.shut
    assert any(i["category"] == "VALVE_FAULT" for i in world.gateway.snapshot()["active_incidents"])
    brain.save_state()
    world.start_gateway()                            # the server restarting does not clear the fault
    assert world.host.devices[LEAK].brain.valve["fault"]
    board.actuators.valve = working
    world.gateway.order(LEAK, "VALVE_CLOSE")
    for _ in range(3):
        world.minute()
    assert node.shut and not node.saved["fault"] and not world.host.devices[LEAK].brain.valve["fault"]


def check_trial_needs_an_open_valve():
    world = World("cloud", folder(), [LEAK], host_options={"trial_reopen": True})
    node = world.nodes[LEAK]
    until(world, lambda: node.shut)
    for _ in range(3):
        world.minute()
    brain = world.host.devices[LEAK].brain
    quiet = {"flow_lps": 0.0, "pressure_m": 10.0}
    running = lambda: {"why": "LEAK_NIGHT_FLOW", "hold": ["LEAK_NIGHT_FLOW"], "readings": 0, "quiet": 0}
    brain.trial["run"] = running()
    brain.set_valve(False, "trial")
    brain.device_shut = True                         # readings from before the node opened it
    for i in range(TRIAL_QUIET + 5):
        brain.try_reopening(i * 60, quiet, True)
    assert brain.trial["run"]["quiet"] == 0 == brain.trial["run"]["readings"], "they pass nothing"
    brain.device_shut = False
    for i in range(TRIAL_QUIET):
        brain.try_reopening(i * 60, quiet, True)
    assert brain.trial["run"] is None and not brain.valve["closed"], "really open and quiet: passed"

    brain.set_valve(True, "LEAK_NIGHT_FLOW")
    brain.trial["run"] = running()
    brain.set_valve(False, "trial")
    brain.device_report("VALVE_FAULT")               # the node could not open it
    assert brain.trial["run"] is None and brain.valve["closed"]


def check_trial_survives_a_real_clock():
    """radio_poll() once compared the wall clock with the monotonic one, so on a real board every
    trial failed at the first look at the radio."""
    class Quiet:
        def send(self, raw, cad=False):
            return True

        def receive(self):
            return []

    board = NodeBoard("B-101", Quiet(), lambda: (0.0, {"flow_lps": 0.0, "pressure_m": 10.0}), None,
                      {n: sim_key(n) for n in ("B-101", "GATEWAY")}, gpio=False)
    board.log = lambda text: None
    node = make_node("B-101", board)
    board.advance()
    node.step()
    node.trial["run"] = {"why": "LEAK_NIGHT_FLOW", "hold": ["LEAK_NIGHT_FLOW"], "readings": 0, "quiet": 0}
    node.radio_poll()
    assert node.trial["run"], "readings are fresh: the trial goes on"
    node.reading_at -= STALE_S + 1
    node.radio_poll()
    assert node.trial["run"] is None and node.valve["closed"], "no readings: shut again"


# --- packets and storage ---
def check_forged_and_foreign_packets():
    world = World("cloud", folder(), [LEAK, "B-101"])
    world.minute()
    device = world.host.devices[LEAK]
    forged = pack({"k": "RAW", "n": LEAK, "s": 10 ** 6, "t": world.t, "st": 0, "dev": 0, "r_flow": 9.0},
                  b"attacker-guessed")
    edge_node = pack({"k": "ALERT", "n": LEAK, "s": 10 ** 6, "t": world.t, "a": "LEAK_NIGHT_FLOW"}, sim_key(LEAK))
    before = device.seq
    world.air.to_gateway += [(world.t, forged), (world.t, edge_node)]
    world.minute(read=False)
    assert world.gateway.snapshot()["network_health"]["bad_packets"] == 1
    assert device.seq == before and not world.messages("ALERT")
    assert sum("edge-based program" in line for line in world.said) == 1
    # a repeated reading is confirmed again but not taken in, and is no sign of life
    replay = pack({"k": "RAW", "n": LEAK, "s": before, "t": world.t, "st": 0, "dev": 0}, sim_key(LEAK))
    device.heard, sent = None, world.air.traffic[("GATEWAY", "ACK")][0]
    world.air.to_gateway.append((world.t, replay))
    world.minute(read=False)
    assert world.air.traffic[("GATEWAY", "ACK")][0] > sent and device.heard is None


def check_queue_keeps_readings_in_order():
    path = os.path.join(folder(), "queue.jsonl")
    store = SampleStore(path)
    store.limit = 3
    store.add({"k": "RAW", "t": 0, "a": "VALVE_CLOSED", "dev": 1})
    for i in range(5):
        store.add({"k": "RAW", "t": i, "dev": 0})
    assert [m["s"] for m in store.queue] == [1, 4, 5, 6], "newest readings, and every valve report"
    assert [m["s"] for m in store.due(0, True)] == [1] and store.due(0.5, True) == []
    store.ack(1)
    assert [m["s"] for m in SampleStore(path).queue] == [4, 5, 6] and [m["s"] for m in store.due(1, True)] == [4]


# --- the real programs ---
def check_programs_end_to_end():
    """The real dashboard server, cloud gateway and thin node: the server finds the leak and the
    node shuts its valve; the reopen endpoint is called and the node reports it open."""
    root, port, udp = folder(), free_port(), free_udp_port()
    keys = os.path.join(root, "keys.json")
    with open(keys, "w") as f:
        json.dump({n: sim_key(n).hex() for n in ("GATEWAY", LEAK)}, f)
    tree = os.path.join(ROOT, "gateway", "virtual_1node.json")
    data, sd = os.path.join(root, "gateway"), os.path.join(root, "node")
    logs = {name: os.path.join(root, f"{name}.log") for name in ("gateway", "node")}
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "AQUA_DASHBOARD_TOKEN": GATEWAY_TOKEN}

    def start(name, *args):
        return subprocess.Popen([sys.executable, "-u", "-m", *args], cwd=ROOT, env=env,
                                stdout=open(logs[name], "a"), stderr=subprocess.STDOUT)

    dash, running = Dashboard(root, port), []
    try:
        running.append(start("gateway", "gateway.cloud_based", "--keys", keys, "--data", data, "--tree", tree,
                             "--port", "none", "--udp-listen", f"127.0.0.1:{udp}",
                             "--dashboard-url", f"http://127.0.0.1:{port}/api/v1/ingest", "--dashboard-id", "test"))
        running.append(start("node", "edge.cloud_based", LEAK, "--tree", tree, "--keys", keys, "--sd", sd,
                             "--radio", "udp", "--gateway", f"127.0.0.1:{udp}", "--actuators", "sim",
                             "--prime-s", "86400", "--step-s", "0.05"))

        def valve():
            status, reply = dash.call("/valves?gateway_id=test")
            return reply["valves"].get(LEAK, {}) if status == 200 else {}

        started = time.monotonic()
        wait(lambda: valve().get("state") == "closed", "the dashboard shows the valve closed", 400)
        print(f"  closed {time.monotonic() - started:.0f} s after the node started", flush=True)
        assert "VALVE closed" in open(logs["node"]).read()
        assert dash.reopen(LEAK)[0] == 202
        wait(lambda: valve()["state"] == "open", "the node reports its valve open", 90)
        assert valve()["request"]["status"] == "sent" and "VALVE open" in open(logs["node"]).read()
        frame = dash.call("/snapshots/latest?gateway_id=test")[1]["frame"]
        assert frame["mode"] == "cloud" and frame["snapshot"]["nodes"][LEAK]["light"]
        for process in running:
            process.send_signal(signal.SIGTERM)
        assert [process.wait(20) for process in running] == [0, 0]

        # an edge-based gateway's data folder is refused
        other = os.path.join(root, "edge-data")
        os.makedirs(other)
        open(os.path.join(other, "gateway_state.json"), "w").close()
        refused = subprocess.run([sys.executable, "-m", "gateway.cloud_based", "--keys", keys, "--data", other,
                                  "--tree", tree, "--port", "none", "--udp-listen", "127.0.0.1:0"],
                                 cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
        assert refused.returncode == 1 and "edge-based gateway's state" in refused.stderr, refused.stderr
    except Exception:
        for name, path in logs.items():
            print(f"--- {name}", "".join(open(path).readlines()[-15:]))
        raise
    finally:
        for process in running:
            if process.poll() is None:
                process.kill()
        dash.stop()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    CLOSES = leak_closes_at()
    for check in (check_thin_node_has_no_detectors, check_queue_keeps_readings_in_order,
                  check_trial_survives_a_real_clock, check_same_results_as_edge, check_same_results_for_a_pipe_leak,
                  check_no_link_no_decisions,
                  check_lost_packets, check_restarts_change_nothing, check_server_that_lost_everything_opens_nothing,
                  check_operator_orders_and_old_ones, check_old_or_replayed_set_moves_nothing, check_failing_valve,
                  check_trial_needs_an_open_valve, check_forged_and_foreign_packets, check_programs_end_to_end):
        started = time.monotonic()
        check()
        print(f"{check.__name__}: ok ({time.monotonic() - started:.0f} s)", flush=True)
    print("cloud baseline tests passed")
