"""How serious each thing the gateway reports is, and what each node's light should be showing, by
planning/valve_spec.md. Alert and command names get their level from edge/policy.py, the table the
nodes act on; only conditions the gateway alone knows are listed here. These are labels for the
dashboard: what closes a valve is the commands core.py sends.
"""
from edge import policy

# only the gateway knows of these. None closes a valve (spec section 8)
_OWN = {"NODE_SILENT": policy.WARNING, "CLOCK_SKEW": policy.WARNING, "BAD_PACKET": policy.WARNING,
        "BAD_READING": policy.WARNING, "BALANCE_PAUSED": policy.WARNING, "ORDER_EXPIRED": policy.WARNING,
        "NODE_BACK": policy.INFO, "CLOCK_OK": policy.INFO, "ORDER": policy.INFO,
        "PIPE_LEAK_PAST": policy.INFO}         # found only once it was history
_CATEGORIES = {"FLAT_LEAK": "LEAK_NIGHT_FLOW"}      # analytics.py's name for any of a flat's leak alerts
_TOLD = ("PIPE_LEAK", "SUPPLY_EVENT", "LEAK_TANK_DRAIN", "SUPPLY_UNCERTAIN")


def level(name):
    """ALERT, WARNING or INFO for an alert, a gateway event or an incident category."""
    name = _CATEGORIES.get(name, name)
    return _OWN.get(name) or policy.level(name)


def event_level(event):
    kind = event["event"]
    return level(event.get("alert", kind) if kind in ("NEW_ALERT", "REPEAT_ALERT") else kind)


def light(node, incidents, told, supply_low=False, ordered=False):
    """The light `node` should be showing, as far as the gateway knows. It cannot see a node's own
    delivery or sensor warnings, so the node may show yellow when this says green.
    incidents: (target, category) of the open ones. told: the problems the gateway has told this
    node of and not ended. ordered: an operator's order to close or to light red is in force."""
    # A cross-node problem lights only the nodes told of it (spec section 8), whatever its target
    codes = {_CATEGORIES.get(c, c) for target, c in incidents if target == node and c not in _TOLD}
    codes |= set(told) | ({"SUPPLY_LOW"} if supply_low else set())
    codes = {c for c in codes if c not in ("NODE_SILENT", "CLOCK_SKEW")}
    sensors = any(target == node and c == "CLOCK_SKEW" for target, c in incidents)
    colour, blinks = policy.signal(codes, red=ordered, sensors=sensors)
    pattern = "G" if colour == policy.GREEN else f"{'R' if colour == policy.RED else 'Y'}{blinks}"
    return {"colour": colour, "blinks": blinks, "pattern": pattern}
