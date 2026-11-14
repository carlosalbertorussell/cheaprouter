"""
Routing history + spend analytics for cheaprouter (S1).

Records go through a pluggable storage backend (storage.py): Upstash Redis when
configured, JSONL otherwise. Every record is privacy-clean by construction —
routing metadata only, never keys, never message content (Invariant A).

Session attribution (S1.3): callers pass an opaque `session` token, stored only
as a hash. The server never maps a session to an identity — it is just an opaque
grouping key the caller supplies.

Read scoping (Invariant B, enforced at the tool layer since ST5): the
caller-facing history and spend tools read only the caller's own records. A call
with a `session` token reads that token's records; a call without one reads the
shared "anon" session's records, never everyone's. A global, all-sessions view
exists only when the operator sets ARBITRAGE_ALLOW_GLOBAL_READS=1 (see
caller_scope). Note that "anon" is shared by every caller who omits a token, so
those callers can see each other's anon records; a private view needs a token.

The functions below take `session=None` to mean *unscoped* (every session).
That is for internal, server-side aggregates such as health scoring (health.py)
and tests; tool code must resolve the caller's scope with caller_scope() and
pass the resulting `key`.
"""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from typing import Optional

from storage import (
    BUDGET_MARKER, budget_snapshot, build_record, is_budget_marker, load_budget,
    month_key, read_records, save_budget, write_record,
)


# ─── Session tokens ───────────────────────────────────────────────────────────

DEFAULT_SESSION = "anon"


def normalize_session(session: Optional[str]) -> str:
    """
    Reduce a caller-supplied session token to an opaque, fixed-length key.

    We hash it so that whatever the caller passes (which might be something they
    consider low-sensitivity but we shouldn't store verbatim) becomes an opaque
    identifier holding no PII. Same input always maps to the same key, so a
    caller can query their own spend across requests.
    """
    if not session:
        return DEFAULT_SESSION
    return "s_" + hashlib.sha256(session.encode("utf-8")).hexdigest()[:16]


# ─── Caller read scope (ST5) ──────────────────────────────────────────────────

GLOBAL_READS_ENV = "ARBITRAGE_ALLOW_GLOBAL_READS"
SCOPE_SESSION = "session"          # the caller's own token
SCOPE_ANON = "anon"                # no token: the shared anon session
SCOPE_ALL = "all_sessions"         # operator opt-in only


def global_reads_allowed() -> bool:
    """True only when the operator explicitly opted in to all-sessions reads."""
    return os.getenv(GLOBAL_READS_ENV, "").strip().lower() in ("1", "true", "yes")


def caller_scope(session: Optional[str]) -> tuple[Optional[str], str]:
    """
    The storage key a tool caller may read, and a label for it.

    Returns (key, scope): key is a normalized session key, or None for an
    unscoped all-sessions read — which happens only when no session was passed
    AND the operator set ARBITRAGE_ALLOW_GLOBAL_READS=1. Otherwise a missing
    session reads the "anon" session, never everyone's records.
    """
    if session:
        return normalize_session(session), SCOPE_SESSION
    if global_reads_allowed():
        return None, SCOPE_ALL
    return DEFAULT_SESSION, SCOPE_ANON


_UNSET = object()


def _resolve_key(session: Optional[str], key) -> Optional[str]:
    if key is not _UNSET:
        return key                 # already resolved (caller_scope); None = all
    return normalize_session(session) if session is not None else None


# ─── Writing ──────────────────────────────────────────────────────────────────

def log_decision(
    decision_dict: dict,
    session: Optional[str] = None,
    actual_input_tokens: Optional[int] = None,
    actual_output_tokens: Optional[int] = None,
    actual_latency_ms: Optional[int] = None,
    actual_cost_usd: Optional[float] = None,
    error: Optional[str] = None,
    transient: Optional[bool] = None,
) -> None:
    """
    Persist one routing record via the active backend. Best-effort.

    `transient` classifies a failure (pass client.is_transient_error(exc)): only
    transient, provider-side failures count against provider health (ST5).
    """
    winner = (decision_dict or {}).get("winner") or {}
    savings = (decision_dict or {}).get("savings") or {}

    record = build_record(
        session=normalize_session(session),
        provider_id=winner.get("provider_id"),
        provider_name=winner.get("provider_name"),
        region=winner.get("region"),
        tier=(decision_dict or {}).get("tier"),
        input_tokens=actual_input_tokens,
        output_tokens=actual_output_tokens,
        cost_usd=actual_cost_usd if actual_cost_usd is not None else winner.get("total_cost_usd"),
        saved_usd=savings.get("saved_usd"),
        latency_ms=actual_latency_ms,
        success=error is None,
        error=error,
        transient=transient,
    )
    write_record(record)


# ─── Reading ──────────────────────────────────────────────────────────────────

def read_history(limit: int = 50, session: Optional[str] = None, *, key=_UNSET) -> list[dict]:
    """
    Recent routing records, newest first, scoped to a session when given.
    `session=None` reads every session (internal use only, see module doc);
    `key` takes an already-resolved storage key from caller_scope() instead.
    Legacy budget markers (pre-ST3 budgets stored as history records) are not
    routing records and are filtered out, so analytics never count them.
    """
    scope = _resolve_key(session, key)
    return [r for r in read_records(scope, limit) if not is_budget_marker(r)]


def history_summary(limit: int = 50, session: Optional[str] = None, *, key=_UNSET) -> dict:
    """Aggregate stats over recent records (optionally session-scoped)."""
    records = read_history(limit, session, key=key)
    if not records:
        return {"total_records": 0, "message": "No history found."}

    total_cost = total_saved = 0.0
    provider_counts: dict[str, int] = defaultdict(int)
    successful = failed = 0

    for r in records:
        if r.get("success"):
            successful += 1
        else:
            failed += 1
        pid = r.get("provider_id") or "unknown"
        provider_counts[pid] += 1
        total_cost += r.get("cost_usd") or 0
        total_saved += r.get("saved_usd") or 0

    return {
        "total_records": len(records),
        "successful": successful,
        "failed": failed,
        "total_cost_usd": round(total_cost, 6),
        "total_saved_usd": round(total_saved, 6),
        "provider_usage": dict(provider_counts),
    }


# ─── Spend report (S1.4) ──────────────────────────────────────────────────────

def spend_report(
    session: Optional[str] = None,
    limit: int = 500,
    since: Optional[str] = None,
    *,
    key=_UNSET,
) -> dict:
    """
    Spend broken down by provider, by tier, and by day, with total saved vs.
    baseline. `since` is an ISO date/datetime string; records older are excluded.
    """
    records = read_history(limit, session, key=key)
    if since:
        records = [r for r in records if (r.get("timestamp") or "") >= since]
    if not records:
        return {"total_records": 0, "message": "No spend in range."}

    by_provider: dict[str, float] = defaultdict(float)
    by_tier: dict[str, float] = defaultdict(float)
    by_day: dict[str, float] = defaultdict(float)
    total_cost = total_saved = 0.0

    for r in records:
        cost = r.get("cost_usd") or 0
        saved = r.get("saved_usd") or 0
        total_cost += cost
        total_saved += saved
        by_provider[r.get("provider_name") or "unknown"] += cost
        by_tier[r.get("tier") or "unknown"] += cost
        day = (r.get("timestamp") or "")[:10]  # YYYY-MM-DD
        if day:
            by_day[day] += cost

    rnd = lambda d: {k: round(v, 6) for k, v in d.items()}
    return {
        "total_records": len(records),
        "total_cost_usd": round(total_cost, 6),
        "total_saved_usd": round(total_saved, 6),
        "spend_by_provider": rnd(by_provider),
        "spend_by_tier": rnd(by_tier),
        "spend_by_day": rnd(dict(sorted(by_day.items()))),
    }


# ─── Budgets (S1.5, storage reworked in ST3) ──────────────────────────────────
#
# Budgets are stored apart from the spend history (Upstash `budget:{session}`,
# or a small JSON file next to the JSONL history), so they never fall out of a
# read window or get trimmed with old records. The month's spend comes from a
# per-month counter (Upstash) or a full scan (JSONL), so it is correct however
# many calls a session makes. Budgets set before ST3 were history records with
# a sentinel provider_id; they are still honoured as a fallback.

_BUDGET_MARKER = BUDGET_MARKER   # kept for callers/tests that referenced it


def set_budget(session: Optional[str], monthly_usd: float) -> dict:
    """Record a monthly budget for a session. Returns the stored budget."""
    save_budget(normalize_session(session), float(monthly_usd))
    return {"session_scoped": True, "monthly_budget_usd": round(float(monthly_usd), 2)}


def get_budget(session: Optional[str], limit: int = 500) -> Optional[float]:
    """Most recent budget set for this session, or None."""
    return load_budget(normalize_session(session))


def budget_status(session: Optional[str], limit: int = 500) -> Optional[dict]:
    """
    Current month spend vs. budget for a session. None if no budget is set.
    One backend read serves both the budget lookup and the month's spend.
    `limit` bounds the history window used only for pre-ST3 data on Upstash.
    """
    budget, spent = budget_snapshot(normalize_session(session), month_key(), limit)
    if budget is None:
        return None

    pct = (spent / budget * 100) if budget > 0 else 0.0
    return {
        "monthly_budget_usd": round(budget, 2),
        "spent_this_month_usd": round(spent, 6),
        "used_pct": round(pct, 1),
        "over_budget": spent > budget,
    }
