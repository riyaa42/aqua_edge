"""Write ns3/positions.csv: where every node and the gateway sit (metres), from the Webots layout.

    python3 ns3/positions.py
"""
import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "webots"))
from make_world import flat_xyz, GATEWAY_AT, ROOF  # noqa: E402
from simlink import SensorFeed  # noqa: E402

if __name__ == "__main__":
    rows = [(n, *((2.2, 0, ROOF + 1.2) if n.endswith("TANK") else flat_xyz(n))) for n in SensorFeed().nodes()]
    rows.append(("GATEWAY", *GATEWAY_AT))
    with open(os.path.join(HERE, "positions.csv"), "w", newline="") as f:
        csv.writer(f).writerows([("node", "x", "y", "z"), *rows])
    assert len(rows) == 22 and rows[-1][0] == "GATEWAY"
    print(f"wrote positions.csv: {len(rows) - 1} nodes + gateway")
