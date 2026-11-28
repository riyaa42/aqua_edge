"""Aqua Edge radio packets: compact binary, encrypted and authenticated. Used by the nodes and the
gateway. Needs the cryptography package.

Plain LoRa has no security, so every packet is protected here. Radio drivers, and the ESP8266, only
ever see the finished bytes and need no keys.

    every packet but BEACON
        header  type(1) node(1) counter(4) time(4)      10 bytes, readable, authenticated
        sealed  AES-SIV: tag(16) + encrypted(format version(1) + fields)
                fields, each: field number(1) + value   (ALERT, FLAG, SUM, CMD; none for ACK, GOT)
    BEACON
        header  as above                                10 bytes, readable
        tag     first 4 bytes of HMAC-SHA256(beacon key, header)

- Node packets: node = sender, counter = the node's sequence number, time = node clock.
- ACK: node = the node being answered, counter = the sequence number it acknowledges,
  time = gateway clock ("gt"). Made with that node's key, so nobody else can fake it.
- Each node shares one secret 16-byte key with the gateway: hal.key(name). The AES-SIV key is
  derived from it with HKDF-SHA256 (SIV_INFO), which adds no strength to the secret.
- AES-SIV (RFC 5297) needs no nonce. That matters: one counter value is used by several kinds
  of packet, and a node's counter starts again if its SD card is wiped. The worst a repeat can
  do is show that two packets were exactly the same.
- The gateway's BEACON goes to everyone and holds only its clock and boot number. It is signed,
  not encrypted, with a building-wide beacon key, hal.key("GATEWAY"), and can carry no fields.
  A beacon only makes nodes send (it never deletes anything), so a leaked beacon key could
  cause extra transmissions, no more.
- NOT hidden: which node, the kind of packet (an ALERT is visibly an ALERT), the counter, the
  time and the length.
- NOT prevented: sending an old packet again. The node and the gateway check for that.
- No forward secrecy: whoever gets a node's key can also read that node's recorded packets,
  and can pose as that node or as the gateway towards it.

    python3 -m hal.packet      # self-check
"""
import functools
import hashlib
import hmac
import json
import re
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESSIV
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

BEACON_TAG_BYTES = 4                            # LoRaWAN also uses a 4-byte check
SIV_BYTES = 16                                  # AES-SIV's tag, in front of the encrypted bytes
SIV_INFO = b"aqua-edge packet encryption v1"    # HKDF label: both ends must use the same
FORMAT = b"\x01"                               # first encrypted byte; also means there is always one
HEADER = struct.Struct(">BBII")
KINDS = ["", "ALERT", "FLAG", "SUM", "ACK",                # up: ALERT FLAG SUM GOT; down: ACK
         "BEACON", "CMD", "GOT"]                                    # down: BEACON CMD (append only)
GATEWAY_CLOCK = ("ACK", "BEACON")           # kinds whose time field is the gateway's clock, "gt"
# A packet names its node by one number. Building B's names are built in: a name's number is its
# position in NAMES (append only). Any other name needs a number from CUSTOM_IDS, set in the building
# tree as "radio_id" (gateway/topology.py) and registered with add_names() on both ends.
NAMES = ["GATEWAY", "B-TANK"] + [f"B-{floor}{flat:02d}" for floor in "G1234" for flat in range(1, 5)]
CUSTOM_IDS = range(128, 256)            # below 128 is left for more built-in names
# a custom name is also an MQTT topic level and part of a file name
CUSTOM_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}")
_numbers = {name: number for number, name in enumerate(NAMES)}
_names = dict(enumerate(NAMES))
ALERTS = ["LEAK_NIGHT_FLOW", "LEAK_CONTINUOUS_FLOW", "LEAK_PRESSURE_DROP", "ABNORMAL_CONSUMPTION", "ABNORMAL_SUPPLY",
          "QUALITY_RUNOFF", "QUALITY_CHEMICAL", "QUALITY_LIMIT_VIOLATION", "QUALITY_CLEARED",
          "QUALITY_TEMPORAL_ANOMALY", "QUALITY_TEMPORAL_CLEARED",
          "UNUSUAL_NIGHT_CONSUMPTION", "PROLONGED_ZERO_FLOW", "SUDDEN_CONSUMPTION_CHANGE",
          "FLAT_LEAK_CLEARED", "CONSUMPTION_CLEARED", "ZERO_FLOW_CLEARED", "SUPPLY_FLOW_CLEARED",
          # gateway -> node (CMD): what only the gateway can know, from several nodes together
          "PIPE_LEAK", "PIPE_LEAK_CLEARED", "PIPE_LEAK_CAUSE", "PIPE_LEAK_CAUSE_CLEARED",
          "SUPPLY_EVENT", "LEAK_TANK_DRAIN", "SUPPLY_UNCERTAIN", "SUPPLY_CLEARED"]          # append only
FLAGS = ["SUPPLY_LOW", "SUPPLY_RECOVERED"]                      # append only
VIOLATIONS = ["pH", "TDS", "TURBIDITY"]     # "viol" is a bit mask of broken limits
# field name -> how its value is packed; its number on air is its position here; "B" = index into ALERTS/FLAGS
FIELDS = {"a": "B", "f": "B", "i": ">I", "vol_l": ">f", "p_min": ">f", "in_l": ">f", "level_m": ">f",
          "flow": ">f", "ph": ">f", "tds": ">f", "level": ">f", "inflow": ">f", "score": ">f", "p_mean": ">f",
          "turb": ">f", "viol": ">B", "over": ">B"}      # append only: a field's position is its number on air
FIELD_NAMES = list(FIELDS)
CODES = {"a": ALERTS, "f": FLAGS}
HEADER_KEYS = {"k", "n", "s", "t", "gt"}


def check_custom_name(name, number):
    """Raises ValueError unless (name, number) can be a custom node."""
    if name in NAMES:
        raise ValueError(f"{name} is built in: it has its number already")
    if not CUSTOM_NAME.fullmatch(name) or name.upper() == "GATEWAY":
        raise ValueError(f"{name!r}: a node name is 1 to 32 letters, digits, '_', '.' or '-', starting with a "
                         "letter or digit, and is not GATEWAY")
    if type(number) is not int or number not in CUSTOM_IDS:
        raise ValueError(f"{name}: radio_id must be a whole number from {CUSTOM_IDS[0]} to {CUSTOM_IDS[-1]}")


def add_names(custom):
    """Register custom nodes, {name: number}, before packing, reading packets or loading keys.
    Every device must use the same numbers. Adding the same ones again is fine; giving a name or a
    number a second meaning raises ValueError, and then nothing is added."""
    numbers, names = dict(_numbers), dict(_names)
    for name, number in custom.items():
        if name not in NAMES:
            check_custom_name(name, number)
        if numbers.get(name, number) != number or names.get(number, name) != name:
            raise ValueError(f"{name} = {number} clashes with {name} = {numbers.get(name)} "
                             f"or {names.get(number)} = {number}")
        numbers[name], names[number] = number, name
    _numbers.update(numbers)
    _names.update(names)


def node_number(name):
    """The number a node's packets carry. ValueError if the name is not known here."""
    if name not in _numbers:
        raise ValueError(f"unknown node {name!r}: not built in, and not added with add_names()")
    return _numbers[name]


def is_node(name):
    return name in _numbers


def violation_mask(violations):
    return sum(1 << VIOLATIONS.index(v) for v in violations)


def sim_key(name):
    """Simulation only: anyone who knows a node's name can work out its key. Real hardware
    uses load_keys()."""
    return hashlib.sha256(b"aqua-edge-simulation:" + name.encode()).digest()[:16]


def load_keys(path):
    """Real nodes and gateway: secret keys from a JSON file on the SD card,
    {"GATEWAY": "<32 hex>", "B-101": "<32 hex>", ...} -> {name: 16 bytes}. Never committed
    (*keys.json is in .gitignore)."""
    with open(path) as f:
        keys = {n: bytes.fromhex(h) for n, h in json.load(f).items()}
    bad = sorted(n for n, k in keys.items() if not is_node(n) or len(k) != 16)
    if bad:
        raise ValueError(f"bad entries in {path}: {bad} (unknown node or key not 16 bytes)")
    return keys


def _beacon_tag(key, data):
    return hmac.new(key, data, hashlib.sha256).digest()[:BEACON_TAG_BYTES]


@functools.lru_cache(maxsize=64)
def _cipher(key):
    return AESSIV(HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=SIV_INFO).derive(key))


def pack(m, key):
    """Message dict -> the bytes to send: encrypted, except a BEACON, which is only signed.
    Unknown fields or alert names fail loudly."""
    beacon = m["k"] == "BEACON"
    if beacon != (m["n"] == "GATEWAY"):
        raise ValueError("a BEACON is from GATEWAY, and GATEWAY sends nothing else")
    t = m["gt"] if m["k"] in GATEWAY_CLOCK else m["t"]
    header = HEADER.pack(KINDS.index(m["k"]), node_number(m["n"]), m["s"], round(t))
    body = b""
    for name in FIELD_NAMES:
        if name in m:
            v = CODES[name].index(m[name]) if name in CODES else m[name]
            body += bytes([FIELD_NAMES.index(name)]) + struct.pack(FIELDS[name], v)
    extra = set(m) - HEADER_KEYS - set(FIELDS)
    if extra:
        raise ValueError(f"no packet field for {sorted(extra)}")
    if beacon:
        if body:
            raise ValueError("a BEACON is not encrypted, so it carries no fields")
        return header + _beacon_tag(key, header)
    return header + _cipher(bytes(key)).encrypt(FORMAT + body, [header])


def peek(raw):
    """Read the header without checking the tag: who, what, which number. Only for routing
    and logs; never act on it."""
    kind, node, counter, t = HEADER.unpack_from(raw)
    return {"k": KINDS[kind], "n": _names[node], "s": counter, ("gt" if KINDS[kind] in GATEWAY_CLOCK else "t"): t}


def _open(raw, kind, node, key):
    """The fields' bytes if the packet is genuine and unchanged, else None."""
    header, rest = raw[:HEADER.size], raw[HEADER.size:]
    if (kind == "BEACON") != (node == "GATEWAY"):
        return None
    if kind == "BEACON":                    # exactly header and tag
        return b"" if hmac.compare_digest(_beacon_tag(key, header), rest) else None
    if len(rest) <= SIV_BYTES:
        return None
    try:
        plain = _cipher(bytes(key)).decrypt(rest, [header])
    except InvalidTag:
        return None
    return plain[len(FORMAT):] if plain.startswith(FORMAT) else None


def unpack(raw, key_for):
    """Received bytes -> message dict, or None if the packet is broken, changed, made with another
    key, or in another format.
    key_for(name) gives the key for the node named in the header (None: not ours to read)."""
    try:
        m = peek(raw)
        key = key_for(m["n"])
        body = None if key is None else _open(bytes(raw), m["k"], m["n"], key)
        if body is None:
            return None
        pos = 0
        while pos < len(body):
            name = FIELD_NAMES[body[pos]]
            (v,) = struct.unpack_from(FIELDS[name], body, pos + 1)
            pos += 1 + struct.calcsize(FIELDS[name])
            m[name] = CODES[name][v] if name in CODES else round(v, 4) if isinstance(v, float) else v
        return m
    except (IndexError, KeyError, struct.error):    # KeyError: a node number unknown here
        return None


if __name__ == "__main__":
    import json
    k203, k101 = sim_key("B-203"), sim_key("B-101")
    keys = lambda n: sim_key(n)
    msgs = [{"n": "B-203", "s": 7, "t": 93600, "k": "SUM", "i": 104, "vol_l": 12.3456, "p_min": 10.4321},
            {"n": "B-203", "s": 8, "t": 93660, "k": "ALERT", "a": "LEAK_NIGHT_FLOW", "flow": 0.0512},
            {"n": "B-TANK", "s": 3, "t": 60, "k": "ALERT", "a": "QUALITY_CHEMICAL", "ph": 9.12, "tds": 700},
            {"n": "B-TANK", "s": 4, "t": 120, "k": "ALERT", "a": "QUALITY_CLEARED"},
            {"n": "B-TANK", "s": 9, "t": 130, "k": "ALERT", "a": "QUALITY_LIMIT_VIOLATION", "ph": 7.1, "tds": 300,
             "turb": 12.5, "viol": violation_mask(["TURBIDITY"])},
            {"n": "B-TANK", "s": 5, "t": 180, "k": "FLAG", "f": "SUPPLY_LOW", "level": 0.312, "inflow": 0.0},
            {"n": "B-TANK", "s": 6, "t": 240, "k": "SUM", "i": 5, "vol_l": 300.5, "in_l": 0.0, "level_m": 1.4991},
            {"k": "ACK", "n": "B-203", "s": 8, "gt": 93720}]
    msgs[-1:-1] = [{"n": "B-203", "s": 10, "t": 300, "k": "ALERT", "a": "FLAT_LEAK_CLEARED"},
                   {"n": "B-TANK", "s": 11, "t": 360, "k": "FLAG", "f": "SUPPLY_RECOVERED", "level": 1.5}]
    msgs[-1:-1] = [{"n": "GATEWAY", "s": 1700, "k": "BEACON", "gt": 93720},
                   {"n": "B-203", "s": 1701, "t": 93000, "k": "CMD", "a": "PIPE_LEAK", "over": 1},
                   {"n": "B-203", "s": 1701, "t": 93721, "k": "GOT"}]
    for m in msgs:
        raw = pack(m, keys(m["n"]))
        assert unpack(raw, keys) == m, (unpack(raw, keys), m)          # same message back
        for i in range(len(raw) * 8):                                   # any one bit changed
            bad = bytearray(raw)
            bad[i // 8] ^= 1 << (i % 8)
            assert unpack(bytes(bad), keys) is None, (m["k"], i)
        for n in range(len(raw)):                                       # cut short anywhere
            assert unpack(raw[:n], keys) is None, (m["k"], n)
        assert unpack(raw + b"\0", keys) is None
        print(f"{m['k']:5} {len(raw):3} bytes (JSON {len(json.dumps(m, separators=(',', ':'))):3})")
    ack = pack(msgs[-1], k203)
    assert unpack(ack, lambda n: k101 if n == "B-203" else None) is None   # signed with the wrong key
    assert unpack(ack, lambda n: None) is None                           # not ours to read
    assert len(ack) == HEADER.size + SIV_BYTES + 1                       # nothing to say: only the format byte
    assert unpack(pack(msgs[-1], k101), keys) is None

    # the fields cannot be read off the air; the same message gives the same bytes, any change others
    alert = msgs[1]
    sent = pack(alert, k203)
    assert struct.pack(">f", alert["flow"]) not in sent and sent == pack(alert, k203)
    for other in ({**alert, "s": 99}, {**alert, "flow": 0.06}, {**alert, "k": "FLAG"}):
        assert pack(other, k203)[HEADER.size:] != sent[HEADER.size:], other
    assert sent[HEADER.size + SIV_BYTES:] != pack({**alert, "s": 99}, k203)[HEADER.size + SIV_BYTES:]

    # the wire format, checked from the documented recipe and not through pack's own helpers
    prk = hmac.new(bytes(32), k203, hashlib.sha256).digest()             # HKDF-SHA256, no salt
    siv = AESSIV(hmac.new(prk, b"aqua-edge packet encryption v1\x01", hashlib.sha256).digest())
    head = HEADER.pack(KINDS.index("ALERT"), NAMES.index("B-203"), alert["s"], alert["t"])
    body = (bytes([FIELD_NAMES.index("a")]) + bytes([ALERTS.index("LEAK_NIGHT_FLOW")])
            + bytes([FIELD_NAMES.index("flow")]) + struct.pack(">f", alert["flow"]))
    assert sent == head + siv.encrypt(b"\x01" + body, [head])

    # rejected: an unknown format version, an old signed-only packet, a format switched
    assert unpack(head + siv.encrypt(b"\x02" + body, [head]), keys) is None
    assert unpack(head + body + hmac.new(k203, head + body, hashlib.sha256).digest()[:4], keys) is None
    assert unpack(head + hmac.new(k203, head, hashlib.sha256).digest()[:4], keys) is None
    gk = sim_key("GATEWAY")
    for kind, node, key in (("ACK", "GATEWAY", gk), ("BEACON", "B-203", k203)):
        head = HEADER.pack(KINDS.index(kind), NAMES.index(node), 1, 1)
        for forged in (head + _beacon_tag(key, head), head + _cipher(key).encrypt(FORMAT, [head])):
            assert unpack(forged, keys) is None, (kind, node)
    for wrong in ({"k": "BEACON", "n": "GATEWAY", "s": 1, "gt": 1, "flow": 1.0},
                  {"k": "BEACON", "n": "B-203", "s": 1, "gt": 1}, {"k": "SUM", "n": "GATEWAY", "s": 1, "t": 1}):
        try:
            pack(wrong, gk)
            raise AssertionError(f"packed {wrong}")
        except ValueError:
            pass
    rng = __import__("random").Random(1)                                  # noise never crashes it
    for _ in range(3000):
        assert unpack(rng.randbytes(rng.randrange(0, 80)), keys) is None
    everything = {"k": "SUM", "n": "B-203", "s": 1, "t": 1,
                  **{f: ALERTS[0] if f == "a" else FLAGS[0] if f == "f" else 1 for f in FIELDS}}
    assert unpack(pack(everything, k203), keys) == everything and len(pack(everything, k203)) <= 255
    import os
    import secrets
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), "aqua_keys.json")
    good = {n: secrets.token_hex(16) for n in NAMES}
    for content, ok in ((good, True), ({**good, "B-101": "abcd"}, False), ({"B-999": "00" * 16}, False)):
        with open(path, "w") as f:
            json.dump(content, f)
        try:
            assert load_keys(path)["B-101"] == bytes.fromhex(good["B-101"]) and ok
        except ValueError:
            assert not ok
    print("packet self-check passed")
