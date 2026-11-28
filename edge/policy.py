"""The node's alert rules, from planning/valve_spec.md: what each result does to the LEDs and the
valve. THE ONE PLACE to change them; change the spec with it.

    ALERT    red: close the node's valve, intermittent buzzer. Whether the node or the gateway
             found it. It reopens by hand, or by itself after a tolerance period of normal
             readings once its conditions are over (aqua_edge.py, reopen_when_normal). A flat can also
             be set to try reopening while still red (try_reopening; off by default)
    WARNING  yellow: valve left as it is, no buzzer
    INFO     does not light anything: green unless something else is going on

The LED shows one colour and one blink pattern (spec sections 2 to 5):

    green   solid       nothing active
    yellow  one blink   Y1: messages not getting through
    yellow  two blinks  Y2: sensor or monitoring trouble
    red     one blink   R1: water quality
    red     two blinks  R2: leak, supply or consumption

Red beats yellow beats green; R1 beats R2 and Y1 beats Y2. The level is worked out from the code a
packet already carries (hal/packet.py), so changing this table changes nothing on the radio.

    python3 -m edge.policy      # self-check
"""
from hal.packet import ALERTS, FLAGS

ALERT, WARNING, INFO = "ALERT", "WARNING", "INFO"
GREEN, YELLOW, RED = "green", "yellow", "red"
SOLID, ONE_BLINK, TWO_BLINKS = 0, 1, 2

# R1: the node's own water-quality alerts
_QUALITY = ("QUALITY_LIMIT_VIOLATION", "QUALITY_TEMPORAL_ANOMALY", "QUALITY_RUNOFF", "QUALITY_CHEMICAL")
# R2: the node's own leak, supply and consumption alerts
_DISTRIBUTION = (
    "LEAK_NIGHT_FLOW", "LEAK_CONTINUOUS_FLOW", "LEAK_PRESSURE_DROP", "ABNORMAL_SUPPLY",
    "ABNORMAL_CONSUMPTION", "UNUSUAL_NIGHT_CONSUMPTION", "SUDDEN_CONSUMPTION_CHANGE", "PROLONGED_ZERO_FLOW",
)
# R2: problems the gateway finds across nodes and tells the nodes concerned (spec section 7)
_GATEWAY = ("PIPE_LEAK", "PIPE_LEAK_CAUSE", "SUPPLY_EVENT", "LEAK_TANK_DRAIN", "SUPPLY_UNCERTAIN")
_ORDERS = ("VALVE_CLOSE", "ALERT_LED_ON")      # from the gateway's operator: red, R2
_WARNING = ("SUPPLY_LOW", "VALVE_FAULT")       # not named in the spec: Y2 (monitoring), closes nothing
_INFO = (
    "FLAT_LEAK_CLEARED", "CONSUMPTION_CLEARED", "ZERO_FLOW_CLEARED", "SUPPLY_FLOW_CLEARED",
    "QUALITY_CLEARED", "QUALITY_TEMPORAL_CLEARED", "SUPPLY_RECOVERED", "PIPE_LEAK_CLEARED", "PIPE_LEAK_CAUSE_CLEARED", "SUPPLY_CLEARED",
    "VALVE_OPEN", "ALERT_LED_OFF", "VALVE_CLOSED", "VALVE_OPENED",
)
LEVELS = {**{c: ALERT for c in (*_QUALITY, *_DISTRIBUTION, *_GATEWAY, *_ORDERS)},
          **{c: WARNING for c in _WARNING}, **{c: INFO for c in _INFO}}


def level(code):
    """The level of an alert, flag or order name. An unknown name is a WARNING: it closes nothing."""
    return LEVELS.get(code, WARNING)


def signal(codes, red=False, delivery=False, sensors=False):
    """(colour, blinks) for everything in force. codes: the active alerts, flags and gateway
    problems. red: an operator's order to close the valve, or for the alert LED, is in force. delivery: messages are
    not getting through. sensors: readings missing or stale, or the clock cannot be trusted."""
    alerts = {c for c in codes if level(c) == ALERT}
    if alerts or red:
        return RED, ONE_BLINK if alerts & set(_QUALITY) else TWO_BLINKS
    if delivery:
        return YELLOW, ONE_BLINK
    if sensors or any(level(c) == WARNING for c in codes):
        return YELLOW, TWO_BLINKS
    return GREEN, SOLID


if __name__ == "__main__":
    assert len(LEVELS) == sum(map(len, (_QUALITY, _DISTRIBUTION, _GATEWAY, _ORDERS, _WARNING, _INFO))), \
        "a name has two levels"
    assert not (set(ALERTS) | set(FLAGS)) - set(LEVELS), f"no level for {(set(ALERTS) | set(FLAGS)) - set(LEVELS)}"
    assert not set(LEVELS) - set(ALERTS) - set(FLAGS), "a level for a name no packet carries"
    assert level("LEAK_CONTINUOUS_FLOW") == ALERT and level("SOMETHING_NEW") == WARNING
    # the examples in the spec, section 5
    assert signal(["QUALITY_CHEMICAL", "LEAK_NIGHT_FLOW"]) == (RED, 1)
    assert signal(["LEAK_NIGHT_FLOW"], delivery=True) == (RED, 2)
    assert signal([], delivery=True, sensors=True) == (YELLOW, 1)
    assert signal([], sensors=True) == (YELLOW, 2) == signal(["SUPPLY_LOW"])
    assert all(signal([c]) == (RED, 2) for c in _GATEWAY)
    assert signal(["QUALITY_CLEARED"]) == (GREEN, 0) and signal([], red=True) == (RED, 2)
    print("policy self-check passed")
