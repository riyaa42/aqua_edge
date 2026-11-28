"""Sends the gateway's snapshots to the dashboard server (dashboard/API_SPEC.md) through
dashboard.shipper.DashboardShipper, which keeps them on disk until the dashboard has them.

The shipper writes to disk and waits for HTTP answers, so it runs on its own thread: a slow or
unreachable dashboard never delays the radio. The gateway's thread only copies a snapshot.
"""
import json
import queue
import threading
import time

from comms.common import observe

SNAPSHOT_S = 60         # a frame at least this often
EVENT_GAP_S = 2         # sooner after a new gateway decision, but no closer together than this
WAITING_FRAMES = 100    # copies waiting for the thread; when full, no more are taken for now
HELD_EVENTS = 1000      # decisions kept to go with a later frame if one cannot be stored


class DashboardFeed:
    """gateway: a gateway.core.Gateway. shipper: a DashboardShipper, used only by this thread."""

    def __init__(self, gateway, shipper, log=print, interval_s=SNAPSHOT_S, clock=time.monotonic):
        self.gw, self.shipper, self.log = gateway, shipper, log
        self.interval_s, self.clock = interval_s, clock
        self.inbox = queue.Queue(WAITING_FRAMES)
        self.taken, self.taken_at = 0, None     # how far into gw.events, and when
        self.held = []                          # decisions of frames that could not be stored
        self.problem = None                     # the last trouble logged, so it is logged once
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._work, daemon=True)

    def start(self):
        observe(self.gw, "step", lambda result: self.update())
        self.thread.start()
        return self

    # --- the gateway's thread ---
    def update(self, force=False):
        """Take a copy of the gateway's state if one is due. Never waits."""
        now = self.clock()
        waited = float("inf") if self.taken_at is None else now - self.taken_at
        news = len(self.gw.events) > self.taken
        if not (force or waited >= self.interval_s or (news and waited >= EVENT_GAP_S)) or self.inbox.full():
            return
        # through JSON: the other thread must not share objects the gateway goes on changing
        frame = json.loads(json.dumps({"snapshot": self.gw.snapshot(), "events": self.gw.events[self.taken:]}))
        self.inbox.put_nowait({**frame, "created": time.time()})
        self.taken, self.taken_at = len(self.gw.events), now

    # --- the dashboard thread ---
    def _store(self, frame):
        events = self.held + frame["events"]
        try:
            self.shipper.enqueue(frame["snapshot"], new_events=events, created_at_utc=frame["created"])
            self.held = []
        except Exception as e:          # disk full: this snapshot is skipped, its decisions are kept
            self.held = events[-HELD_EVENTS:]
            self._report(f"dashboard: snapshot not stored: {e}")

    def _send(self):
        """One batch, if the shipper is not waiting before its next try."""
        try:
            self.shipper.flush_once()
        except Exception as e:
            self._report(f"dashboard: could not send: {e!r}")
            return
        if self.shipper.connected is False:
            self._report("dashboard: not reachable, keeping snapshots to send later", self.shipper.last_error)
        elif self.shipper.connected and self.problem:
            self.problem = None
            self.log("dashboard: delivering again")

    def _report(self, text, detail=None):
        if self.problem != text:
            self.problem = text
            self.log(text if detail is None else f"{text} ({detail})")

    def _work(self):
        while not self.stopping.is_set():
            behind = self.shipper.connected and len(self.shipper.queue)     # catching up: do not wait
            try:
                self._store(self.inbox.get(timeout=0.01 if behind else 0.2))
            except queue.Empty:
                pass
            self._send()

    def stop(self):
        """Take a last snapshot and put everything waiting on disk, to go after the restart."""
        try:
            self.update(force=True)
        except Exception as e:
            self.log(f"dashboard: no last snapshot: {e!r}")
        self.stopping.set()
        self.thread.join(timeout=self.shipper.timeout_s + 2)      # it may be waiting for an answer
        if self.thread.is_alive():
            self.log("dashboard: sender still busy; the newest snapshots are not saved")
            return
        while not self.inbox.empty():
            self._store(self.inbox.get_nowait())
