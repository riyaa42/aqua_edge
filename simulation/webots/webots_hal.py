"""Hardware layer for running the firmware inside Webots (the Pi Zero gets its own version).

Sensors come from the EPANET scenario (simlink.SensorFeed), the radio is a Webots
Emitter/Receiver pair, the status light is a Webots LED, and the SD card is a file.
"""
import json
import os
from simlink import SensorFeed, load_outages, link_up

TIME_STEP_MS = 128            # Webots time per firmware step; one step = one minute of water time
ROOT = os.path.dirname(os.path.abspath(__file__))
RUN_DIR = os.path.join(ROOT, "results", "webots_run")
LED_INDEX = {"green": 1, "yellow": 2, "red": 3}


class WebotsHAL:
    def __init__(self, robot):
        os.makedirs(RUN_DIR, exist_ok=True)
        self.name = robot.getName()
        self.feed, self.outages = SensorFeed(), load_outages()
        self.emitter = robot.getDevice("emitter")
        self.receiver = robot.getDevice("receiver")
        self.receiver.enable(TIME_STEP_MS)
        self.led_dev = robot.getDevice("led")
        self.t = 0.0
        self.storage_path = os.path.join(RUN_DIR, f"{self.name}_sd.jsonl")
        self.valve_path = os.path.join(RUN_DIR, f"{self.name}_valve.jsonl")
        for p in (self.storage_path, self.valve_path):      # every simulation run starts clean
            if os.path.exists(p):
                os.remove(p)

    def now(self):
        return self.t

    def link_ok(self):
        return link_up(self.name, self.t, self.outages)

    def read(self):
        return self.feed.read(self.name, self.t)

    def send(self, msg, at_s=None, cad=False):      # timing ignored: Webots radio has no airtime
        if self.link_ok():
            self.emitter.send(json.dumps(msg, separators=(",", ":")))

    def receive(self):
        msgs = []
        while self.receiver.getQueueLength() > 0:
            data = self.receiver.getString() if hasattr(self.receiver, "getString") else self.receiver.getData().decode()
            self.receiver.nextPacket()
            if self.link_ok():
                msgs.append(json.loads(data))
        return msgs

    def set_valve(self, closed, reason):
        with open(self.valve_path, "a") as f:
            f.write(json.dumps({"t": self.t, "node": self.name, "closed": closed, "reason": reason}) + "\n")

    def led(self, colour):
        self.led_dev.set(LED_INDEX[colour])
