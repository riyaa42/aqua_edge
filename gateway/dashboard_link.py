"""Sends the gateway's snapshots to the dashboard server (dashboard/API_SPEC.md) through
dashboard.shipper.DashboardShipper, which keeps them on disk until the dashboard has them.

It also collects the dashboard's requests to reopen a valve (DashboardCommands) and gives each to
the gateway as an operator's VALVE_OPEN order. The gateway asks; nothing connects to it.

The shipper writes to disk and waits for HTTP answers, so it runs on its own thread: a slow or
unreachable dashboard never delays the radio. The gateway's thread only copies a snapshot, and
carries out requests the other thread has fetched.
"""
import json
import math
import os
import queue
import threading
import time
import urllib.parse
import urllib.request

from comms.common import observe
from edge.aqua_edge import save_atomic

SNAPSHOT_S = 60         # a frame at least this often
EVENT_GAP_S = 2         # sooner after a new gateway decision, but no closer together than this
WAITING_FRAMES = 100    # copies waiting for the thread; when full, no more are taken for now
HELD_EVENTS = 1000      # decisions kept to go with a later frame if one cannot be stored
COMMANDS_S = 3          # how often the dashboard is asked for reopen requests
MAX_AGE_S = 120         # an older request is refused (the dashboard expires them too)
REMEMBERED = 200        # requests already acted on, so none is acted on twice


class DashboardCommands:
    """The dashboard's valve reopen requests (dashboard/API_SPEC.md). fetch() and answer() talk to
    the dashboard, on its thread; carry_out() acts, on the gateway's. memory_path: where the IDs
    already acted on are kept. ingest_url: the address snapshots go to."""

    def __init__(self, ingest_url, gateway_id, token, memory_path, timeout_s=5.0, clock=time.monotonic):
        if not ingest_url.rstrip("/").endswith("/ingest"):
            raise ValueError(f"cannot tell the dashboard's address from {ingest_url!r}: it should end /ingest")
        self.url = ingest_url.rstrip("/").removesuffix("/ingest") + "/gateway/commands"
        self.gateway_id, self.token, self.memory_path = gateway_id, token, memory_path
        self.timeout_s, self.clock = timeout_s, clock
        self.done = {}          # id -> [result, detail]; result None: begun and not finished
        if os.path.exists(memory_path):
            with open(memory_path) as f:
                self.done = json.load(f)

    def _call(self, url, body=None):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers=headers)
        local = (urllib.parse.urlparse(url).hostname or "").lower() in ("127.0.0.1", "localhost", "::1")
        opener = urllib.request.build_opener(*([urllib.request.ProxyHandler({})] if local else []))
        with opener.open(req, timeout=self.timeout_s) as resp:
            return json.loads(resp.read(65536).decode("utf-8"))

    def fetch(self):
        """The waiting requests, each with when it was fetched."""
        asked_at = self.clock()     # before asking, so a slow answer makes a request older, not newer
        reply = self._call(f"{self.url}?{urllib.parse.urlencode({'gateway_id': self.gateway_id})}")
        return [{**c, "fetched": asked_at} for c in reply["commands"]
                if isinstance(c, dict) and isinstance(c.get("id"), str)]

    def answer(self, request_id, result, detail):
        self._call(self.url + "/ack", {"gateway_id": self.gateway_id, "id": request_id,
                                       "result": result, "detail": detail})

    def _remember(self, request_id, result, detail=None):
        self.done[request_id] = [result, detail]
        while len(self.done) > REMEMBERED:
            del self.done[next(iter(self.done))]
        save_atomic(self.memory_path, json.dumps(self.done))

    def carry_out(self, gateway, command, log=print):
        """-> (id, "sent" or "rejected", detail). A request seen before gets the same answer."""
        request_id = command["id"]
        if request_id in self.done:
            result, detail = self.done[request_id]
            if result is None:
                return request_id, "rejected", "the gateway restarted while handling it: ask again"
            return request_id, result, detail
        self._remember(request_id, None)            # saved first: it is never acted on twice
        result, detail = "rejected", None
        age = command.get("age_s")
        if command.get("command") != "VALVE_OPEN":
            detail = "only VALVE_OPEN can be asked for"
        elif not (isinstance(age, (int, float)) and math.isfinite(age) and age >= 0):
            detail = "the request has no usable age"
        else:
            age += max(0.0, self.clock() - command["fetched"])
            if age > MAX_AGE_S:
                detail = "too old by the time the gateway could act"
            else:
                try:        # dated when it was asked for: anything that closes the valve later wins
                    gateway.order(command.get("node"), "VALVE_OPEN", t=gateway.hal.now() - age)
                    gateway.save_state()
                    result, detail = "sent", "queued for the node"
                    log(f"dashboard: reopen asked for {command.get('node')}")
                except (ValueError, TypeError) as e:
                    detail = str(e)
        self._remember(request_id, result, detail)
        return request_id, result, detail


class DashboardFeed:
    """gateway: a gateway.core.Gateway. shipper: a DashboardShipper, used only by this thread."""

    def __init__(self, gateway, shipper, log=print, interval_s=SNAPSHOT_S, clock=time.monotonic,
                 commands=None, commands_s=COMMANDS_S):
        self.gw, self.shipper, self.log = gateway, shipper, log
        self.commands, self.commands_s, self.asked_at = commands, commands_s, None
        self.asked, self.answers = queue.Queue(), queue.Queue()     # to and from the gateway's thread
        self.in_hand = set()                    # ids handed over and not yet answered
        self.command_problem = None
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
    def take_requests(self):
        """Carry out the reopen requests the dashboard thread has fetched."""
        while not self.asked.empty():
            command = self.asked.get_nowait()
            try:
                self.answers.put(self.commands.carry_out(self.gw, command, self.log))
            except Exception as e:          # a full disk must not stop the radio; asked for again later
                self.in_hand.discard(command["id"])
                self.log(f"dashboard: reopen request not carried out: {e!r}")

    def update(self, force=False):
        """Act on fetched requests, then take a copy of the gateway's state if one is due. Never
        waits for the dashboard."""
        if self.commands:
            self.take_requests()
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

    def _commands(self):
        """On the dashboard thread: report what was done, and now and then ask for more."""
        try:
            while not self.answers.empty():
                request_id, result, detail = self.answers.get_nowait()
                self.in_hand.discard(request_id)        # if this is lost, it is offered and answered again
                self.commands.answer(request_id, result, detail)
            if self.asked_at is None or self.clock() - self.asked_at >= self.commands_s:
                self.asked_at = self.clock()
                for command in self.commands.fetch():
                    if command["id"] not in self.in_hand:
                        self.in_hand.add(command["id"])
                        self.asked.put(command)
                self.command_problem = None
        except Exception as e:
            text = f"dashboard: no reopen requests: {e}"
            if self.command_problem != text:
                self.command_problem = text
                self.log(text)

    def _work(self):
        while not self.stopping.is_set():
            behind = self.shipper.connected and len(self.shipper.queue)     # catching up: do not wait
            try:
                self._store(self.inbox.get(timeout=0.01 if behind else 0.2))
            except queue.Empty:
                pass
            self._send()
            if self.commands:
                self._commands()

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
