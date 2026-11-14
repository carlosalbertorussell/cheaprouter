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
provider, tier, region, token counts, cost, latency, savings, success flag, a
transient-failure flag, and an opaque session token. Never API keys, never message content. The record
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
    "transient",
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
    transient: Optional[bool] = None,
) -> dict:
    """
    Build a privacy-clean history record. This is the ONLY way records are made.

    Note there is deliberately no field for message content or API keys. `error`
    is reduced to an error *class* (the exception type name), never the full
    message, since provider error bodies can echo request content.

    `transient` (ST5) says whether a failure was provider-side and transient
    (429, 5xx, timeout, connection error — classified by the caller with
    client.is_transient_error). Health scoring counts only those failures, so a
    caller's own bad key or bad request never counts against a provider. It is a
    bare boolean: nothing about the caller or the request. Successes store
    False; a failure the caller did not classify stores None (never counted).
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
        "transient": False if success else (bool(transient) if transient is not None else None),
    }


def sanitize(record: dict) -> dict:
    """Drop any field not in ALLOWED_FIELDS. Defense in depth for the invariant."""
    return {k: v for k, v in record.items() if k in ALLOWED_FIELDS}


# ─── Budgets and monthly spend (ST3) ──────────────────────────────────────────
#
# Budgets are stored apart from the spend history (a dedicated key / file), so
# they can never fall out of a read window or be trimmed with old records.
# Before ST3 a budget was a history record with this sentinel provider_id that
# reused cost_usd for the limit. New budgets are never written that way; legacy
# markers are still honoured as a fallback and filtered out of analytics.

BUDGET_MARKER = "__budget__"


def is_budget_marker(record: dict) -> bool:
    return (record or {}).get("provider_id") == BUDGET_MARKER


def month_key(timestamp: Optional[str] = None) -> str:
    """'YYYY-MM' of an ISO timestamp, or of now (UTC)."""
    if timestamp:
        return timestamp[:7]
    return datetime.now(timezone.utc).strftime("%Y-%m")


def legacy_budget(records: list[dict]) -> Optional[float]:
    """Newest pre-ST3 budget marker among records (newest first), or None."""
    for r in records:
        if is_budget_marker(r) and isinstance(r.get("cost_usd"), (int, float)):
            return float(r["cost_usd"])
    return None


def month_spend_from(records: list[dict], month: str) -> float:
    """Sum cost_usd of real spend records in `month` (budget markers excluded)."""
    total = 0.0
    for r in records:
        if is_budget_marker(r):
            continue
        if (r.get("timestamp") or "").startswith(month):
            total += r.get("cost_usd") or 0
    return total


# ─── Backend interface ────────────────────────────────────────────────────────

class StorageBackend(Protocol):
    def write(self, record: dict) -> None: ...
    def read(self, session: Optional[str], limit: int) -> list[dict]: ...
    def set_budget(self, session: str, monthly_usd: float) -> None: ...
    def get_budget(self, session: str) -> Optional[float]: ...
    def budget_snapshot(self, session: str, month: str, limit: int) -> tuple[Optional[float], float]:
        """(budget or None, spend in `month`) for one session, in as few reads as possible."""
        ...
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

    # ── budgets: a small JSON file next to the history file ──

    @property
    def budget_path(self) -> Path:
        env = os.getenv("ARBITRAGE_BUDGET_FILE")
        if env:
            return Path(env)
        return self.path.with_name(self.path.stem + ".budgets.json")

    def _load_budgets(self) -> dict:
        try:
            data = json.loads(self.budget_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def set_budget(self, session: str, monthly_usd: float) -> None:
        data = self._load_budgets()
        data[session] = {"monthly_usd": float(monthly_usd),
                         "set_at": datetime.now(timezone.utc).isoformat()}
        path = self.budget_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)          # atomic: a reader never sees a half-written file

    def _all_for(self, session: str) -> list[dict]:
        # Full scan, newest first: exact however many records the session has.
        return self.read(session, limit=10**12)

    def _dedicated_budget(self, session: str) -> Optional[float]:
        entry = self._load_budgets().get(session)
        if isinstance(entry, dict) and isinstance(entry.get("monthly_usd"), (int, float)):
            return float(entry["monthly_usd"])
        return None

    def get_budget(self, session: str) -> Optional[float]:
        b = self._dedicated_budget(session)
        return b if b is not None else legacy_budget(self._all_for(session))

    def budget_snapshot(self, session: str, month: str, limit: int) -> tuple[Optional[float], float]:
        records = self._all_for(session)
        budget = self._dedicated_budget(session)
        if budget is None:
            budget = legacy_budget(records)
        return budget, month_spend_from(records, month)


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
      budget:{session}    → the session's monthly budget in USD (a string). Kept
                            apart from the capped history lists so it is never
                            trimmed or pushed out of a read window (ST3).
      spend:{session}:{YYYY-MM}
                          → running total of the session's spend that month,
                            INCRBYFLOAT'd in the same write pipeline, expiring
                            after SPEND_TTL_S. Exact however many calls are made.

    Session isolation is physical: each session's records live under a separate
    key, so a scoped read can only ever touch one session's list.

    Every write is a single /pipeline request (LPUSH + LTRIM per list + SADD),
    with a short timeout (UPSTASH_TIMEOUT_S, default 2s).
    """

    MAX_PER_SESSION = 5000   # cap list length so a session can't grow unbounded
    MAX_RECENT = 5000        # cap on the global recent-records list
    RECENT_KEY = "history:recent"
    SESSIONS_KEY = "history:sessions"
    SPEND_TTL_S = 70 * 86400  # monthly counters outlive their month by ~5 weeks

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
        commands = [
            ["LPUSH", key, payload],
            ["LTRIM", key, 0, self.MAX_PER_SESSION - 1],
            ["LPUSH", self.RECENT_KEY, payload],
            ["LTRIM", self.RECENT_KEY, 0, self.MAX_RECENT - 1],
            ["SADD", self.SESSIONS_KEY, key],
        ]
        # Monthly spend counter, in the same request (ST3). Budget markers are
        # not spend; records without a cost add nothing.
        cost = rec.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and not is_budget_marker(rec):
            spend_key = self._spend_key(session, month_key(rec.get("timestamp")))
            commands += [["INCRBYFLOAT", spend_key, cost],
                         ["EXPIRE", spend_key, self.SPEND_TTL_S]]
        self._pipeline(*commands)

    # ── budgets and monthly spend ──

    @staticmethod
    def _budget_key(session: str) -> str:
        return f"budget:{session}"

    @staticmethod
    def _spend_key(session: str, month: str) -> str:
        return f"spend:{session}:{month}"

    @staticmethod
    def _as_float(raw) -> Optional[float]:
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def set_budget(self, session: str, monthly_usd: float) -> None:
        self._cmd("SET", self._budget_key(session), float(monthly_usd))

    def get_budget(self, session: str) -> Optional[float]:
        budget, _, _ = self._budget_and_records(session, None, 500)
        return budget

    def _budget_and_records(self, session: str, month: Optional[str], limit: int):
        """One pipelined request: dedicated budget, month counter, recent records."""
        cmds = [["GET", self._budget_key(session)]]
        if month:
            cmds.append(["GET", self._spend_key(session, month)])
        cmds.append(["LRANGE", f"history:{session}", 0, limit - 1])
        results = self._pipeline(*cmds)
        budget = self._as_float(results[0])
        counter = self._as_float(results[1]) if month else None
        records = self._decode(results[-1])
        if budget is None:
            budget = legacy_budget(records)
            if budget is not None:
                # Migrate a pre-ST3 marker so it survives the list being trimmed.
                # NX: never overwrite a budget set concurrently. Best-effort.
                self._cmd("SET", self._budget_key(session), budget, "NX")
        return budget, counter, records

    def budget_snapshot(self, session: str, month: str, limit: int) -> tuple[Optional[float], float]:
        budget, counter, records = self._budget_and_records(session, month, limit)
        # The counter is exact for spend written since ST3. In the month ST3 is
        # deployed, older spend exists only in the history list, so take the
        # larger of the two (they never double count: both are lower bounds).
        spent = max(counter or 0.0, month_spend_from(records, month))
        return budget, spent

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
    _flush_pending()
    return get_backend().read(session, limit)


def _flush_pending() -> None:
    if _writer.pending():
        _writer.flush(timeout=_upstash_timeout() + 0.5)


def save_budget(session: str, monthly_usd: float) -> None:
    """Store a session's monthly budget apart from spend history. Best-effort."""
    try:
        get_backend().set_budget(session, monthly_usd)
    except Exception:
        pass


def load_budget(session: str) -> Optional[float]:
    """The session's budget (dedicated store, else a legacy marker), or None."""
    _flush_pending()
    try:
        return get_backend().get_budget(session)
    except Exception:
        return None


def budget_snapshot(session: str, month: str, limit: int = 500) -> tuple[Optional[float], float]:
    """(budget or None, spend this month). Sees this process's pending writes."""
    _flush_pending()
    try:
        return get_backend().budget_snapshot(session, month, limit)
    except Exception:
        return None, 0.0


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
