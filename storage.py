"""
Pluggable storage backend for cheaprouter routing history (S1.1).

Two backends, selected automatically at import time:

  - UpstashBackend   — used when UPSTASH_REDIS_REST_URL and
                       UPSTASH_REDIS_REST_TOKEN are set. Durable across MCPize
                       redeploys. Per-session Redis lists over the HTTP REST API.
  - JSONLBackend     — fallback for local/self-hosted use. Appends to the
                       file at ARBITRAGE_HISTORY_FILE (default /tmp).

Both implement the same StorageBackend interface so history.py never needs to
know which is active. Self-hosters are never forced onto Upstash (Invariant C).

PRIVACY (Invariants A & B): records carry only routing metadata — timestamp,
provider, tier, region, token counts, cost, latency, savings, success flag, and
an opaque session token. Never API keys, never message content. The record
schema is enforced by build_record(); callers cannot inject arbitrary fields.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

import httpx


# ─── Record construction (single source of truth for the schema) ──────────────

# The exact set of fields allowed in a stored record. Anything not here is
# dropped before persistence — this is what makes the privacy invariant testable.
ALLOWED_FIELDS = frozenset({
    "timestamp",
    "session",
    "provider_id",
    "provider_name",
    "region",
    "tier",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "saved_usd",
    "latency_ms",
    "success",
    "error_class",
})


def build_record(
    *,
    session: str,
    provider_id: Optional[str],
    provider_name: Optional[str],
    region: Optional[str],
    tier: Optional[str],
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    cost_usd: Optional[float],
    saved_usd: Optional[float],
    latency_ms: Optional[int],
    success: bool,
    error: Optional[str] = None,
) -> dict:
    """
    Build a privacy-clean history record. This is the ONLY way records are made.

    Note there is deliberately no field for message content or API keys. `error`
    is reduced to an error *class* (the exception type name), never the full
    message, since provider error bodies can echo request content.
    """
    error_class = None
    if error:
        # Keep only a short, safe classifier — never the full error body.
        error_class = error.strip().split(":")[0][:60]
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session": session,
        "provider_id": provider_id,
        "provider_name": provider_name,
        "region": region,
        "tier": tier,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(cost_usd, 8) if cost_usd is not None else None,
        "saved_usd": round(saved_usd, 8) if saved_usd is not None else None,
        "latency_ms": latency_ms,
        "success": success,
        "error_class": error_class,
    }


def sanitize(record: dict) -> dict:
    """Drop any field not in ALLOWED_FIELDS. Defense in depth for the invariant."""
    return {k: v for k, v in record.items() if k in ALLOWED_FIELDS}


# ─── Backend interface ────────────────────────────────────────────────────────

class StorageBackend(Protocol):
    def write(self, record: dict) -> None: ...
    def read(self, session: Optional[str], limit: int) -> list[dict]: ...
    @property
    def name(self) -> str: ...


# ─── JSONL backend (local / self-hosted fallback) ─────────────────────────────

class JSONLBackend:
    def __init__(self, path: Optional[str] = None):
        self.path = Path(path or os.getenv("ARBITRAGE_HISTORY_FILE", "/tmp/routing_history.jsonl"))

    @property
    def name(self) -> str:
        return "jsonl"

    def write(self, record: dict) -> None:
        rec = sanitize(record)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def read(self, session: Optional[str], limit: int) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        out = []
        for line in reversed(lines):  # newest first
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if session is not None and rec.get("session") != session:
                continue
            out.append(rec)
            if len(out) >= limit:
                break
        return out


# ─── Upstash Redis backend (durable serverless store) ─────────────────────────

def _upstash_timeout() -> float:
    """Per-request timeout for Upstash calls. Short: history is best-effort."""
    try:
        return max(0.1, float(os.getenv("UPSTASH_TIMEOUT_S", "2.0")))
    except ValueError:
        return 2.0


class UpstashBackend:
    """
    Upstash Redis over its HTTP REST API — no SDK, no connection pooling, which
    suits MCPize's serverless container model.

    Data model:
      history:{session}   → Redis list per session; each element is one JSON
                            record. LPUSH on write (newest first), LRANGE on read.
      history:recent      → Redis list of the most recent records across ALL
                            sessions (capped). Unscoped reads (health scoring,
                            global history) use it: one request, however many
                            sessions exist.
      history:sessions    → Redis set of every session key seen. Only used as a
                            fallback for unscoped reads when history:recent is
                            empty (data written before it existed).

    Session isolation is physical: each session's records live under a separate
    key, so a scoped read can only ever touch one session's list.

    Every write is a single /pipeline request (LPUSH + LTRIM per list + SADD),
    with a short timeout (UPSTASH_TIMEOUT_S, default 2s).
    """

    MAX_PER_SESSION = 5000   # cap list length so a session can't grow unbounded
    MAX_RECENT = 5000        # cap on the global recent-records list
    RECENT_KEY = "history:recent"
    SESSIONS_KEY = "history:sessions"

    def __init__(self, url: str, token: str):
        self.base = url.rstrip("/")
        self.token = token

    @property
    def name(self) -> str:
        return "upstash"

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def _cmd(self, *args) -> Optional[object]:
        """
        Run one Redis command via the REST API: POST a JSON array of the command
        and its arguments. Returns the `result` field, or None on any failure —
        history is best-effort and must never break routing.
        """
        try:
            with httpx.Client(timeout=_upstash_timeout()) as c:
                r = c.post(self.base, headers=self._headers, json=list(args))
                r.raise_for_status()
                return r.json().get("result")
        except Exception:
            return None

    def _pipeline(self, *commands: list) -> list:
        """
        Run several Redis commands in ONE HTTP request via Upstash's /pipeline
        endpoint. Returns one result per command (None for a command that
        errored). On any transport failure, returns all-None — best-effort.
        """
        if not commands:
            return []
        try:
            with httpx.Client(timeout=_upstash_timeout()) as c:
                r = c.post(f"{self.base}/pipeline", headers=self._headers,
                           json=[list(cmd) for cmd in commands])
                r.raise_for_status()
                body = r.json()
            if not isinstance(body, list) or len(body) != len(commands):
                return [None] * len(commands)
            return [item.get("result") if isinstance(item, dict) and "error" not in item else None
                    for item in body]
        except Exception:
            return [None] * len(commands)

    def write(self, record: dict) -> None:
        rec = sanitize(record)
        session = rec.get("session", "anon")
        key = f"history:{session}"
        payload = json.dumps(rec)
        # newest-first per-session list + global recent list, both capped, and
        # the session registered in the index — all in a single request.
        self._pipeline(
            ["LPUSH", key, payload],
            ["LTRIM", key, 0, self.MAX_PER_SESSION - 1],
            ["LPUSH", self.RECENT_KEY, payload],
            ["LTRIM", self.RECENT_KEY, 0, self.MAX_RECENT - 1],
            ["SADD", self.SESSIONS_KEY, key],
        )

    def read(self, session: Optional[str], limit: int) -> list[dict]:
        if session is not None:
            return self._read_key(f"history:{session}", limit)

        # Unscoped: one LRANGE on the global recent list.
        recent = self._decode(self._cmd("LRANGE", self.RECENT_KEY, 0, limit - 1))
        if recent:
            return recent[:limit]

        # Fallback for data written before history:recent existed: fan out across
        # the session lists, but in one pipelined request rather than one per key.
        keys = self._cmd("SMEMBERS", self.SESSIONS_KEY) or []
        if not keys:
            return []
        results = self._pipeline(*[["LRANGE", k, 0, limit - 1] for k in keys])
        merged: list[dict] = []
        for raw in results:
            merged.extend(self._decode(raw))
        merged.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
        return merged[:limit]

    def _read_key(self, key: str, limit: int) -> list[dict]:
        return self._decode(self._cmd("LRANGE", key, 0, limit - 1))

    @staticmethod
    def _decode(raw) -> list[dict]:
        out = []
        for item in raw or []:
            try:
                out.append(json.loads(item))
            except (json.JSONDecodeError, TypeError):
                continue
        return out


# ─── Background writer (keeps remote writes off the routing path) ─────────────

class _BackgroundWriter:
    """
    A single daemon thread that drains a bounded queue of (backend, record)
    writes. One thread keeps writes in order. If the queue is full (the remote
    store is down and writes are backing up) new writes are dropped — history
    is best-effort and must never slow routing down.
    """

    def __init__(self, maxsize: int = 1000):
        self._q: "queue.Queue[tuple]" = queue.Queue(maxsize=maxsize)
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.dropped = 0

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="cheaprouter-history-writer", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            backend, record = self._q.get()
            try:
                backend.write(record)
            except Exception:
                pass   # best-effort
            finally:
                self._q.task_done()

    def submit(self, backend, record: dict) -> None:
        self._ensure_thread()
        try:
            self._q.put_nowait((backend, record))
        except queue.Full:
            self.dropped += 1

    def pending(self) -> int:
        return self._q.unfinished_tasks

    def flush(self, timeout: Optional[float] = None) -> bool:
        """Wait until queued writes have finished. True if drained in time."""
        q = self._q
        deadline = None if timeout is None else time.monotonic() + timeout
        with q.all_tasks_done:
            while q.unfinished_tasks:
                if deadline is None:
                    q.all_tasks_done.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                q.all_tasks_done.wait(remaining)
        return True


_writer = _BackgroundWriter()
atexit.register(lambda: _writer.flush(timeout=_upstash_timeout()))


def write_record(record: dict) -> None:
    """
    Persist one record via the active backend.

    The backend is resolved NOW (on the caller's thread), so the write goes to
    the backend configured at call time. Remote backends (Upstash) are written
    in the background so a slow or unreachable store never delays routing; the
    local JSONL backend is written inline (it is a fast local append).
    """
    backend = get_backend()
    if backend.name == "upstash":
        _writer.submit(backend, record)
    else:
        backend.write(record)


def read_records(session: Optional[str], limit: int) -> list[dict]:
    """
    Read recent records via the active backend. Waits briefly for this process's
    pending background writes first, so a read sees writes made just before it.
    """
    if _writer.pending():
        _writer.flush(timeout=_upstash_timeout() + 0.5)
    return get_backend().read(session, limit)


def flush_writes(timeout: Optional[float] = None) -> bool:
    """Wait for pending background writes (for tests and shutdown)."""
    return _writer.flush(timeout)


# ─── Backend selection ────────────────────────────────────────────────────────

def get_backend() -> StorageBackend:
    """Upstash when configured, else JSONL. Called fresh so tests can monkeypatch env."""
    url = os.getenv("UPSTASH_REDIS_REST_URL")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
    if url and token:
        return UpstashBackend(url, token)
    return JSONLBackend()
