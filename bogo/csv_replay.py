"""Sensor readings replayed from a CSV, in place of physical sensors: one row per step, one column
per node and sensor ("B-203:flow_lps"), the format simulation/webots/make_scenario.py writes.
Standard library only.
"""
import csv
import os

_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CSV = os.path.join(_ROOT_DIR, "simulation", "webots", "data", "sensors.csv")


class CSVReplay:
    def __init__(self, name, csv_path=DEFAULT_CSV):
        self.rows = csv.reader(open(csv_path))
        header = next(self.rows)
        prefix = name + ":"
        self.cols = [(i, c[len(prefix):]) for i, c in enumerate(header) if c.startswith(prefix)]
        if not self.cols:
            raise SystemExit(f"no columns for {name} in {csv_path}")
        self.t, self.r = 0.0, {}         # the CSV time of the current row, and its readings

    def advance(self):
        """Load the next row. False at the end of the file."""
        row = next(self.rows, None)
        if row is None:
            return False
        self.t, self.r = float(row[0]), {k: float(row[i]) for i, k in self.cols}
        return True

    def read(self):
        return self.r
