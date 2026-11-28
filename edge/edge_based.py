"""Node entry point, edge-based solution: alerts that need only this node's data are decided on
the node, which acts on them itself (policy.py: LEDs, buzzer, valve). It runs the checks in
aqua_edge.py with the board from board.py and sensor readings from sensors/ (a recorded dataset).
The radio is the Pi's SX1278 (hal/pi_hal.py) or, without radio hardware, the network (hal/udp.py).
Setup: hal/README.md, "Running on the Pi", or edge/README.md, "Run virtual nodes". Not tested on
real hardware.

    uv run prod-edge-edge B-203 --keys aqua_keys.json --sd /home/pi/aqua --slot 5 20       # on a Pi
    python3 -m edge.edge_based B-203 --keys aqua_keys.json --sd /home/pi/aqua --slot 5 20   # without uv
    python3 -m edge.edge_based B-203 --keys aqua_keys.json --sd ./b203 \\
        --radio udp --gateway 127.0.0.1:1700 --actuators sim --prime-s 86400               # no hardware

It runs until stopped (Ctrl-C or SIGTERM). When the dataset ends it stops reading sensors but goes
on sending what is waiting and obeying the gateway.
"""
import argparse
import os
import signal
import threading

from edge.aqua_edge import REOPEN_AFTER_S, FlatNode, TankNode, saved_sample_time
from edge.board import SAMPLE_S, NodeBoard
from edge.sensors import DATASET, sensor_reader
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


def prime(node, board, seconds):
    """First start only: learn from the first `seconds` of the data at once, without checks,
    messages or the valve. True if a row beyond them has been read and still waits to be used."""
    first = None
    while board.advance():
        first = board.sample_time() if first is None else first
        if board.sample_time() - first >= seconds or not node.prime(board.read()):
            return True
    return False


def make_node(name, board, topology=None):
    """A tank or a flat node, as the building tree says (default: building B's)."""
    topology = topology or Topology.load()
    return TankNode(name, board) if name == topology.tank else FlatNode(name, board)


def parse_args(argv=None):
    env = os.environ.get
    ap = argparse.ArgumentParser(description="Run an Aqua Edge node (edge-based solution).")
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
    ap.add_argument("--reopen-after-s", type=float, default=float(env("AQUA_REOPEN_AFTER_S") or REOPEN_AFTER_S),
                    help="tolerance before a closed valve reopens by itself: this long normal once its "
                         "alerts have cleared (default %(default)s)")
    ap.add_argument("--trial-reopen", action="store_true",
                    default=(env("AQUA_TRIAL_REOPEN") or "").lower() in ("1", "true", "yes", "on"),
                    help="flats: after hours shut by their own alert, reopen to see whether the water "
                         "stops, and shut again if not. Water may run through a leak meanwhile")
    ap.add_argument("--exit-at-end", action="store_true", help="stop when the data ends")
    return ap.parse_args(argv), ap


def main(argv=None):
    a, ap = parse_args(argv)
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
    if not 0 <= a.reopen_after_s < float("inf"):
        ap.error("--reopen-after-s must be 0 or more")
    if a.name not in topology.devices:
        ap.error(f"{a.name} is not a tank or flat in {a.tree}")
    if a.name not in keys or "GATEWAY" not in keys:
        ap.error(f"{a.keys} needs a key for {a.name} and for GATEWAY")
    os.makedirs(a.sd, exist_ok=True)
    storage = os.path.join(a.sd, f"{a.name}.jsonl")
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
    node = make_node(a.name, board, topology)
    node.reopen_after_s, node.trial_reopen = a.reopen_after_s, a.trial_reopen

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):         # Ctrl-C and `docker stop`
        signal.signal(sig, lambda *_: stop.set())
    poll_s = NET_POLL_S if address else RX_POLL_S
    print(f"{a.name} running: radio {a.radio}{f' to {a.gateway}' if address else ''}, actuators {a.actuators}")
    try:
        first_taken = prime(node, board, a.prime_s) if a.prime_s and resume_after is None else False
        run(node, board, a.step_s, stop, a.sd, poll_s, first_taken)
        if not stop.is_set() and not a.exit_at_end:
            print(f"{a.name}: end of the sensor data; still sending and taking orders")
            _wait_with_radio(node, board, None, stop, a.sd, poll_s)
    finally:
        node.fail_trial("the node is stopping")          # nobody would be watching it
        node.save_state()
        getattr(radio, "close", lambda: None)()         # apart from a trial, the valve is left as it is
        print(f"{a.name} stopped")


if __name__ == "__main__":
    main()
