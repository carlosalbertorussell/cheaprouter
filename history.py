"""
Routing history + spend analytics for cheaprouter (S1).

Records go through a pluggable storage backend (storage.py): Supabase when
configured, JSONL otherwise. Every record is privacy-clean by construction —
routing metadata only, never keys, never message content (Invariant A).

Session attribution (S1.3): callers pass an opaque `session` token. Reads and
spend reports are scoped to that token, so one user cannot see another's spend
(Invariant B). The server never maps a session to an identity — it is just an
opaque grouping key the caller supplies.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Optional

from storage import get_backend, build_record


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


# ─── Writing ──────────────────────────────────────────────────────────────────

def log_decision(
    decision_dict: dict,
    session: Optional[str] = None,
    actual_input_tokens: Optional[int] = None,
    actual_output_tokens: Optional[int] = None,
    actual_latency_ms: Optional[int] = None,
    actual_cost_usd: Optional[float] = None,
    error: Optional[str] = None,
) -> None:
    """Persist one routing record via the active backend. Best-effort."""
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
    )
    get_backend().write(record)


# ─── Reading ──────────────────────────────────────────────────────────────────

def read_history(limit: int = 50, session: Optional[str] = None) -> list[dict]:
    """Recent records, newest first, scoped to a session when given."""
    scope = normalize_session(session) if session is not None else None
    return get_backend().read(scope, limit)


def history_summary(limit: int = 50, session: Optional[str] = None) -> dict:
    """Aggregate stats over recent records (optionally session-scoped)."""
    records = read_history(limit, session)
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
) -> dict:
    """
    Spend broken down by provider, by tier, and by day, with total saved vs.
    baseline. `since` is an ISO date/datetime string; records older are excluded.
    """
    records = read_history(limit, session)
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


# ─── Budgets (S1.5) ───────────────────────────────────────────────────────────
#
# Budgets are stored in the same backend as ordinary spend records, tagged with
# a sentinel provider_id so they round-trip through the identical clean schema.
# A budget "record" reuses cost_usd to carry the limit. This keeps the store
# single-table and avoids a second schema.

_BUDGET_MARKER = "__budget__"


def set_budget(session: Optional[str], monthly_usd: float) -> dict:
    """Record a monthly budget for a session. Returns the stored budget."""
    from storage import build_record as _br
    rec = _br(
        session=normalize_session(session),
        provider_id=_BUDGET_MARKER,
        provider_name=None, region=None, tier=None,
        input_tokens=None, output_tokens=None,
        cost_usd=float(monthly_usd), saved_usd=None,
        latency_ms=None, success=True,
    )
    get_backend().write(rec)
    return {"session_scoped": True, "monthly_budget_usd": round(float(monthly_usd), 2)}


def get_budget(session: Optional[str], limit: int = 500) -> Optional[float]:
    """Most recent budget set for this session, or None."""
    for r in read_history(limit, session):
        if r.get("provider_id") == _BUDGET_MARKER:
            return r.get("cost_usd")
    return None


def budget_status(session: Optional[str], limit: int = 500) -> Optional[dict]:
    """
    Current month spend vs. budget for a session. None if no budget is set.
    Real spend excludes budget-marker records.
    """
    budget = get_budget(session, limit)
    if budget is None:
        return None

    from datetime import datetime, timezone
    month_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    spent = 0.0
    for r in read_history(limit, session):
        if r.get("provider_id") == _BUDGET_MARKER:
            continue
        if (r.get("timestamp") or "").startswith(month_prefix):
            spent += r.get("cost_usd") or 0

    pct = (spent / budget * 100) if budget > 0 else 0.0
    return {
        "monthly_budget_usd": round(budget, 2),
        "spent_this_month_usd": round(spent, 6),
        "used_pct": round(pct, 1),
        "over_budget": spent > budget,
    }
