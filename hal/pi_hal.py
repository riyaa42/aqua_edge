"""The Pi node's way into the HAL: its SX1278 on the GPIO pins (lora_pi.py), plus the shared
message link (link.py). Radio communication only: the node's clock, storage, LEDs and buzzer are
in edge/board.py, its sensor readings come from bogo/, and edge/edge_based.py runs it.
Wiring and setup: hal/README.md, "Running on the Pi".
"""
import time

from hal.link import MessageLink  # noqa: F401  (part of this way in)
from hal.lora_pi import RX_POLL_S, SX1278, LoRaLink  # noqa: F401


def open_radio(sleep=time.sleep, log=print):
    """The Pi's radio, receiving: send(raw, cad) -> bool, receive() -> [(arrival time, raw)].
    Raises OSError if there is no SX1278."""
    return LoRaLink(SX1278(), sleep=sleep, log=log)
