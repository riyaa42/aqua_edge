"""Stdlib-only Gateway-to-Dashboard HTTP shipper with durable JSONL outbox.

Implements the v1 ingest contract in dashboard/API_SPEC.md:
- Persistent monotonic sequence numbers across process restarts.
- Append-before-send JSONL disk outbox with atomic state/compaction writes.
- Bounded batch delivery (max 50 frames) in ascending sequence order.
- Bearer token authentication, bounded exponential backoff with jitter,
  and strict ACK validation before removing frames from the outbox.
"""
import json
import os
import random
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

SCHEMA_VERSION = 1
MAX_BATCH_SIZE = 50
MAX_PAYLOAD_BYTES = 1_048_576
VALID_MODES = ("edge", "cloud")


class OutboxStorageError(RuntimeError):
    """Raised when disk space or outbox quota is critically low; old telemetry is preserved."""


def _save_atomic(path, text):
    """Write text to a temp file in the same directory and atomically replace target."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


class DashboardShipper:
    """Durable store-and-forward Internet shipper for Gateway.snapshot() frames."""

    def __init__(
        self,
        endpoint,
        storage_dir,
        gateway_id="building-B",
        mode="edge",
        auth_token="",
        max_batch=MAX_BATCH_SIZE,
        timeout_s=5.0,
        base_backoff_s=2.0,
        max_backoff_s=30.0,
        max_outbox_bytes=50 * 1024 * 1024,
        min_free_bytes=100 * 1024 * 1024,
        warn_queue_frames=500,
        warn_oldest_age_s=3600.0,
        jitter_fn=None,
        clock_fn=None,
    ):
        if mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {VALID_MODES}, got {mode!r}")
        if not gateway_id:
            raise ValueError("gateway_id must be non-empty")
        if not (1 <= int(max_batch) <= MAX_BATCH_SIZE):
            raise ValueError(f"max_batch must be between 1 and {MAX_BATCH_SIZE}")

        self.endpoint = endpoint
        self.storage_dir = os.path.abspath(storage_dir)
        self.gateway_id = gateway_id
        self.mode = mode
        self.auth_token = auth_token
        self.max_batch = int(max_batch)
        self.timeout_s = float(timeout_s)
        self.base_backoff_s = float(base_backoff_s)
        self.max_backoff_s = float(max_backoff_s)
        self.max_outbox_bytes = int(max_outbox_bytes)
        self.min_free_bytes = int(min_free_bytes)
        self.warn_queue_frames = int(warn_queue_frames)
        self.warn_oldest_age_s = float(warn_oldest_age_s)
        self._jitter = jitter_fn or (lambda: random.uniform(0.8, 1.2))
        self._clock = clock_fn or time.time

        os.makedirs(self.storage_dir, exist_ok=True)
        self.state_path = os.path.join(self.storage_dir, "shipper_state.json")
        self.outbox_path = os.path.join(self.storage_dir, "outbox.jsonl")

        self.next_seq = 1
        self.delivered_frames = 0
        self.retries = 0
        self.consecutive_failures = 0
        self.last_successful_delivery_utc = None
        self.next_attempt_at = 0.0
        self.connected = None
        self.last_error = None
        self.queue = []

        self._load()

    def _load(self):
        if os.path.exists(self.state_path):
            with open(self.state_path, encoding="utf-8") as f:
                st = json.load(f)
            self.next_seq = max(1, int(st.get("next_seq", 1)))
            self.delivered_frames = int(st.get("delivered_frames", 0))
            self.retries = int(st.get("retries", 0))
            self.last_successful_delivery_utc = st.get("last_successful_delivery_utc")

        loaded = []
        if os.path.exists(self.outbox_path):
            with open(self.outbox_path, encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        rec = json.loads(s)
                        if isinstance(rec, dict) and "seq" in rec and "snapshot" in rec:
                            # Any frame still on disk after restart was buffered in the outbox.
                            rec["buffered"] = True
                            rec.setdefault("mode", self.mode)
                            loaded.append(rec)
                    except json.JSONDecodeError:
                        # Tolerate a truncated trailing line after an abrupt power loss.
                        continue
        loaded.sort(key=lambda r: int(r["seq"]))
        self.queue = loaded
        if self.queue:
            self.next_seq = max(self.next_seq, int(self.queue[-1]["seq"]) + 1)

    def _save_state(self):
        payload = {
            "next_seq": self.next_seq,
            "delivered_frames": self.delivered_frames,
            "retries": self.retries,
            "last_successful_delivery_utc": self.last_successful_delivery_utc,
        }
        _save_atomic(self.state_path, json.dumps(payload))

    def _rewrite_outbox(self):
        body = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in self.queue)
        _save_atomic(self.outbox_path, body)

    def set_mode(self, mode):
        """Switch mode for future frames while preserving original mode on already-queued frames."""
        if mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {VALID_MODES}, got {mode!r}")
        self.mode = mode

    def _storage_status(self):
        outbox_bytes = os.path.getsize(self.outbox_path) if os.path.exists(self.outbox_path) else 0
        free_bytes = shutil.disk_usage(self.storage_dir).free
        return outbox_bytes, free_bytes

    def enqueue(self, snapshot, new_events=None, created_at_utc=None):
        """Persist a snapshot frame to the disk outbox before any network send."""
        ts = float(self._clock() if created_at_utc is None else created_at_utc)
        rec = {
            "seq": self.next_seq,
            "mode": self.mode,
            "created_at_utc": ts,
            "buffered": bool(self.queue),
            "snapshot": snapshot,
            "new_events": list(new_events) if new_events else [],
        }
        line = json.dumps(rec, separators=(",", ":")) + "\n"
        outbox_bytes, free_bytes = self._storage_status()
        if outbox_bytes + len(line.encode("utf-8")) > self.max_outbox_bytes:
            raise OutboxStorageError(
                f"outbox limit reached ({outbox_bytes} / {self.max_outbox_bytes} bytes); preserving {len(self.queue)} queued frames"
            )
        if free_bytes < self.min_free_bytes:
            raise OutboxStorageError(
                f"disk free space critically low ({free_bytes} < {self.min_free_bytes} bytes); preserving {len(self.queue)} queued frames"
            )

        seq = self.next_seq
        self.next_seq += 1
        with open(self.outbox_path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        self.queue.append(rec)
        self._save_state()
        return seq

    def _wire_frame(self, rec):
        return {
            "seq": rec["seq"],
            "created_at_utc": rec["created_at_utc"],
            "buffered": rec["buffered"],
            "snapshot": rec["snapshot"],
            "new_events": rec.get("new_events", []),
        }

    def _post_batch(self, batch):
        batch_mode = batch[0].get("mode", self.mode)
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "gateway_id": self.gateway_id,
            "mode": batch_mode,
            "frames": [self._wire_frame(r) for r in batch],
        }
        raw = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"batch payload {len(raw)} bytes exceeds {MAX_PAYLOAD_BYTES} byte limit")

        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        req = urllib.request.Request(self.endpoint, data=raw, headers=headers, method="POST")
        host = (urllib.parse.urlparse(self.endpoint).hostname or "").lower()
        if host in ("127.0.0.1", "localhost", "::1"):
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            cm = opener.open(req, timeout=self.timeout_s)
        else:
            cm = urllib.request.urlopen(req, timeout=self.timeout_s)
        with cm as resp:
            if resp.status < 200 or resp.status >= 300:
                raise RuntimeError(f"unexpected HTTP status {resp.status}")
            data = json.loads(resp.read().decode("utf-8"))
        return data

    def _build_batch(self):
        """Build up to max_batch frames sharing the same mode and staying under MAX_PAYLOAD_BYTES."""
        batch = []
        batch_mode = self.queue[0].get("mode", self.mode)
        for rec in self.queue[: self.max_batch]:
            if rec.get("mode", self.mode) != batch_mode:
                break
            candidate = batch + [rec]
            raw = json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "gateway_id": self.gateway_id,
                    "mode": batch_mode,
                    "frames": [self._wire_frame(r) for r in candidate],
                },
                separators=(",", ":"),
            ).encode("utf-8")
            if len(raw) > MAX_PAYLOAD_BYTES:
                if not batch:
                    raise ValueError(f"single frame seq={rec['seq']} exceeds {MAX_PAYLOAD_BYTES} bytes")
                break
            batch = candidate
        return batch

    def _mark_failure(self, err, now):
        self.connected = False
        self.last_error = str(err)
        self.retries += 1
        self.consecutive_failures += 1
        changed = False
        for rec in self.queue:
            if not rec.get("buffered"):
                rec["buffered"] = True
                changed = True
        if changed:
            self._rewrite_outbox()
        delay = min(self.max_backoff_s, self.base_backoff_s * (2 ** (self.consecutive_failures - 1)))
        delay *= float(self._jitter())
        self.next_attempt_at = now + delay
        self._save_state()

    def flush_once(self, force=False):
        """Attempt to send one ordered batch (<= max_batch). Returns True if queue emptied or no pending."""
        if not self.queue:
            return True
        now = float(self._clock())
        if not force and now < self.next_attempt_at:
            return False

        batch = self._build_batch()
        try:
            ack = self._post_batch(batch)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
            self._mark_failure(exc, now)
            return False

        if (
            not isinstance(ack, dict)
            or ack.get("ok") is not True
            or ack.get("gateway_id") != self.gateway_id
            or not isinstance(ack.get("acked_seq"), int)
        ):
            self._mark_failure("invalid ACK payload from dashboard", now)
            return False

        acked_seq = ack["acked_seq"]
        sent_seqs = {int(r["seq"]) for r in batch}
        acked_count = sum(1 for s in sent_seqs if s <= acked_seq)
        if acked_count == 0 and batch and acked_seq < int(batch[0]["seq"]):
            self._mark_failure(f"stale acked_seq={acked_seq} < batch[0]={batch[0]['seq']}", now)
            return False

        remaining = [r for r in self.queue if int(r["seq"]) > acked_seq]
        removed = len(self.queue) - len(remaining)
        self.queue = remaining
        self.delivered_frames += removed
        self.consecutive_failures = 0
        self.next_attempt_at = 0.0
        self.connected = True
        self.last_error = None
        self.last_successful_delivery_utc = now
        self._rewrite_outbox()
        self._save_state()
        return len(self.queue) == 0

    def flush_all(self, force=False, max_batches=100):
        """Flush pending batches sequentially until empty, backed off, or max_batches reached."""
        for _ in range(max_batches):
            if not self.queue:
                return True
            before = len(self.queue)
            self.flush_once(force=force)
            if len(self.queue) == before:
                return False
        return len(self.queue) == 0

    def publish(self, snapshot, new_events=None, created_at_utc=None, force=False):
        """Persist snapshot to disk outbox and attempt immediate delivery."""
        seq = self.enqueue(snapshot, new_events=new_events, created_at_utc=created_at_utc)
        self.flush_once(force=force)
        return seq

    def metrics(self):
        """Return operational metrics and storage warnings for the shipper and outbox."""
        now = float(self._clock())
        oldest = self.queue[0]["created_at_utc"] if self.queue else None
        age_s = round(max(0.0, now - oldest), 2) if oldest is not None else 0.0
        outbox_bytes, free_bytes = self._storage_status()
        warnings = []
        if len(self.queue) >= self.warn_queue_frames:
            warnings.append("queue_size_high")
        if oldest is not None and age_s >= self.warn_oldest_age_s:
            warnings.append("oldest_frame_stale")
        if self.max_outbox_bytes > 0 and outbox_bytes >= 0.8 * self.max_outbox_bytes:
            warnings.append("outbox_near_limit")
        if free_bytes < self.min_free_bytes * 2:
            warnings.append("disk_space_low")
        storage_critical = outbox_bytes >= self.max_outbox_bytes or free_bytes < self.min_free_bytes
        return {
            "gateway_id": self.gateway_id,
            "mode": self.mode,
            "pending_frames": len(self.queue),
            "delivered_frames": self.delivered_frames,
            "retries": self.retries,
            "last_successful_delivery_utc": self.last_successful_delivery_utc,
            "oldest_queued_created_at_utc": oldest,
            "oldest_queued_age_s": age_s,
            "next_seq": self.next_seq,
            "connected": self.connected,
            "last_error": self.last_error,
            "outbox_bytes": outbox_bytes,
            "disk_free_bytes": free_bytes,
            "storage_warning": bool(warnings),
            "storage_critical": storage_critical,
            "warnings": warnings,
        }
