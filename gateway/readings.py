"""Checks the sensor readings in a node's message before the gateway uses them. The gateway has no
sensors of its own: these are all it knows of them (15-minute summaries, and the values sent with
alerts and flags). A reading that cannot be true is left out, as if the sensor gave nothing.
"""
import math

AMOUNTS = ("vol_l", "in_l", "inflow")      # litres and flows: never below zero


def check(message):
    """-> (the message without its bad readings, the names of those left out, sorted)."""
    bad = sorted(k for k, v in message.items()
                 if isinstance(v, float) and (not math.isfinite(v) or (k in AMOUNTS and v < 0)))
    return ({k: v for k, v in message.items() if k not in bad} if bad else message), bad
