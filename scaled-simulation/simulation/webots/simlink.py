"""Glue between the EPANET scenario files and a simulated node: sensor values and network outages.

Used by both the offline test (run_offline.py) and the Webots controllers, so both see
exactly the same sensor readings.
"""
import csv
import os

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
STEP_S = 60                     # one firmware step = one minute of water time


class SensorFeed:
    def __init__(self, path=os.path.join(DATA, "sensors.csv")):
        with open(path) as f:
            rows = list(csv.reader(f))
        self.cols = rows[0]
        self.rows = [[float(x) for x in r] for r in rows[1:]]
        self.duration_s = len(self.rows) * STEP_S

    def nodes(self):
        return sorted({c.split(":")[0] for c in self.cols[1:]})

    def read(self, node, t):
        row = self.rows[min(int(t // STEP_S), len(self.rows) - 1)]
        prefix = node + ":"
        return {c[len(prefix):]: v for c, v in zip(self.cols, row) if c.startswith(prefix)}


def load_outages(path=os.path.join(DATA, "outages.csv")):
    with open(path) as f:
        return [(r["nodes"], float(r["start_s"]), float(r["end_s"])) for r in csv.DictReader(f)]


def link_up(node, t, outages):
    """False while `node` (or everyone, for an outage on "all") cannot reach the radio network."""
    return not any(start <= t < end and who in ("all", node) for who, start, end in outages)
