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
import os

from edge.aqua_edge import REOPEN_AFTER_S, FlatNode, TankNode
from edge.loop import NET_POLL_S, _wait_with_radio, node_parser, open_board, run, stop_on_signals
from gateway.topology import Topology
from hal.pi_hal import RX_POLL_S


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
    ap = node_parser("Run an Aqua Edge node (edge-based solution).")
    ap.add_argument("--reopen-after-s", type=float, default=float(env("AQUA_REOPEN_AFTER_S") or REOPEN_AFTER_S),
                    help="tolerance before a closed valve reopens by itself: this long normal once its "
                         "alerts have cleared (default %(default)s)")
    ap.add_argument("--trial-reopen", action="store_true",
                    default=(env("AQUA_TRIAL_REOPEN") or "").lower() in ("1", "true", "yes", "on"),
                    help="flats: after hours shut by their own alert, reopen to see whether the water "
                         "stops, and shut again if not. Water may run through a leak meanwhile")
    return ap.parse_args(argv), ap


def main(argv=None):
    a, ap = parse_args(argv)
    if not 0 <= a.reopen_after_s < float("inf"):
        ap.error("--reopen-after-s must be 0 or more")
    board, topology, resume_after, radio = open_board(a, ap)
    node = make_node(a.name, board, topology)
    node.reopen_after_s, node.trial_reopen = a.reopen_after_s, a.trial_reopen

    stop = stop_on_signals()
    poll_s = NET_POLL_S if a.radio == "udp" else RX_POLL_S
    print(f"{a.name} running: radio {a.radio}{f' to {a.gateway}' if a.radio == 'udp' else ''}, actuators {a.actuators}")
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
