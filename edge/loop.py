"""What both kinds of node program share (edge_based.py, cloud_based.py): the run loop, the common
command line and building the board. No detection imports.
"""
import argparse
import os
import signal
import threading

from edge.board import SAMPLE_S, NodeBoard
from edge.sensors import DATASET, sensor_reader
from edge.store import saved_sample_time
from edge.valve import take_request
from gateway.topology import BUILDING_B, Topology
from hal.packet import add_names, load_keys
from hal.pi_hal import RX_POLL_S, open_radio
from hal.udp import UdpRadio, parse_address

HERE = os.path.dirname(os.path.abspath(__file__))
NET_POLL_S = 0.1        # how often the network radio is read: far quicker than any reply is awaited


def _requests(node, folder):
    """Carry out what was asked for at the node itself."""
    if take_request(folder):
        node.note("valve: open requested at the node")
        node.open_valve()


def _wait_with_radio(node, board, until_t, stop=None, folder=None, poll_s=RX_POLL_S):
    """Sleep until `until_t` (None: until stopped), polling Node.radio_poll() for beacons, ACKs,
    orders and urgent retries, and watching for requests. Uses monotonic time for deadlines."""
    mono = getattr(board, "monotonic", board.now)
    while not (stop and stop.is_set()):
        if until_t is not None and mono() >= until_t:
            break
        _requests(node, folder)
        wake = node.radio_poll()
        dt = poll_s if until_t is None else until_t - mono()
        if wake is not None:
            dt = min(dt, max(0.0, wake - mono()))
        board.sleep(min(dt, poll_s) if dt > 0 else 0.0)


def run(node, board, step_s=None, stop=None, folder=None, poll_s=RX_POLL_S, first_taken=False):
    """One firmware step per sensor row: on each new minute (real time), or every step_s seconds.
    Returns at the end of the data, or when stopped. first_taken: board already holds the first row."""
    mono = getattr(board, "monotonic", board.now)

    def pause():
        if step_s is None:
            now = board.now()
            _wait_with_radio(node, board, mono() + (SAMPLE_S - now % SAMPLE_S), stop, folder, poll_s)
        elif step_s:
            _wait_with_radio(node, board, mono() + step_s, stop, folder, poll_s)

    if step_s is None and not first_taken:
        pause()
    while not (stop and stop.is_set()) and (first_taken or board.advance()):
        first_taken = False
        _requests(node, folder)
        node.step()
        pause()


def node_parser(description):
    env = os.environ.get
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("name", help="node name, e.g. B-203 or B-TANK")
    ap.add_argument("--tree", default=env("AQUA_TOPOLOGY_PATH") or BUILDING_B,
                    help="the building tree the gateway uses: the node's type and any custom radio number")
    ap.add_argument("--keys", required=True, help="key file (see load_keys in hal/packet.py)")
    ap.add_argument("--csv", default=DATASET, help="sensor dataset, one row per minute (sensors/)")
    ap.add_argument("--sd", default=os.path.join(HERE, "sd"), help="folder for the queue and saved state")
    ap.add_argument("--slot", nargs=2, type=int, metavar=("INDEX", "COUNT"), help="time slot, e.g. 5 20")
    ap.add_argument("--radio", choices=["lora", "udp"], default=env("AQUA_RADIO") or "lora",
                    help="lora: the Pi's SX1278. udp: the network, to a gateway with --udp-listen")
    ap.add_argument("--gateway", default=env("AQUA_GATEWAY_ADDR") or "127.0.0.1:1700",
                    help="with --radio udp: the gateway's host:port")
    ap.add_argument("--actuators", choices=["gpio", "sim"], default=env("AQUA_ACTUATORS") or "gpio",
                    help="gpio: the Pi's LEDs, buzzer and valve relay. sim: only logged")
    ap.add_argument("--prime-s", type=float, default=float(env("AQUA_PRIME_S") or 0),
                    help="first start only: learn from this much of the data at once (86400: the first day)")
    ap.add_argument("--step-s", type=float, default=float(env("AQUA_STEP_S")) if env("AQUA_STEP_S") else None,
                    help="one row every this many seconds instead of one a minute (single-node demos)")
    ap.add_argument("--exit-at-end", action="store_true", help="stop when the data ends")
    return ap


def open_board(a, ap, suffix=""):
    """(board, building tree, sample time to resume after, radio) from the command line. suffix
    keeps one kind of node's saved files apart from the other's in the same folder."""
    try:
        topology = Topology.load(a.tree)
        add_names(topology.radio_ids)           # before load_keys, which refuses unknown names
        try:
            keys = load_keys(a.keys)
        except ValueError as e:
            ap.error(f"key file {a.keys}: {e}")
        address = parse_address(a.gateway) if a.radio == "udp" else None
    except (OSError, ValueError) as e:
        ap.error(str(e))
    if a.name not in topology.devices:
        ap.error(f"{a.name} is not a tank or flat in {a.tree}")
    if a.name not in keys or "GATEWAY" not in keys:
        ap.error(f"{a.keys} needs a key for {a.name} and for GATEWAY")
    os.makedirs(a.sd, exist_ok=True)
    storage = os.path.join(a.sd, f"{a.name}{suffix}.jsonl")
    try:
        resume_after = saved_sample_time(storage)
        get_readings = sensor_reader(a.name, a.csv, after=resume_after)
    except (OSError, ValueError) as e:
        ap.error(str(e))
    try:
        radio = UdpRadio(*address) if address else open_radio()
    except OSError as e:
        ap.error(f"radio: {e}")
    board = NodeBoard(a.name, radio, get_readings, storage, keys,
                      slot=tuple(a.slot) if a.slot else None, step_s=a.step_s, gpio=a.actuators == "gpio")
    return board, topology, resume_after, radio


def stop_on_signals():
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):         # Ctrl-C and `docker stop`
        signal.signal(sig, lambda *_: stop.set())
    return stop
