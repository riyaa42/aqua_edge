"""The one place that uses an MQTT library: an MQTT 5 connection with paho-mqtt 2, imported only
when a connection is made (`uv sync --extra mqtt`). Used by the node, the gateway and any program
that subscribes.

    from comms.client import MqttLink
    from comms.common import alert_filters, dispatcher
    show = dispatcher(lambda topic, payload: print(topic.node, topic.channel, payload))
    link = MqttLink("my-screen", username="screen", password=...,
                    subscriptions={f: show for f in alert_filters()})
"""
from comms.common import WEEK_S, encode

KEEPALIVE_S = 30
MAX_WAITING = 1000          # unsent messages held while the broker is away; newer ones are dropped


class MqttLink:
    """A transport for comms.common.Publisher, and a subscriber.

    Never waits for the broker: it connects in the background and keeps reconnecting. The session
    is kept for session_s, so the broker holds QoS 1 messages for a subscriber that is away; that
    needs the same client_id every time, and only one running program per client_id.

    subscriptions: {filter: on_message(topic string, bytes)}. Give them here, not later, so that
    messages the broker kept are not delivered before anyone listens."""

    def __init__(self, client_id, host="127.0.0.1", port=1883, username=None, password=None,
                 status_topic=None, subscriptions=None, session_s=WEEK_S, log=print):
        import paho.mqtt.client as mqtt
        from paho.mqtt.packettypes import PacketTypes
        from paho.mqtt.properties import Properties
        self.log, self.status_topic = log, status_topic
        self.subscriptions = dict(subscriptions or {})
        self.callbacks = []
        self.matches = mqtt.topic_matches_sub
        self.expiry = lambda seconds: self._expiry(Properties, PacketTypes, seconds)
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, protocol=mqtt.MQTTv5)
        if username is not None:
            client.username_pw_set(username, password)
        if status_topic:            # the broker publishes this if we vanish without saying goodbye
            client.will_set(status_topic, encode({"online": False}), qos=1, retain=True)
        client.max_queued_messages_set(MAX_WAITING)
        client.on_connect = self._connected
        client.on_message = self._message
        self.client = client
        properties = Properties(PacketTypes.CONNECT)
        properties.SessionExpiryInterval = session_s
        client.connect_async(host, port, keepalive=KEEPALIVE_S, clean_start=False, properties=properties)
        client.loop_start()

    @staticmethod
    def _expiry(Properties, PacketTypes, seconds):
        properties = Properties(PacketTypes.PUBLISH)
        properties.MessageExpiryInterval = seconds
        return properties

    def on_connected(self, callback):
        """Call callback() after every (re)connection."""
        self.callbacks.append(callback)

    def publish(self, where, data, qos, retain, expiry_s):
        """Queue and return at once. While the broker is away, QoS 1 waits and QoS 0 is lost."""
        self.client.publish(where, data, qos=qos, retain=retain,
                            properties=None if expiry_s is None else self.expiry(expiry_s))

    def _connected(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            self.log(f"comms: broker refused the connection: {reason_code}")
            return
        for filter_ in self.subscriptions:
            client.subscribe(filter_, qos=1)
        if self.status_topic:
            client.publish(self.status_topic, encode({"online": True}), qos=1, retain=True)
        for callback in self.callbacks:
            try:
                callback()
            except Exception as e:
                self.log(f"comms: after connecting: {e!r}")

    def _message(self, client, userdata, message):
        for filter_, handler in self.subscriptions.items():
            if self.matches(filter_, message.topic):
                try:
                    handler(message.topic, message.payload)
                except Exception as e:      # must not stop the network thread
                    self.log(f"comms: handler failed for {message.topic}: {e!r}")

    def close(self, wait_s=2.0):
        """Say we are going offline, then disconnect."""
        if self.status_topic and self.client.is_connected():
            self.client.publish(self.status_topic, encode({"online": False}), qos=1,
                                retain=True).wait_for_publish(wait_s)
        self.client.disconnect()
        self.client.loop_stop()
