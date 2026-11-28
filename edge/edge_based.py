"""Node entry point, edge-based solution: alerts that need only this node's data are decided on
the node. It runs the checks in aqua_edge.py on the Pi, with the radio from hal/pi_hal.py, the
board (clock, LEDs, buzzer) from board.py, and sensor readings replayed from a CSV by bogo/.
Setup: hal/README.md, "Running on the Pi". Not tested on real hardware.

    python3 -m edge.edge_based B-203 --keys aqua_keys.json --sd /home/pi/aqua --slot 5 20   # real time
    python3 -m edge.edge_based B-203 --keys aqua_keys.json --sd /home/pi/aqua --step-s 1     # fast demo
"""
import argparse
import os

from bogo.csv_replay import DEFAULT_CSV, CSVReplay
from edge.aqua_edge import FlatNode, TankNode
from gateway.topology import BUILDING_B, Topology
from edge.board import NodeBoard
from hal.packet import add_names, load_keys
from hal.pi_hal import RX_POLL_S, open_radio

HERE = os.path.dirname(os.path.abspath(__file__))


def _wait_with_radio(node, board, until_t):
    """Sleep until `until_t` while polling Node.radio_poll() for beacons, ACKs, and urgent retries."""
    while True:
        now = board.now()
        if now >= until_t:
            break
        wake = node.radio_poll()
        dt = until_t - board.now()
        if wake is not None:
            dt = min(dt, max(0.0, wake - board.now()))
        board.sleep(min(dt, RX_POLL_S) if dt > 0 else 0.0)


def run(node, board, step_s=None):
    """One firmware step per sensor row: on each new minute (real time), or every step_s seconds."""
    if step_s is None:
        _wait_with_radio(node, board, board.now() + (60 - board.now() % 60))
    while board.advance():
        node.step()
        if step_s is None:
            _wait_with_radio(node, board, board.now() + (60 - board.now() % 60))
        elif step_s:
            _wait_with_radio(node, board, board.now() + step_s)


def make_node(name, board, topology=None):
    """A tank or a flat node, as the building tree says (default: building B's)."""
    topology = topology or Topology.load()
    return TankNode(name, board) if name == topology.tank else FlatNode(name, board)


def main():
    ap = argparse.ArgumentParser(description="Run an Aqua Edge node on a Raspberry Pi with an SX1278 radio "
                                             "(edge-based solution).")
    ap.add_argument("name", help="node name, e.g. B-203 or B-TANK")
    ap.add_argument("--tree", default=os.environ.get("AQUA_TOPOLOGY_PATH") or BUILDING_B,
                    help="the building tree the gateway uses: the node's type and any custom radio number")
    ap.add_argument("--keys", required=True, help="key file (see load_keys in hal/packet.py)")
    ap.add_argument("--csv", default=DEFAULT_CSV, help="sensor readings, one row per minute")
    ap.add_argument("--sd", default=os.path.join(HERE, "sd"), help="folder for the queue and baseline")
    ap.add_argument("--slot", nargs=2, type=int, metavar=("INDEX", "COUNT"), help="time slot, e.g. 5 20")
    ap.add_argument("--step-s", type=float, help="fast mode: one step every this many seconds, CSV time")
    a = ap.parse_args()
    try:
        topology = Topology.load(a.tree)
        add_names(topology.radio_ids)           # before load_keys, which refuses unknown names
        keys = load_keys(a.keys)
    except (OSError, ValueError) as e:
        ap.error(str(e))
    if a.name not in topology.devices:
        ap.error(f"{a.name} is not a tank or flat in {a.tree}")
    os.makedirs(a.sd, exist_ok=True)
    if a.name not in keys:
        ap.error(f"{a.name} has no key in {a.keys}")
    sensors = CSVReplay(a.name, a.csv)
    board = NodeBoard(a.name, open_radio(), sensors, os.path.join(a.sd, f"{a.name}.jsonl"), keys,
                      slot=tuple(a.slot) if a.slot else None, step_s=a.step_s)
    run(make_node(a.name, board, topology), board, a.step_s)


if __name__ == "__main__":
    main()
