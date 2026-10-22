"""Webots controller for every sensor node: plugs the unchanged firmware into Webots."""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "firmware")]

from controller import Robot  # noqa: E402
from aqua_edge import FlatNode, TankNode  # noqa: E402
from simlink import STEP_S  # noqa: E402
from webots_hal import WebotsHAL, TIME_STEP_MS  # noqa: E402

robot = Robot()
hal = WebotsHAL(robot)
node = (TankNode if hal.name.endswith("TANK") else FlatNode)(hal.name, hal)

while robot.step(TIME_STEP_MS) != -1:
    if hal.t < hal.feed.duration_s:
        node.step()
        hal.t += STEP_S
