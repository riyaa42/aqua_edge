"""The gateway's way into the HAL: the LoRa radio behind a NodeMCU ESP8266 on USB serial
(esp8266_passthrough.py), plus the shared message link (link.py). Radio communication only.
Wiring, flashing and setup: hal/README.md, "Gateway modem (ESP8266)".
"""
from hal.esp8266_passthrough import DEFAULT_PORT, ESPLink, SerialPort
from hal.link import MessageLink  # noqa: F401  (part of this way in)


def open_radio(port=DEFAULT_PORT, log=print):
    """The gateway's radio, receiving, with the same send() and receive() as the Pi's
    (pi_hal.open_radio): send(raw, cad) -> bool, receive() -> [(arrival time, raw)].
    Raises OSError if no working modem answers on `port`."""
    return ESPLink(lambda: SerialPort(port), log=log)
