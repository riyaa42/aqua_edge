"""Gateway server entry point, edge-based solution: nodes decide their own alerts, and the gateway
decides what needs several nodes' data (core.py). It reaches the nodes through the ESP8266 LoRa
modem (hal/gate_hal.py) and publishes what it hears and decides over MQTT (comms/).

    python3 -m gateway.edge_based --keys gateway_keys.json --data gateway-data --port /dev/ttyUSB0
    python3 -m gateway.edge_based ... --mqtt-host 127.0.0.1 --mqtt-user gateway    # password: MQTT_PASSWORD

Without --mqtt-host (or MQTT_HOST) it runs without MQTT. The radio never waits for the broker.
With Docker Compose, which also runs the broker: gateway/README.md. Not tested on real hardware.
"""
import argparse
import os
import signal
import threading
import time

from gateway.core import Gateway
from gateway.topology import BUILDING_B, Topology
from hal.gate_hal import open_radio
from hal.packet import load_keys

POLL_S = 0.02           # a packet that has arrived is handled within this long
MIN_WAIT_S = 0.005


class GatewayBoard:
    """What gateway.core.Gateway needs of its `hal`: clock, keys, state file and radio."""

    def __init__(self, radio, keys, storage_path, clock=time.time):
        self.radio, self.keys, self.storage_path, self.clock = radio, keys, storage_path, clock

    def now(self):
        return self.clock()         # UTC seconds. Nodes set their clocks from this

    def key(self, name):
        return self.keys.get(name)

    def send(self, raw, cad=False):
        return self.radio.send(raw, cad)

    def receive(self):
        return [raw for _, raw in self.radio.receive()]     # the radio also gives arrival times


def run(gateway, stop, log=print):
    """Step the gateway until `stop` is set: when it asks, and soon after any packet arrives.
    New decisions are logged."""
    logged = 0
    while not stop.is_set():
        wake = gateway.step()
        for event in gateway.events[logged:]:
            log(f"gateway: {event}")
        logged = len(gateway.events)
        stop.wait(max(MIN_WAIT_S, min(wake - gateway.hal.now(), POLL_S)))


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Run the Aqua Edge gateway (edge-based solution).")
    ap.add_argument("--keys", required=True, help="key file with GATEWAY and every node (load_keys, hal/packet.py)")
    ap.add_argument("--data", default="gateway-data", help="folder for the gateway's saved state")
    ap.add_argument("--port", default="/dev/ttyUSB0", help="the ESP8266's serial port")
    ap.add_argument("--tree", default=BUILDING_B, help="the building's pipe tree (JSON)")
    ap.add_argument("--building", default="B", help="building name in MQTT topics")
    ap.add_argument("--mqtt-host", default=os.environ.get("MQTT_HOST"), help="broker; without it, no MQTT")
    ap.add_argument("--mqtt-port", type=int, default=int(os.environ.get("MQTT_PORT", 1883)))
    ap.add_argument("--mqtt-user", default=os.environ.get("MQTT_USER"))
    return ap.parse_args(argv)


def load_gateway_keys(path, topology):
    keys = load_keys(path)
    missing = sorted(({"GATEWAY", topology.tank} | topology.flats) - set(keys))
    if missing:
        raise SystemExit(f"{path} has no key for: {', '.join(missing)}")
    return keys


def main(argv=None):
    a = parse_args(argv)
    topology = Topology.load(a.tree)
    keys = load_gateway_keys(a.keys, topology)
    os.makedirs(a.data, exist_ok=True)
    try:
        radio = open_radio(a.port)
    except OSError as e:
        raise SystemExit(f"{a.port}: {e}")
    gateway = Gateway(GatewayBoard(radio, keys, os.path.join(a.data, "gateway.jsonl")), topology)

    mqtt = None
    if a.mqtt_host:
        from comms.gateway import GatewayComms, connect
        try:
            mqtt = connect(a.building, host=a.mqtt_host, port=a.mqtt_port, username=a.mqtt_user,
                           password=os.environ.get("MQTT_PASSWORD"))
        except ImportError:
            radio.close()
            raise SystemExit("paho-mqtt is not installed: uv sync --extra gateway")
        GatewayComms(gateway, mqtt, building=a.building).attach()

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):         # Ctrl-C and `docker stop`
        signal.signal(sig, lambda *_: stop.set())
    print(f"gateway running: radio on {a.port}, " +
          (f"MQTT {a.mqtt_host}:{a.mqtt_port}" if mqtt else "no MQTT"))
    try:
        run(gateway, stop)
    finally:
        if mqtt:
            mqtt.close()
        radio.close()
        print("gateway stopped")


if __name__ == "__main__":
    main()
