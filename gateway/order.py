"""Send an order to a node, through the running gateway:

    python3 -m gateway.order B-203 valve-close --data <the gateway's --data folder>
    docker compose exec gateway python -m gateway.order B-203 valve-open --data /data

Orders: valve-close, valve-open (the reopen signal), led-on (red alert LED and buzzer; red also
closes the valve), led-off (ends that red; reopens nothing).

This leaves a request in the gateway's data folder; the gateway picks it up, signs it with the
node's key and repeats it until the node confirms. So only someone who can write that folder can
order anything: nothing listens on the network for orders, and MQTT stays read-only. The gateway
never reopens a valve unasked; the problems it finds itself do close the valves of the nodes told
about them (planning/valve_spec.md). A confirmation means the node received the order; what its valve
did comes back as VALVE_CLOSED, VALVE_OPENED or VALVE_FAULT.
"""
import argparse
import json
import os
import time

ORDERS = {"valve-close": "VALVE_CLOSE", "valve-open": "VALVE_OPEN",
          "led-on": "ALERT_LED_ON", "led-off": "ALERT_LED_OFF"}


def folder(data):
    return os.path.join(data, "orders")


def request(data, node, order):
    """Queue one order for the running gateway."""
    if order not in ORDERS:
        raise ValueError(f"unknown order {order!r}")
    if not os.path.isdir(data):
        raise OSError(f"no such folder: {data}")
    os.makedirs(folder(data), exist_ok=True)
    path = os.path.join(folder(data), f"{time.time_ns()}-{os.getpid()}.json")
    with open(path + ".tmp", "w") as f:
        json.dump({"node": node, "order": order}, f)
    os.replace(path + ".tmp", path)             # the gateway never sees half a file


def take(gateway, data, log=print):
    """The gateway's side: carry out the waiting requests, oldest first."""
    if not os.path.isdir(folder(data)):
        return
    for name in sorted(n for n in os.listdir(folder(data)) if n.endswith(".json")):
        path = os.path.join(folder(data), name)
        try:
            with open(path) as f:
                asked = json.load(f)
            gateway.order(asked["node"], ORDERS[asked["order"]])
        except (OSError, ValueError, KeyError, TypeError) as e:
            log(f"gateway: order not carried out: {e!r}")
        try:
            os.remove(path)
        except OSError as e:
            log(f"gateway: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Send a valve or alert-LED order to a node.")
    ap.add_argument("node", help="node name, e.g. B-203 or B-TANK")
    ap.add_argument("order", choices=sorted(ORDERS))
    ap.add_argument("--data", default="gateway-data", help="the running gateway's --data folder")
    a = ap.parse_args()
    try:
        request(a.data, a.node, a.order)
    except (OSError, ValueError) as e:
        raise SystemExit(str(e))
    print(f"asked the gateway to send {a.order} to {a.node}; its log shows when the node has it")
