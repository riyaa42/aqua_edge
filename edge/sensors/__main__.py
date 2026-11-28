"""Print a node's first readings: python3 -m edge.sensors B-203 [dataset.csv]"""
import sys

from edge.sensors import sensor_reader

if len(sys.argv) not in (2, 3):
    sys.exit(__doc__)
try:
    get_readings = sensor_reader(*sys.argv[1:])
except (OSError, ValueError) as e:
    sys.exit(str(e))
for _ in range(5):
    print(get_readings())
