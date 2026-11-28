"""Gateway side of the communication spec (comms/README.md): what the gateway publishes on its
broker.

    from comms.gateway import GatewayComms, connect
    link = connect(username="gateway", password=...)    # never blocks; reconnects by itself
    GatewayComms(gateway, link).attach()                # gateway: a gateway.core.Gateway

This only reads the gateway after each step() returns, so after its state is saved and its ACKs
are sent.
"""
from comms.client import MqttLink
from comms.common import BUILDING, GATEWAY, KIND_CHANNEL, Publisher, message_payload, observe, topic

SNAPSHOT_S = 60         # the building state is rebuilt on new data, and at least this often


def connect(building=BUILDING, **options):
    """MQTT connection for the gateway program, with its online/offline status topic."""
    return MqttLink("aqua-gateway", status_topic=topic(GATEWAY, "status", building=building), **options)


class GatewayComms:
    def __init__(self, gateway, transport, building=BUILDING, log=print):
        self.gw, self.out = gateway, Publisher(transport, building, log)
        self.logged = self.decided = 0      # how far into gw.log and gw.events we have published
        self.commands = set()               # command numbers already published
        self.last_message = {}              # node -> receive time of its latest new message
        self.next_snapshot = None
        if hasattr(transport, "on_connected"):
            transport.on_connected(self.out.republish_retained)

    def attach(self):
        observe(self.gw, "step", lambda result: self.update())
        return self

    def update(self):
        now = self.gw.hal.now()
        messages = self._messages()
        events = self._events()
        self._commands(now)
        self._links(now)
        if messages or events or self.next_snapshot is None or now >= self.next_snapshot:
            self.next_snapshot = now + SNAPSHOT_S
            snapshot = self.gw.snapshot(now=now)
            snapshot.pop("t", None)
            self.out.publish_changed(GATEWAY, "state", snapshot, name="building", t=now)

    def _messages(self):
        """Node messages, copied once each: the gateway has already dropped duplicates."""
        new, self.logged = self.gw.log[self.logged:], len(self.gw.log)
        for m in new:
            self.last_message[m["n"]] = m["rx"]
            try:
                payload = {**message_payload(m), "rx": m["rx"]}
            except (KeyError, ValueError) as e:
                self.out.log(f"comms: message {m.get('s')} from {m.get('n')} not published: {e!r}")
                continue
            self.out.publish(m["n"], KIND_CHANNEL[m["k"]], payload)
        return len(new)

    def _events(self):
        """The gateway's own decisions: gateway/event/{EVENT}."""
        new, self.decided = self.gw.events[self.decided:], len(self.gw.events)
        for event in new:
            self.out.publish(GATEWAY, "event", {k: v for k, v in event.items() if k != "event"},
                             name=event["event"])
        return len(new)

    def _commands(self, now):
        """Commands to nodes: each once on {node}/cmd, and the unconfirmed ones as state."""
        waiting = sorted(self.gw.outbox.items(), key=lambda item: int(item[0]))
        for number, c in waiting:
            if number not in self.commands:
                self.out.publish(c["n"], "cmd", message_payload(
                    {"k": "CMD", "s": int(number), "t": c["t"], "a": c["a"], "over": c["over"]}))
        self.commands = {number for number, _ in waiting}
        self.out.publish_changed(GATEWAY, "state", {"commands": [
            {"s": int(number), "node": c["n"], "a": c["a"], "t": c["t"], "over": c["over"]}
            for number, c in waiting]}, name="unconfirmed", t=now)

    def _links(self, now):
        if self.gw.last_seen is None:
            return
        for node in sorted(self.gw.last_seen):
            self.out.publish_changed(node, "link", {
                "silent": node in self.gw.silent,
                "last_message": self.last_message.get(node),    # null: none since the gateway started
            }, t=now)
