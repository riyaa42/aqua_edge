"""The MQTT side of the Pi <-> gateway communication spec (comms/README.md): topic names, payloads,
delivery settings, and publishing and subscribing that can never disturb the node or the gateway.
Shared by edge.py and gateway.py. No MQTT library here: a "transport" is anything with
publish(topic, data, qos, retain, expiry_s) (client.py is the real one).

    python3 -m comms.common      # self-check
"""
import json
import math
from collections import namedtuple

from hal.packet import ALERTS, FIELDS, FLAGS, NAMES

VERSION = 1
ROOT = f"aqua/v{VERSION}"
BUILDING = "B"
GATEWAY = "gateway"             # the topic level for what belongs to the gateway, not to one node
WEEK_S = 7 * 86400
SENSOR_EXPIRY_S = 180           # a reading older than three minutes is not "current"

# How each kind of topic is published. expiry_s: the broker drops the message after this long.
Channel = namedtuple("Channel", "qos retain expiry_s")
CHANNELS = {
    "sensor": Channel(0, True, SENSOR_EXPIRY_S),
    "alert": Channel(1, False, WEEK_S),
    "flag": Channel(1, False, WEEK_S),
    "summary": Channel(1, False, WEEK_S),
    "cmd": Channel(1, False, WEEK_S),
    "event": Channel(1, False, WEEK_S),         # gateway decisions
    "state": Channel(1, True, None),
    "link": Channel(1, True, None),
    "status": Channel(1, True, None),
}

# Radio packet kinds that have a topic. ACK, BEACON and GOT stay on the radio.
KIND_CHANNEL = {"ALERT": "alert", "FLAG": "flag", "SUM": "summary", "CMD": "cmd"}
CHANNEL_KIND = {channel: kind for kind, channel in KIND_CHANNEL.items()}

# Reading name in the code -> name in a `sensor` payload. Where hal/packet.py already has a field
# for the quantity, its name is used.
SENSOR_FIELDS = {"flow_lps": "flow", "pressure_m": "pressure", "level_m": "level_m",
                 "inflow_lps": "inflow", "outflow_lps": "outflow", "ph": "ph", "tds_ppm": "tds",
                 "turbidity_ntu": "turb", "temp_c": "temp"}

Topic = namedtuple("Topic", "building node channel name")


# --- topics ---
def _level(text):
    if not text or any(c in text for c in "/+#\0"):
        raise ValueError(f"not a single topic level: {text!r}")
    return text


def topic(node, channel, name=None, building=BUILDING):
    """aqua/v1/{building}/{node}/{channel}[/{name}]. node: a node's name, or GATEWAY."""
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel {channel!r}")
    parts = [ROOT, _level(building), _level(node), channel]
    return "/".join(parts + ([_level(name)] if name is not None else []))


def topic_filter(node="+", channel="+", name=None, building=BUILDING):
    """A subscription filter. "+" matches any one node or channel."""
    if channel != "+" and channel not in CHANNELS:
        raise ValueError(f"unknown channel {channel!r}")
    levels = [building, node, channel] + ([name] if name is not None else [])
    return "/".join([ROOT] + [x if x == "+" else _level(x) for x in levels])


def alert_filters(node="+", building=BUILDING):
    """Everything that raises or ends an alarm: node alerts, tank flags, gateway commands."""
    return [topic_filter(node, c, building=building) for c in ("alert", "flag", "cmd")]


def sensor_filters(node="+", building=BUILDING):
    """Live readings and 15-minute summaries. On the gateway, edge-based nodes send only summaries."""
    return [topic_filter(node, c, building=building) for c in ("sensor", "summary")]


def parse(text):
    """A topic string -> Topic, or None if it is not one of ours."""
    parts = text.split("/")
    if len(parts) not in (5, 6) or "/".join(parts[:2]) != ROOT or parts[4] not in CHANNELS or not all(parts):
        return None
    return Topic(parts[2], parts[3], parts[4], parts[5] if len(parts) == 6 else None)


# --- payloads ---
def _clean(value):
    """JSON has no NaN or infinity: leave such entries out of objects, and use null in lists."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if not (isinstance(v, float) and not math.isfinite(v))}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return None if isinstance(value, float) and not math.isfinite(value) else value


def encode(payload):
    """Payload dict -> UTF-8 JSON with the spec version added."""
    return json.dumps({"v": VERSION, **_clean(payload)}, separators=(",", ":"), allow_nan=False).encode()


def decode(data):
    """Received bytes -> payload dict, or None if it is not a payload of this version."""
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) and payload.get("v") == VERSION else None


def message_payload(message):
    """A node message or gateway command (the dicts used in edge/ and gateway/) -> the payload
    for its topic. Number "s", time "t" and the packet fields; node and kind are in the topic.
    Extra keys such as the SD queue's ts_iso are left out."""
    payload = {k: message[k] for k in ("s", "t", *FIELDS) if k in message}
    _check_names(KIND_CHANNEL[message["k"]], payload)
    return payload


def payload_message(channel, node, payload):
    """The reverse: a topic and its payload -> the message dict hal.packet.pack() takes."""
    kind = CHANNEL_KIND[channel]
    if node not in NAMES:
        raise ValueError(f"unknown node {node!r}")
    _check_names(channel, payload)
    return {"k": kind, "n": node, **{k: payload[k] for k in ("s", "t", *FIELDS) if k in payload}}


def _check_names(channel, payload):
    for key, allowed, channels in (("a", ALERTS, ("alert", "cmd")), ("f", FLAGS, ("flag",))):
        if channel in channels and payload.get(key) not in allowed:
            raise ValueError(f"{channel}: {key}={payload.get(key)!r} is not a known name")


def sensor_payload(readings, t):
    """One set of readings. A missing or non-numeric reading is left out."""
    values = {short: readings[name] for name, short in SENSOR_FIELDS.items()
              if isinstance(readings.get(name), (int, float)) and math.isfinite(readings[name])}
    return {"t": t, **values}


# --- publishing and subscribing ---
class Publisher:
    """Publishes by the rules above. It never raises: MQTT is a copy for subscribers, and a
    broken broker must not stop a buzzer, a queue or an ACK. Keeps the latest retained payload of
    each topic, so they can be sent again to a broker that restarted and lost them."""

    def __init__(self, transport, building=BUILDING, log=print):
        self.transport, self.building, self.log = transport, building, log
        self.retained, self.latest = {}, {}     # topic -> (data, channel) / payload last sent

    def publish(self, node, channel, payload, name=None):
        try:
            where, data = topic(node, channel, name, self.building), encode(payload)
            how = CHANNELS[channel]
            if how.retain:
                self.retained[where] = (data, how)
            self.transport.publish(where, data, how.qos, how.retain, how.expiry_s)
            return True
        except Exception as e:          # whatever went wrong, the caller carries on
            self.log(f"comms: {channel} for {node} not published: {e!r}")
            return False

    def publish_changed(self, node, channel, payload, name=None, t=None):
        """Retained state: publish only when it differs from what was sent last. "t" is added
        but not compared, so the time passing is not a change."""
        key = (node, channel, name)
        if self.latest.get(key) != payload and self.publish(node, channel, {**payload, "t": t}, name):
            self.latest[key] = payload

    def republish_retained(self):
        """Call after (re)connecting. Short-lived readings are not repeated: they may be stale."""
        for where, (data, how) in list(self.retained.items()):
            if how.expiry_s is None:
                try:
                    self.transport.publish(where, data, how.qos, True, None)
                except Exception as e:
                    self.log(f"comms: {where} not republished: {e!r}")


def dispatcher(handler, log=print):
    """For subscribers: turns (topic string, bytes) into handler(Topic, payload dict). Anything
    that is not a valid message of this spec is ignored; an error in handler is logged."""
    def on_message(where, data):
        parsed, payload = parse(where), decode(data)
        if parsed is None or payload is None:
            return
        try:
            handler(parsed, payload)
        except Exception as e:
            log(f"comms: handler failed for {where}: {e!r}")
    return on_message


def observe(obj, method, after):
    """Run after(result, *args) each time obj.method returns, without changing what it does.
    The method's own errors pass through; errors in `after` are logged, never raised."""
    original = getattr(obj, method)

    def wrapped(*args, **kwargs):
        result = original(*args, **kwargs)
        try:
            after(result, *args, **kwargs)
        except Exception as e:
            print(f"comms: could not publish after {method}: {e!r}")
        return result

    setattr(obj, method, wrapped)


if __name__ == "__main__":
    from hal.packet import pack, sim_key, unpack

    assert topic("B-203", "alert") == "aqua/v1/B/B-203/alert"
    assert topic(GATEWAY, "event", "PIPE_LEAK") == "aqua/v1/B/gateway/event/PIPE_LEAK"
    assert parse("aqua/v1/B/gateway/event/PIPE_LEAK") == Topic("B", "gateway", "event", "PIPE_LEAK")
    assert alert_filters()[0] == "aqua/v1/B/+/alert" and sensor_filters("B-203")[0] == "aqua/v1/B/B-203/sensor"
    for bad in ("aqua/v2/B/B-203/alert", "aqua/v1/B/B-203/nonsense", "aqua/v1/B//alert", "x"):
        assert parse(bad) is None, bad
    for bad in ("B/203", "+", "#", ""):
        try:
            topic(bad, "alert")
        except ValueError:
            continue
        raise AssertionError(f"accepted node {bad!r}")

    # every message with a topic survives MQTT and then the radio unchanged
    key = sim_key("B-203")
    for m in ({"k": "ALERT", "n": "B-203", "s": 8, "t": 93660, "a": "LEAK_NIGHT_FLOW", "flow": 0.0512},
              {"k": "FLAG", "n": "B-TANK", "s": 5, "t": 180, "f": "SUPPLY_LOW", "level": 0.312, "inflow": 0.0},
              {"k": "SUM", "n": "B-203", "s": 7, "t": 93600, "i": 104, "vol_l": 12.3456, "p_min": 10.4321},
              {"k": "CMD", "n": "B-203", "s": 1701, "t": 93000, "a": "PIPE_LEAK", "over": 1}):
        payload = decode(encode(message_payload({**m, "ts_iso": "local only"})))
        back = payload_message(KIND_CHANNEL[m["k"]], m["n"], payload)
        assert back == m and unpack(pack(back, key), lambda n: key) == m, (back, m)
    try:
        message_payload({"k": "ALERT", "n": "B-203", "s": 1, "t": 0, "a": "MADE_UP"})
    except ValueError:
        pass
    else:
        raise AssertionError("accepted an unknown alert")

    nan = float("nan")
    assert decode(encode(sensor_payload({"flow_lps": 0.2, "pressure_m": nan, "ph": None, "other": 1}, 60))) \
        == {"v": 1, "t": 60, "flow": 0.2}
    assert decode(encode({"x": [nan, 1.0], "y": {"z": nan}})) == {"v": 1, "x": [None, 1.0], "y": {}}
    assert decode(b"not json") is None and decode(b"[1]") is None and decode(b'{"v":2}') is None

    class Broken:
        def publish(self, *args):
            raise OSError("broker gone")

    lines = []
    assert Publisher(Broken(), log=lines.append).publish("B-203", "alert", {"s": 1}) is False and lines
    got = []
    receive = dispatcher(lambda where, payload: got.append((where.node, payload["s"])), log=lines.append)
    receive(topic("B-203", "alert"), encode({"s": 1}))
    receive(topic("B-203", "alert"), b"junk")
    receive("elsewhere", encode({"s": 2}))
    assert got == [("B-203", 1)]
    print("comms self-check passed")
