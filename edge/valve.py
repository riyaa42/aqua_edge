"""Reopen a node's valve by hand, from the node itself:

    python3 -m edge.valve open --sd <the node's --sd folder>
    docker compose exec node-b203 python -m edge.valve open --sd /data      # in a container

This leaves a request in that folder, which the running node carries out at once (or when it next
starts). The gateway can order it too (gateway/order.py). A node also reopens by itself once its alerts have been over for the tolerance period,
and a flat can be set to try reopening (aqua_edge.py); these two work at once.
"""
import argparse
import os

REQUEST = "valve.request"


def request_open(folder):
    if not os.path.isdir(folder):
        raise OSError(f"no such folder: {folder}")
    with open(os.path.join(folder, REQUEST), "w") as f:
        f.write("open\n")


def take_request(folder):
    """True, once, if a request to open is waiting."""
    path = folder and os.path.join(folder, REQUEST)
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path) as f:
            asked = f.read().strip() == "open"
        os.remove(path)
    except OSError:
        return False
    return asked


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Reopen this node's water valve.")
    ap.add_argument("action", choices=["open"])
    ap.add_argument("--sd", required=True, help="the running node's --sd folder")
    a = ap.parse_args()
    try:
        request_open(a.sd)
    except OSError as e:
        raise SystemExit(str(e))
    print("asked the node to open its valve; its log shows when it has")
