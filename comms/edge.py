"""Node side of the communication spec (comms/README.md): what a Pi publishes on its own broker.

    from comms.edge import EdgeComms, connect
    link = connect("B-203", username="node", password=...)      # never blocks; reconnects by itself
    EdgeComms(node, link).attach()                              # node: a FlatNode or TankNode

The node's own delivery (SD queue, radio, ACKs, buzzer) is untouched: this only reads the node
after each of its calls returns.
"""
from comms.client import MqttLink
from comms.common import BUILDING, KIND_CHANNEL, Publisher, message_payload, observe, sensor_payload, topic
from hal.lora433 import HEARD_S


def connect(node_id, building=BUILDING, **options):
    """MQTT connection for a node's program, with its online/offline status topic."""
    return MqttLink(f"aqua-node-{node_id}", status_topic=topic(node_id, "status", building=building), **options)


class EdgeComms:
    def __init__(self, node, transport, building=BUILDING, log=print):
        self.node, self.out = node, Publisher(transport, building, log)
        self.last_s = 0             # newest queued message published
        if hasattr(transport, "on_connected"):
            transport.on_connected(self.out.republish_retained)

    def attach(self):
        """Publish as the node runs: after step() and radio_poll(), and on each new command."""
        node, board = self.node, self.node.hal
        reading = {}
        read = board.read

        def remember():
            reading["now"] = read()         # the same readings the node just checked
            return reading["now"]

        def after_step(result):
            if "now" in reading:
                self.sensor(reading.pop("now"), board.now())
            self.update()

        board.read = remember
        observe(node, "step", after_step)
        observe(node, "radio_poll", lambda result: self.update())
        original = node.command

        def command(now, m):
            new = m["s"] not in node.commands_done      # the gateway repeats until confirmed
            result = original(now, m)                   # acted on, saved and confirmed first
            if new and m["s"] in node.commands_done:
                try:
                    self.command(m)
                except Exception as e:
                    self.out.log(f"comms: command not published: {e!r}")
            return result

        node.command = command
        return self

    def sensor(self, readings, t):
        """The latest readings. They stay on the Pi's broker; MQTT never sends them by radio."""
        self.out.publish(self.node.id, "sensor", sensor_payload(readings, t))

    def command(self, m):
        """A cross-node alert (or its end) from the gateway, once."""
        self.out.publish(self.node.id, "cmd", message_payload({**m, "over": m.get("over", 0)}))

    def update(self):
        """Publish messages newly put on the SD queue, then the state and link if changed. After a
        restart, messages still unacknowledged are published again: subscribers go by "s"."""
        node = self.node
        for m in sorted(node.store.queue, key=lambda m: m["s"]):
            if m["s"] > self.last_s:
                self.last_s = m["s"]        # first, so a bad message cannot block the ones after it
                try:
                    self.out.publish(node.id, KIND_CHANNEL[m["k"]], message_payload(m))
                except (KeyError, ValueError) as e:
                    self.out.log(f"comms: queued message {m['s']} not published: {e!r}")
        now = node.hal.now()
        self.out.publish_changed(node.id, "state", {
            "problems": sorted(node.open),              # reported and not yet reported recovered
            "gateway_alerts": dict(node.remote),        # cross-node problems now active
        }, t=now)
        self.out.publish_changed(node.id, "link", {
            "gateway_heard": node.heard_at is not None and now - node.heard_at <= HEARD_S,
            "waiting": len(node.store.queue),           # messages not yet acknowledged
        }, t=now)
