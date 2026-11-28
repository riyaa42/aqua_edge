"""The cloud-based solution's server side (the control case, planning/baselines.md, Baseline A): all
the deciding that an edge-based node does, done here for nodes that only stream readings
(edge/cloud_based.py).

    thin nodes <--RAW / SET--> CloudHost [one "brain" per node] <--in memory--> gateway.core.Gateway

A brain is the edge-based node itself (edge/aqua_edge.py, FlatNode or TankNode), unchanged apart
from what follows from not being on the device. So both solutions run the same detectors and rules.
CloudHost is the radio of an unchanged Gateway: brains and gateway exchange the same packets as an
edge-based building, in memory, so everything beyond the gateway (dashboard, MQTT, orders) is as it
was. Only RAW readings, SET, ACK, GOT and the beacon go on air.

What differs because the node is elsewhere:
- a brain's clock is the time of the reading it is taking in, so late readings keep their times;
- whether the valve is shut is what the node last reported, not what was just decided;
- the node reports its valve; a brain's own valve reports are not passed on;
- the gateway's commands reach a brain only while its node is being heard, so the gateway repeats
  and expires them as it would for an edge-based node;
- a trial reopening counts only readings taken with the valve really open.
"""
import json
import os
import random

from edge.aqua_edge import REOPEN_AFTER_S, FlatNode, Node, TankNode
from edge.store import save_atomic
from hal.link import MessageLink
from hal.lora433 import AirtimeBudget, airtime_s, retry_wait
from hal.packet import (DEV_HAND, DEV_PRIME, DEV_SHUT, LAMPS, READING_FIELDS, SET_CLOSE, SET_OPEN,
                        SET_OPEN_ORDER, peek, unpack)

CONNECTED_S = 90            # a node heard within this long is there: it sends a reading a minute
BRAIN_POLL_S = 0.5          # how often a brain with nothing waiting looks at its timers
REPORTS = ("VALVE_CLOSED", "VALVE_OPENED", "VALVE_FAULT")


class BrainBoard:
    """A brain's `hal` (see edge/aqua_edge.py): readings from the node, packets to and from the
    gateway in memory, and the valve, light and buzzer only as what the node is to be told."""

    def __init__(self, host, name, storage_path):
        self.host, self.name, self.storage_path = host, name, storage_path
        self.at = None                      # the node's time of the reading being taken in
        self.sample_t, self.readings = 0.0, {}
        self.inbox, self.lamp, self.beeps = [], ("green", 0), 0

    def now(self):
        return self.host.clock() if self.at is None else self.at

    def monotonic(self):
        return self.host.monotonic()

    def sample_time(self):
        return self.sample_t

    def read(self):
        return dict(self.readings)

    def key(self, name):
        return self.host.keys[name]

    def send(self, raw, cad=False):
        self.host.to_core.append(raw)
        return True

    def receive(self):
        got, self.inbox = self.inbox, []
        return got

    def led(self, colour):
        self.lamp = (colour, 0)

    def led_pattern(self, colour, blinks):
        self.lamp = (colour, blinks)

    def buzzer(self, code):
        self.beeps += 1

    def set_valve(self, closed):
        return True                         # the node does it, and reports how it went

    def log(self, text):
        self.host.log(f"cloud: {text}")


class _InMemory:
    """No transmit-time limit between a brain and the gateway: they are not on air."""

    def take(self, now, seconds, urgent):
        return True

    def refund_last(self):
        pass


class _Brain:
    """What changes when a node's code runs on the server. Mixed in before FlatNode or TankNode."""

    def __init__(self, name, hal, record):
        self.vrev = record.get("vrev", 0)           # the newest valve decision; 0: none yet
        self.by_order, self.decided_at = record.get("by_order", False), record.get("decided_at", 0)
        self.device_shut = record.get("shut", False)
        super().__init__(name, hal)                 # ends with start()
        self.airtime = _InMemory()

    def start(self):
        fault = self.valve["fault"]                 # only the node can say it has cleared
        self.valve["fault"] = False
        super().start()
        self.valve["fault"] = fault

    @property
    def shut(self):
        return self.has_valve and self.device_shut

    def drive_valve(self):
        return True

    def set_valve(self, closed, why):
        """Every decision about the valve is numbered, for the node to carry out once."""
        super().set_valve(closed, why)
        self.vrev = max(self.vrev + 1, int(self.hal.host.clock()))   # still grows if state is lost
        self.by_order = not closed and why == "gateway"
        self.decided_at = max(0, int(round(self.hal.now())))

    def emit(self, kind, **payload):
        if not (kind == "ALERT" and payload.get("a") in REPORTS):
            super().emit(kind, **payload)

    def device_report(self, code):
        """The node says what its valve did. Passed on to the gateway as an edge-based node's would be."""
        failed = code == "VALVE_FAULT"
        if failed != self.valve["fault"]:
            self.valve["fault"] = failed
            self.save_open()
        Node.emit(self, "ALERT", a=code)
        if failed and self.trial["run"] and not self.valve["closed"]:
            self.fail_trial("the valve did not open")

    def try_reopening(self, td, r, new_reading):
        # readings taken before the node opened its valve show no flow, which would pass any trial
        if not (self.trial["run"] and self.device_shut):
            super().try_reopening(td, r, new_reading)


class FlatBrain(_Brain, FlatNode):
    pass


class TankBrain(_Brain, TankNode):
    pass


class _Device:
    """One thin node as the server knows it."""

    def __init__(self, name, record_path):
        self.name, self.path = name, record_path
        self.record = {}
        if os.path.exists(record_path):
            with open(record_path) as f:
                self.record = json.load(f)
        self.seq = self.record.get("seq", 0)                # the newest message taken in
        self.set_number = self.record.get("set", 0)
        self.pending = self.record.get("pending")            # the SET the node has not confirmed
        self.wanted = self.record.get("wanted")             # what the newest SET asks for
        self.brain, self.heard, self.poll_at = None, None, 0.0
        self.send_at, self.tries, self.beeps_told = 0.0, 0, 0


class CloudHost:
    """air: the real radio (send(raw, cad), receive()). folder: where the brains keep their state.
    limit_air: keep to the transmit-time limit; off only where nothing goes on air (UDP nodes).
    To the Gateway, this object is the radio."""

    def __init__(self, air, keys, topology, folder, clock, monotonic, reopen_after_s=REOPEN_AFTER_S,
                 trial_reopen=False, log=print, limit_air=True):
        self.limit_air, self.refused = limit_air, 0
        self.air, self.keys, self.clock, self.monotonic, self.log = air, keys, clock, monotonic, log
        self.link = MessageLink(air, keys.get)
        self.airtime = AirtimeBudget()              # what really goes on air
        self.rng = random.Random(0)
        self.to_core, self.odd = [], set()
        os.makedirs(folder, exist_ok=True)
        self.devices = {}
        for name in sorted(topology.devices):
            device = _Device(name, os.path.join(folder, f"{name}.cloud.json"))
            kind = TankBrain if name == topology.tank else FlatBrain
            device.brain = kind(name, BrainBoard(self, name, os.path.join(folder, f"{name}.jsonl")), device.record)
            device.brain.reopen_after_s, device.brain.trial_reopen = reopen_after_s, trial_reopen
            self.devices[name] = device

    # --- the Gateway's radio ---
    def receive(self):
        for item in self.air.receive():
            self._from_air(item[1] if isinstance(item, tuple) else item)
        mono = self.monotonic()
        for device in self.devices.values():
            if device.brain.hal.inbox or mono >= device.poll_at:
                self._poll(device)
            self._send_set(device)
        out, self.to_core = self.to_core, []
        return [(self.clock(), raw) for raw in out]

    def send(self, raw, cad=False):
        """A packet from the Gateway: the beacon goes on air and to every brain; an ACK or a
        command to one brain. A command is lost, as on air, while its node is not being heard."""
        header = peek(raw)
        if header["k"] == "BEACON":
            self._on_air(raw, cad)
            for device in self.devices.values():
                device.brain.hal.inbox.append(raw)
        else:
            device = self.devices.get(header["n"])
            if device and (header["k"] == "ACK" or self.connected(device)):
                device.brain.hal.inbox.append(raw)
        return True

    def close(self):
        for device in self.devices.values():
            device.brain.save_state()
            self._save(device)
        getattr(self.air, "close", lambda: None)()

    # --- the nodes ---
    def connected(self, device):
        return device.heard is not None and self.monotonic() - device.heard <= CONNECTED_S

    def _from_air(self, raw):
        message = unpack(raw, self.keys.get)
        if message is None:
            self.to_core.append(raw)        # the Gateway counts it: BAD_PACKET
            return
        device = self.devices.get(message["n"])
        if device is None:
            return
        if message["k"] == "RAW":
            self._take_in(device, message)
        elif message["k"] == "GOT":
            device.heard = self.monotonic()
            if device.pending and message["s"] == device.pending["s"]:
                device.pending = None
                self._save(device)
        elif (device.name, message["k"]) not in self.odd:
            self.odd.add((device.name, message["k"]))
            self.log(f"cloud: {device.name} sends {message['k']}: is it running the edge-based program? ignored")

    def _take_in(self, device, m):
        ack = {"k": "ACK", "n": device.name, "s": m["s"], "gt": self.clock()}
        if m["s"] <= device.seq:            # already taken in (the ACK was lost), or replayed
            self._answer(ack)
            return
        device.heard = self.monotonic()
        brain, hal = device.brain, device.brain.hal
        brain.radio_poll()                  # first what is waiting from the gateway, at the server's time
        hal.at = m["t"]
        flags = m.get("dev", 0)
        brain.device_shut = bool(flags & DEV_SHUT)
        if flags & DEV_HAND:
            brain.open_valve()
        if m.get("a") in REPORTS:
            brain.device_report(m["a"])
        if not (flags & DEV_HAND or "a" in m):
            hal.sample_t = float(m["st"])
            hal.readings = {sensor: m[field] for sensor, field in READING_FIELDS.items() if field in m}
            if flags & DEV_PRIME and brain.prime(hal.read()):
                brain.reading_at = hal.monotonic()      # learning readings are readings too
            else:
                brain.step()
        hal.at = None
        self._poll(device)
        # everything on disk before the ACK: the node deletes a message once it is confirmed
        brain.save_state()
        device.seq = m["s"]
        self._save(device)
        self._answer(ack)
        device.send_at, device.tries = 0.0, 0       # it can hear now: send what waits for it
        self._send_set(device)

    def _poll(self, device):
        wake = device.brain.radio_poll()
        mono = self.monotonic()
        device.poll_at = mono + BRAIN_POLL_S if wake is None else min(max(wake, mono), mono + BRAIN_POLL_S)
        self._update(device)

    # --- what each node is told ---
    def _update(self, device):
        """A new SET whenever the valve decision, the light or the buzzer changes. Only the newest
        is ever sent."""
        brain, hal = device.brain, device.brain.hal
        code = SET_CLOSE if brain.valve["closed"] else SET_OPEN_ORDER if brain.by_order else SET_OPEN
        wanted = [brain.vrev, code, brain.decided_at, LAMPS.index(hal.lamp)]
        beep = hal.beeps != device.beeps_told
        if wanted == device.wanted and not beep:
            return
        beep = beep or bool(device.pending and device.pending.get("beep"))
        device.beeps_told = hal.beeps
        device.wanted = wanted
        device.set_number = max(device.set_number + 1, int(self.clock()))
        device.pending = {"k": "SET", "n": device.name, "s": device.set_number, "t": self.clock(),
                          "vrev": wanted[0], "valve": wanted[1], "vt": wanted[2], "lamp": wanted[3],
                          "beep": int(beep)}
        device.send_at, device.tries = 0.0, 0
        self._save(device)

    def _send_set(self, device):
        mono = self.monotonic()
        if device.pending is None or mono < device.send_at:
            return
        if self.link.send(device.pending, admit=self._admit):
            device.send_at = mono + retry_wait(device.tries, True, self.connected(device), self.rng)
            device.tries += 1
        else:
            device.send_at = mono + 5

    def _save(self, device):
        brain = device.brain
        record = {"seq": device.seq, "set": device.set_number, "pending": device.pending, "wanted": device.wanted,
                  "vrev": brain.vrev, "by_order": brain.by_order, "decided_at": brain.decided_at,
                  "shut": brain.device_shut}
        if record != device.record:
            save_atomic(device.path, json.dumps(record))
            device.record = record

    # --- the real radio ---
    def _admit(self, seconds):
        if not self.limit_air or self.airtime.take(self.monotonic(), seconds, True):
            return True
        if not self.refused:
            self.log("cloud: the radio's transmit-time limit is reached: replies are waiting")
        self.refused += 1
        return False

    def _answer(self, message):
        self.link.send(message, admit=self._admit)

    def _on_air(self, raw, cad):
        if self._admit(airtime_s(len(raw))) and not self.air.send(raw, cad) and self.limit_air:
            self.airtime.refund_last()
