"""Gateway server entry point, cloud-based solution (the control case, planning/baselines.md,
Baseline A): the nodes only stream raw readings (edge/cloud_based.py), and every detector and
decision runs here, one copy of the edge-based node's code per node (cloud_host.py). The gateway
itself, and everything beyond it (dashboard, reopen requests, orders, MQTT), is the same program as
the edge-based one (edge_based.py); the dashboard's frames are labelled "cloud".

    uv run prod-cloud-gateway --keys gateway_keys.json --port /dev/ttyUSB0
    uv run prod-cloud-gateway --keys gateway_keys.json --port none --udp-listen 0.0.0.0:1700

It takes every option of the edge-based gateway, and the two settings that an edge-based node takes
about reopening its valve. Its data folder must not be one an edge-based gateway has used. Not
tested on real hardware.
"""
import os
import time

from edge.aqua_edge import REOPEN_AFTER_S
from gateway import edge_based
from gateway.cloud_host import CloudHost

DATA = "cloud-gateway-data"
BRAINS = "brains"


def add_options(ap):
    env = os.environ.get
    ap.set_defaults(data=DATA)
    ap.add_argument("--reopen-after-s", type=float, default=float(env("AQUA_REOPEN_AFTER_S") or REOPEN_AFTER_S),
                    help="tolerance before a closed valve reopens by itself: this long normal once its "
                         "alerts have cleared (default %(default)s)")
    ap.add_argument("--trial-reopen", action="store_true",
                    default=(env("AQUA_TRIAL_REOPEN") or "").lower() in ("1", "true", "yes", "on"),
                    help="flats: after hours shut by their own alert, reopen to see whether the water "
                         "stops, and shut again if not. Water may run through a leak meanwhile, and "
                         "longer if the link to the node is lost")


def wrap_radio(radio, keys, topology, a):
    """The radio the Gateway sees: the brains, with the real radio behind them."""
    brains = os.path.join(a.data, BRAINS)
    if os.path.exists(os.path.join(a.data, "gateway_state.json")) and not os.path.isdir(brains):
        radio.close()
        raise SystemExit(f"{a.data} holds an edge-based gateway's state: give another --data folder")
    if not 0 <= a.reopen_after_s < float("inf"):
        radio.close()
        raise SystemExit("--reopen-after-s must be 0 or more")
    return CloudHost(radio, keys, topology, brains, clock=time.time, monotonic=time.monotonic,
                     reopen_after_s=a.reopen_after_s, trial_reopen=a.trial_reopen,
                     limit_air=a.port != "none")      # nodes on the network only use no airtime


def main(argv=None):
    edge_based.main(argv, mode="cloud", options=add_options, wrap=wrap_radio)


if __name__ == "__main__":
    main()
