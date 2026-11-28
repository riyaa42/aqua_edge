"""Gateway node entry point, edge-based solution (uv run prod-edge-gateway): nodes decide their own alerts, and the gateway
decides what needs several nodes' data (core.py). It reaches the nodes through the ESP8266 LoRa
modem (hal/gate_hal.py), or over the network to nodes without radio hardware (hal/udp.py), or
both. It publishes what it hears and decides over MQTT (comms/), sends snapshots to the dashboard
(dashboard_link.py), and passes on an operator's orders to nodes (order.py, and the dashboard's
reopen button). It has no sensors: it works from what the nodes send.

    uv run prod-edge-gateway --keys gateway_keys.json --data gateway-data --port /dev/ttyUSB0
    python3 -m gateway.edge_based --keys gateway_keys.json --data gateway-data --port /dev/ttyUSB0
    python3 -m gateway.edge_based ... --mqtt-host 127.0.0.1 --mqtt-user gateway    # password: MQTT_PASSWORD

    python3 -m gateway.edge_based ... --port none --udp-listen 127.0.0.1:1700   # network nodes only

    python3 -m gateway.edge_based ... --dashboard-url http://127.0.0.1:8000/api/v1/ingest  # token: AQUA_DASHBOARD_TOKEN

Without --mqtt-host (or MQTT_HOST) it runs without MQTT, and without --dashboard-url (or
DASHBOARD_URL) it sends nothing to a dashboard. The radio never waits for the broker or dashboard.
With Docker Compose, which also runs the broker: gateway/README.md. Not tested on real hardware.
"""
import argparse
import os
import signal
import threading
import time

from dashboard.shipper import DashboardShipper
from gateway import order
from gateway.core import Gateway
from gateway.dashboard_link import DashboardCommands, DashboardFeed
from gateway.topology import BUILDING_B, Topology
from hal.gate_hal import open_radio
from hal.packet import add_names, load_keys
from hal.udp import Radios, UdpHub, parse_address

POLL_S = 0.02           # a packet that has arrived is handled within this long
MIN_WAIT_S = 0.005
ORDERS_S = 0.5          # how often waiting orders are picked up


class GatewayBoard:
    """What gateway.core.Gateway needs of its `hal`: clock, keys, state file and radio."""

    def __init__(self, radio, keys, storage_path, clock=time.time):
        self.radio, self.keys, self.storage_path, self.clock = radio, keys, storage_path, clock
        if hasattr(radio, "start_send") and hasattr(radio, "poll_send"):
            self.start_send = radio.start_send
            self.poll_send = radio.poll_send

    def now(self):
        return self.clock()         # UTC seconds. Nodes set their clocks from this

    def key(self, name):
        return self.keys.get(name)

    def send(self, raw, cad=False):
        return self.radio.send(raw, cad)

    def receive(self):
        return [raw for _, raw in self.radio.receive()]     # the radio also gives arrival times


def run(gateway, stop, log=print, data=None):
    """Step the gateway until `stop` is set: when it asks, and soon after any packet arrives.
    New decisions are logged. data: the folder where orders are left (order.py)."""
    logged, look_at = 0, 0.0
    while not stop.is_set():
        if data and time.monotonic() >= look_at:
            look_at = time.monotonic() + ORDERS_S
            order.take(gateway, data, log)
        wake = gateway.step()
        new_events, logged = gateway.events_since(logged, consumer="runner")
        for event in new_events:
            log(f"gateway: {event}")
        stop.wait(max(MIN_WAIT_S, min(wake - gateway.hal.now(), POLL_S)))


def parse_args(argv=None, mode="edge", options=None):
    ap = argparse.ArgumentParser(description=f"Run the Aqua Edge gateway ({mode}-based solution).")
    ap.add_argument("--keys", required=True, help="key file with GATEWAY and every node (load_keys, hal/packet.py)")
    ap.add_argument("--data", default="gateway-data", help="folder for the gateway's saved state")
    ap.add_argument("--port", default=os.environ.get("AQUA_RADIO_PORT") or "/dev/ttyUSB0",
                    help="the ESP8266's serial port, or none; also AQUA_RADIO_PORT")
    ap.add_argument("--udp-listen", default=os.environ.get("UDP_LISTEN"),
                    help="host:port for nodes that use the network instead of a radio (hal/udp.py)")
    ap.add_argument("--tree", default=os.environ.get("AQUA_TOPOLOGY_PATH") or BUILDING_B,
                    help="the building's pipe tree (JSON); also AQUA_TOPOLOGY_PATH")
    ap.add_argument("--building", help="building name in MQTT topics (default: the tree's)")
    ap.add_argument("--mqtt-host", default=os.environ.get("MQTT_HOST"), help="broker; without it, no MQTT")
    ap.add_argument("--mqtt-port", type=int, default=int(os.environ.get("MQTT_PORT", 1883)))
    ap.add_argument("--mqtt-user", default=os.environ.get("MQTT_USER"))
    ap.add_argument("--dashboard-url", default=os.environ.get("DASHBOARD_URL"),
                    help="the dashboard's ingest address; without it, nothing is sent")
    ap.add_argument("--dashboard-id", default=os.environ.get("DASHBOARD_GATEWAY_ID"),
                    help="this gateway's name on the dashboard (default: building-<building>)")
    if options:
        options(ap)
    return ap.parse_args(argv)


def load_gateway_keys(path, topology):
    add_names(topology.radio_ids)           # before load_keys, which refuses unknown names
    keys = load_keys(path)
    missing = sorted(({"GATEWAY"} | topology.devices) - set(keys))
    if missing:
        raise SystemExit(f"{path} has no key for: {', '.join(missing)}")
    return keys


def open_radios(port, udp_listen):
    """The modem on `port` (unless "none"), the network port, or both, as one radio."""
    radios = []
    try:
        if port != "none":
            try:
                radios.append(open_radio(port))
            except OSError as e:
                raise SystemExit(f"{port}: {e}")
        if udp_listen:
            try:
                radios.append(UdpHub(*parse_address(udp_listen)))
            except (OSError, ValueError) as e:
                raise SystemExit(f"{udp_listen}: {e}")
    except SystemExit:
        for r in radios:
            r.close()
        raise
    if not radios:
        raise SystemExit("no way to hear the nodes: give --port, --udp-listen or both")
    return radios[0] if len(radios) == 1 else Radios(radios)


def main(argv=None, mode="edge", options=None, wrap=None):
    """mode: the label on the dashboard's frames. options(parser) adds to the command line, and
    wrap(radio, keys, topology, args) gives the radio the Gateway is to use (cloud_based.py)."""
    a = parse_args(argv, mode, options)
    try:
        topology = Topology.load(a.tree)
        keys = load_gateway_keys(a.keys, topology)
    except (OSError, ValueError) as e:
        raise SystemExit(str(e))
    building = a.building or topology.data["building"]
    os.makedirs(a.data, exist_ok=True)
    radio = open_radios(a.port, a.udp_listen)
    if wrap:
        radio = wrap(radio, keys, topology, a)
    gateway = Gateway(GatewayBoard(radio, keys, os.path.join(a.data, "gateway.jsonl")), topology)

    mqtt = None
    if a.mqtt_host:
        from comms.gateway import GatewayComms, connect
        try:
            mqtt = connect(building, host=a.mqtt_host, port=a.mqtt_port, username=a.mqtt_user,
                           password=os.environ.get("MQTT_PASSWORD"))
        except ImportError:
            radio.close()
            raise SystemExit("paho-mqtt is not installed: uv sync --extra gateway")
        GatewayComms(gateway, mqtt, building=building).attach()

    feed = None
    if a.dashboard_url:
        gateway_id = a.dashboard_id or f"building-{building}"
        token = os.environ.get("AQUA_DASHBOARD_TOKEN", "")
        shipper = DashboardShipper(a.dashboard_url, os.path.join(a.data, "dashboard-outbox"),
                                   gateway_id=gateway_id, mode=mode, auth_token=token)
        try:
            commands = DashboardCommands(a.dashboard_url, gateway_id, token,
                                         os.path.join(a.data, "dashboard-commands.json"))
        except ValueError as e:
            commands = None
            print(f"gateway: no reopen requests from the dashboard: {e}")
        feed = DashboardFeed(gateway, shipper, commands=commands).start()

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):         # Ctrl-C and `docker stop`
        signal.signal(sig, lambda *_: stop.set())
    heard = [f"radio on {a.port}"] if a.port != "none" else []
    heard += [f"network nodes on {a.udp_listen}"] if a.udp_listen else []
    print(f"gateway running: {', '.join(heard)}, " +
          (f"MQTT {a.mqtt_host}:{a.mqtt_port}" if mqtt else "no MQTT") +
          (f", dashboard {a.dashboard_url}" if feed else ", no dashboard"))
    try:
        run(gateway, stop, data=a.data)
    finally:
        if feed:
            feed.stop()
        if mqtt:
            mqtt.close()
        radio.close()
        print("gateway stopped")


if __name__ == "__main__":
    main()
