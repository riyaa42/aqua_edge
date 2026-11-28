"""What a node keeps on its SD card: the store-and-forward queue and a power-cut-safe save. Used by
both kinds of node (edge-based and cloud-based) and by the gateway. Standard library and hal/ only:
the cloud-based node must run without any detection code.
"""
import json
import os
import random
import time

from hal.lora433 import FLUSH_ROUTINE_AFTER_S, FLUSH_STEP_S, retry_wait

SUMMARY_S = 900               # send a summary every 15 minutes
MAX_SUMMARIES = 7 * 86400 // SUMMARY_S    # kept while unsent: 7 days, as far back as the gateway's balance check goes
STUCK_S = 30                  # unacknowledged this long: yellow, one blink
ORDER_SLACK_S = 1             # a command's time is sent in whole seconds
PRIORITY = {"ALERT": 0, "FLAG": 1, "SUM": 2}       # FLAG: the tank's SUPPLY_LOW and SUPPLY_RECOVERED


def save_atomic(path, text):
    """Write a file so a power cut leaves either the old version or the new one, never half.
    No battery on the nodes, so a power cut can land in the middle of any save."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())          # new contents really on the SD card
    if os.name == "nt":
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.015)
    else:
        os.replace(tmp, path)             # atomic swap: old file or new file, nothing in between
    if os.name != "nt":               # POSIX: fsync parent directory; Windows does not support os.open on dirs
        fd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
        try:
            os.fsync(fd)              # the swap itself really on the SD card
        finally:
            os.close(fd)


class Store:
    """Store-and-forward queue, mirrored to a file (the SD card) so a reboot loses nothing.
    Also owns the message counter: saved in the same file (first line), so it never repeats,
    even after a power cut with an empty queue. The gateway drops a repeated number as a
    duplicate or replay, so a counter starting again from 1 would silence the node."""

    limit = MAX_SUMMARIES

    @staticmethod
    def droppable(m):
        return m["k"] == "SUM"                  # what may be given up for room, oldest first

    def __init__(self, path, seed=0):
        self.path, self.queue, self.next_try, self.retries, self.seq = path, [], {}, {}, 0
        self.queued_at = {}                     # seq -> monotonic enqueue time (in-memory only, for stuck check)
        self.total_resends, self.dropped = 0, 0       # dropped: old summaries given up for room
        self.rng = random.Random(seed)
        if path and os.path.exists(path):
            with open(path) as f:
                for line in f:
                    if line.strip():
                        d = json.loads(line)
                        if "k" in d:
                            self.queue.append(d)
                        else:
                            self.seq, self.dropped = d["seq"], d.get("dropped", 0)
        self.seq = max([self.seq] + [m["s"] for m in self.queue])

    def _save(self):
        # ponytail: rewrites the whole file on change; switch to an append log if the queue gets long
        if self.path:
            save_atomic(self.path, json.dumps({"seq": self.seq, "dropped": self.dropped}) + "\n"
                        + "".join(json.dumps(m) + "\n" for m in self.queue))

    def add(self, msg, mono_now=None):
        """Give the message the next counter value and keep it until acknowledged. Alerts and
        flags are never given up; of the summaries only the newest MAX_SUMMARIES are kept."""
        self.seq += 1
        self.queue.append({**msg, "s": self.seq})
        if mono_now is not None:
            self.queued_at[self.seq] = mono_now
        spare = [m["s"] for m in self.queue if self.droppable(m)]
        if len(spare) > self.limit:
            old = set(spare[:len(spare) - self.limit])
            self.queue = [m for m in self.queue if m["s"] not in old]
            for s in old:
                self.next_try.pop(s, None)
                self.retries.pop(s, None)
                self.queued_at.pop(s, None)
            self.dropped += len(old)
        self._save()

    def ack(self, seq):
        before = len(self.queue)
        self.queue = [m for m in self.queue if m["s"] != seq]
        self.next_try.pop(seq, None)
        self.retries.pop(seq, None)
        self.queued_at.pop(seq, None)
        if len(self.queue) != before:
            self._save()

    @staticmethod
    def urgent(m):
        return m["k"] != "SUM"                  # alerts and the tank's flags

    def eligible(self):
        """What may be on air: every unacknowledged alert and flag; with none waiting, only the
        oldest summary (one at a time, so a long backlog does not flood the channel)."""
        pending = sorted((m for m in self.queue if self.urgent(m)), key=lambda m: (PRIORITY[m["k"]], m["t"]))
        if pending or not self.queue:
            return pending
        return [min(self.queue, key=lambda m: (m["t"], m["s"]))]

    def due(self, now, heard):
        """The messages to send now; each is scheduled to be sent again unless acknowledged."""
        ready = [m for m in self.eligible() if now >= self.next_try.get(m["s"], -1e18)]
        for m in ready:
            s = m["s"]
            tries = self.retries.get(s, 0)
            if tries:
                self.total_resends += 1
            self.next_try[s] = now + retry_wait(tries, self.urgent(m), heard, self.rng)
            self.retries[s] = tries + 1
        return ready

    def wake(self, now):
        """When something next needs sending, or None."""
        times = [self.next_try.get(m["s"], now) for m in self.eligible()]
        return min(times) if times else None

    def flush(self, now, index):
        """The gateway is back: send what is waiting, node by node so they do not all collide.
        Only ever brings a retry forward."""
        for m in self.queue:
            s = m["s"]
            if self.retries.get(s):
                start = now + index * FLUSH_STEP_S + (0 if self.urgent(m) else FLUSH_ROUTINE_AFTER_S)
                self.next_try[s] = min(self.next_try[s], start)
                self.retries[s] = 0


def saved_sample_time(storage_path):
    """The time of the last reading a node's saved state includes, or None. Replayed data
    resumes after it."""
    path = storage_path and os.path.splitext(storage_path)[0] + "_state.json"
    if not path or not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f).get("last_sample")
