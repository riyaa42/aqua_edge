"""The node's "sensors": readings replayed from a recorded dataset, in place of physical sensors.

    get_readings = sensor_reader("B-203")           # after=t: carry on after the row of time t
    sample = get_readings()      # (time_s, {"flow_lps": ..., "pressure_m": ...}), or None at the end

The dataset is a CSV with one row per minute: the time in seconds first, then one column per node
and sensor ("B-203:flow_lps"). The default is building B's 48 simulated hours, which
simulation/webots/make_scenario.py writes. Standard library only.

    python3 -m edge.sensors B-203      # print the first readings
"""
import csv
import os

_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATASET = os.path.join(_ROOT_DIR, "simulation", "webots", "data", "sensors.csv")


def _numbers(row, columns):
    """The readings in one row. A missing or broken value is left out: a missing reading."""
    values = {}
    for i, sensor in columns:
        try:
            values[sensor] = float(row[i])
        except (IndexError, ValueError):
            pass
    return values


def sensor_reader(node, dataset=DATASET, after=None):
    """Make the function that gets `node`'s readings from `dataset`: each call returns the next
    row as (time_s, {sensor: value}), and None once there are no more. time_s is the dataset's own
    time. after: skip rows up to and including this time (to carry on after a restart). Raises
    OSError if the file cannot be read, ValueError if it has nothing for the node."""
    with open(dataset, newline="") as f:
        header = next(csv.reader(f), [])
    prefix = node + ":"
    columns = [(i, name[len(prefix):]) for i, name in enumerate(header) if name.startswith(prefix)]
    if not columns:
        raise ValueError(f"no columns for {node} in {dataset}")

    def samples():
        with open(dataset, newline="") as f:
            rows = csv.reader(f)
            next(rows)
            for row in rows:
                try:
                    t = float(row[0])
                except (IndexError, ValueError):
                    continue                # a row without a time cannot be used
                if after is None or t > after:
                    yield t, _numbers(row, columns)

    remaining = samples()

    def get_readings():
        return next(remaining, None)

    return get_readings
