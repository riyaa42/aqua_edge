"""Run a node or gateway program with observation hooks, and log what happens to a JSONL file.

    python -m experiments.probe <log.jsonl> edge.edge_based B-203 --keys ...
    python -m experiments.probe <log.jsonl> gateway.edge_based --keys ...

The hooks wrap methods and pass every call and result through unchanged: no system file is edited.
Node: messages queued and acknowledged (with queue length and the data time), valve moves, and the
time each sensor reading takes. Gateway: packets received (first time or repeat), decisions, the time
of each step that handled a packet, and dashboard snapshots queued and posted.
"""
import importlib
import json
import sys
import time

_out = None


def log(event, **fields):
    _out.write(json.dumps({"e": event, "w": time.time(), **fields}) + "\n")


def after(cls, name, hook):
    """Call hook(self, args, result, wall_ns, cpu_ns) after every cls.name()."""
    original = getattr(cls, name)

    def wrapped(self, *args, **kwargs):
        w0, c0 = time.perf_counter_ns(), time.thread_time_ns()
        result = original(self, *args, **kwargs)
        hook(self, args, result, time.perf_counter_ns() - w0, time.thread_time_ns() - c0)
        return result

    setattr(cls, name, wrapped)


def node_hooks():
    from edge import aqua_edge
    from edge.board import NodeBoard

    now = {"sample": None}
    step, observe, ack = aqua_edge.Node.step, aqua_edge.Node.observe, aqua_edge.Store.ack
    observed = {"ns": 0}

    def timed_observe(self, td, r, checks=True):
        t0 = time.perf_counter_ns()
        observe(self, td, r, checks)
        observed["ns"] = time.perf_counter_ns() - t0

    def timed_step(self):
        now["sample"] = self.sample_time()
        w0, c0 = time.perf_counter_ns(), time.thread_time_ns()
        result = step(self)
        log("step", sample=now["sample"], wall_us=(time.perf_counter_ns() - w0) / 1000,
            cpu_us=(time.thread_time_ns() - c0) / 1000, detect_us=observed["ns"] / 1000,
            valve_closed=self.valve["closed"], queue=len(self.store.queue))
        return result

    def logged_ack(self, seq):
        before = len(self.queue)
        ack(self, seq)
        if len(self.queue) != before:
            log("acked", s=seq, queue=len(self.queue), sample=now["sample"])

    aqua_edge.Node.observe, aqua_edge.Node.step, aqua_edge.Store.ack = timed_observe, timed_step, logged_ack
    after(aqua_edge.Store, "add", lambda self, args, result, *_: log(
        "queued", s=self.seq, k=args[0]["k"], code=args[0].get("a") or args[0].get("f"),
        block=args[0].get("i"), queue=len(self.queue), sample=now["sample"]))
    after(NodeBoard, "set_valve", lambda self, args, result, *_: log(
        "valve", closed=bool(args[0]), ok=bool(result), sample=now["sample"]))


def gateway_hooks():
    from dashboard.shipper import DashboardShipper
    from gateway.core import Gateway
    from gateway.edge_based import GatewayBoard
    from hal.packet import peek

    got = {"packets": 0}

    def received(self, args, result, *_):
        got["packets"] += len(result)
        for raw in result:
            m = peek(raw)
            if m:
                log("packet", k=m["k"], n=m["n"], s=m.get("s"), bytes=len(raw))

    after(GatewayBoard, "receive", received)
    after(Gateway, "_is_seen_and_record", lambda self, args, repeat, *_: log(
        "message", n=args[0], s=args[1], repeat=bool(repeat)))
    after(Gateway, "decide", lambda self, args, result, *_: log(
        "decision", kind=args[1] if len(args) > 1 else None))
    step = Gateway.step

    def timed_step(self):
        before = got["packets"]
        w0, c0 = time.perf_counter_ns(), time.thread_time_ns()
        result = step(self)
        if got["packets"] > before:
            log("handled", packets=got["packets"] - before, wall_us=(time.perf_counter_ns() - w0) / 1000,
                cpu_us=(time.thread_time_ns() - c0) / 1000)
        return result

    Gateway.step = timed_step
    after(DashboardShipper, "enqueue", lambda self, args, seq, *_: log(
        "snapshot", seq=seq, pending=len(self.queue)))
    post = DashboardShipper._post_batch

    def timed_post(self, batch):
        seqs, w0 = [r["seq"] for r in batch], time.perf_counter_ns()
        try:
            reply = post(self, batch)
        except Exception as e:
            log("post", ok=False, seqs=seqs, ms=(time.perf_counter_ns() - w0) / 1e6, error=type(e).__name__)
            raise
        log("post", ok=True, seqs=seqs, ms=(time.perf_counter_ns() - w0) / 1e6, reply=reply)
        return reply

    DashboardShipper._post_batch = timed_post


def main():
    global _out
    path, module, *rest = sys.argv[1:]
    _out = open(path, "a", buffering=1)
    program = importlib.import_module(module)
    (gateway_hooks if module.startswith("gateway") else node_hooks)()
    sys.argv = [module, *rest]
    program.main()


if __name__ == "__main__":
    main()
