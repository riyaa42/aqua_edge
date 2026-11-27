"""Hardware layer for running the firmware inside Webots (the Pi Zero gets its own version).

Sensors come from the EPANET scenario (simlink.SensorFeed), the radio is a Webots
Emitter/Receiver pair, the status light is a Webots LED, and the SD card is a file.
"""
import glob
import os
from packet import sim_key
from simlink import SensorFeed, load_outages, link_up

TIME_STEP_MS = 128            # Webots time per firmware step; one step = one minute of water time
ROOT = os.path.dirname(os.path.abspath(__file__))
RUN_DIR = os.path.join(ROOT, "results", "webots_run")
LED_INDEX = {"green": 1, "yellow": 2, "red": 3}


class WebotsHAL:
    key = staticmethod(sim_key)

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
        for p in glob.glob(self.storage_path.replace(".jsonl", "*")):   # queue, baseline, seen: start clean
            os.remove(p)

    def now(self):
        return self.t

    def link_ok(self):
        return link_up(self.name, self.t, self.outages)

    def read(self):
        return self.feed.read(self.name, self.t)

    def send(self, msg, at_s=None, cad=False):      # timing ignored: Webots radio has no airtime
        if self.link_ok():
            self.emitter.send(msg)                  # signed binary packet (firmware/packet.py)

    def receive(self):
        msgs = []
        while self.receiver.getQueueLength() > 0:
            data = self.receiver.getBytes()         # ponytail: untested since Webots is parked
            self.receiver.nextPacket()
            if self.link_ok():
                msgs.append(bytes(data))
        return msgs

    def led(self, colour):
        self.led_dev.set(LED_INDEX[colour])
