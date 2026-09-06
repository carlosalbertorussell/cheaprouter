"""
Price history — the trajectory record (SP1b).

A snapshot answers "what does X cost?"; a *series* answers "how has X moved?" —
and under the financial-KPI framing (PRICING_STRATEGY.md) the series is the core
asset: a KPI's value is its trajectory, and a citable, provenanced record of LLM
price movement is the thing no scraper keeps well.

Design (per PRICING_SERVER_BLUEPRINT.md §7):
- **Append-only.** Each entry records one price change: what moved, from→to, with
  which provenance tier and source, when. Never mutated, never deleted.
- **Pluggable backend** (like S1) so the store choice isn't load-bearing:
    - FileHistoryBackend (default) — versioned append-only JSONL. Weekly-scale
      volume is tiny; git itself becomes the immutable audit log, which for a
      *certified* product is stronger provenance than a database. START HERE.
    - (later) Postgres/Timescale for live analytical queries at scale — same
      interface, swap without touching history logic.
- **Metadata only.** Prices + provenance. Never keys, never content — same
  invariant as S1, and it holds here too.

This module is standalone in SP1b: it defines the record, the store, capture, and
the query surface. Wiring capture into the verification flow is SP1d/SC8; nothing
here touches the router.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

from provenance import Tier

# Default history file — versioned in the repo so git is the audit log.
HISTORY_FILE = Path(os.getenv("PRICE_HISTORY_FILE", str(Path(__file__).parent / "price_history.jsonl")))

# Fields allowed in a history entry — the schema, enforced so nothing else leaks in.
_ALLOWED = frozenset({
    "ts", "provider", "model_id", "tier", "field", "old", "new",
    "provenance_tier", "source",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_entry(
    *,
    provider: str,
    model_id: str,
    tier: str,                    # capability tier: tier_fast/balanced/powerful
    field: str,                   # input_price_per_1m / output_price_per_1m / cached_input_price_per_1m
    old: Optional[float],
    new: float,
    provenance_tier: str,         # provenance.Tier value: verified/proxy/observed
    source: Optional[str] = None, # provider page URL, feed name, "router"
    ts: Optional[str] = None,
) -> dict:
    """Build one append-only history entry. The ONLY way entries are made."""
    Tier(provenance_tier)  # validate it's a real tier; raises on garbage
    return {
        "ts": ts or _now(),
        "provider": provider,
        "model_id": model_id,
        "tier": tier,
        "field": field,
        "old": round(old, 8) if old is not None else None,
        "new": round(float(new), 8),
        "provenance_tier": str(provenance_tier),
        "source": source,
    }


def _sanitize(entry: dict) -> dict:
    """Drop anything not in the schema — defense in depth against content/key leaks."""
    return {k: v for k, v in entry.items() if k in _ALLOWED}


# ─── Backend interface ────────────────────────────────────────────────────────

class HistoryBackend(Protocol):
    def append(self, entry: dict) -> None: ...
    def read(self, provider: Optional[str], model_id: Optional[str], limit: int) -> list[dict]: ...
    @property
    def name(self) -> str: ...


class FileHistoryBackend:
    """Append-only JSONL. Versioned in the repo → git is the immutable audit log."""

    def __init__(self, path: Optional[str] = None):
        self.path = Path(path) if path else HISTORY_FILE

    @property
    def name(self) -> str:
        return "file"

    def append(self, entry: dict) -> None:
        rec = _sanitize(entry)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def read(self, provider: Optional[str] = None, model_id: Optional[str] = None,
             limit: int = 1000) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in reversed(self.path.read_text(encoding="utf-8").strip().splitlines()):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if provider and rec.get("provider") != provider:
                continue
            if model_id and rec.get("model_id") != model_id:
                continue
            out.append(rec)
            if len(out) >= limit:
                break
        return out  # newest first


def get_backend() -> HistoryBackend:
    """File backend by default. (Postgres/Timescale later, same interface.)"""
    return FileHistoryBackend()


# ─── Capture ──────────────────────────────────────────────────────────────────

def record_change(
    *, provider: str, model_id: str, tier: str, field: str,
    old: Optional[float], new: float, provenance_tier: str,
    source: Optional[str] = None, backend: Optional[HistoryBackend] = None,
) -> dict:
    """Append one price change to history. Best-effort; returns the stored entry."""
    entry = build_entry(provider=provider, model_id=model_id, tier=tier, field=field,
                        old=old, new=new, provenance_tier=provenance_tier, source=source)
    (backend or get_backend()).append(entry)
    return entry


def record_diff(old_table: dict, new_table: dict, *, provenance_tier: str,
                source: Optional[str] = None, backend: Optional[HistoryBackend] = None) -> list[dict]:
    """
    Diff two price tables (raw prices.json dicts) and append an entry per changed
    price field. The bridge from a verified table update to the history series.
    """
    be = backend or get_backend()
    entries = []
    op = old_table.get("providers", {})
    npv = new_table.get("providers", {})
    for pid, prov in npv.items():
        for tier, model in prov.get("models", {}).items():
            old_model = op.get(pid, {}).get("models", {}).get(tier, {})
            for field in ("input_price_per_1m", "output_price_per_1m", "cached_input_price_per_1m"):
                if field not in model:
                    continue
                new_val = float(model[field])
                old_raw = old_model.get(field)
                old_val = float(old_raw) if old_raw is not None else None
                # Record when the field is genuinely new (old_val is None) or changed.
                if old_val is None or abs(new_val - old_val) > 1e-9:
                    e = build_entry(provider=pid, model_id=model.get("model_id", ""),
                                    tier=tier, field=field, old=old_val, new=new_val,
                                    provenance_tier=provenance_tier, source=source)
                    be.append(e)
                    entries.append(e)
    return entries


# ─── Query surface (the trajectory) ───────────────────────────────────────────

def history(provider: Optional[str] = None, model_id: Optional[str] = None,
            limit: int = 1000) -> list[dict]:
    """Recent change entries, newest first, optionally filtered."""
    return get_backend().read(provider, model_id, limit)


def trajectory(provider: str, model_id: str, field: str = "input_price_per_1m",
               limit: int = 1000) -> list[dict]:
    """
    The price series for one provider/model/field, oldest→newest — the 'chart'
    behind a ticker symbol. Each point: {ts, new}.
    """
    rows = [r for r in get_backend().read(provider, model_id, limit) if r.get("field") == field]
    rows.reverse()  # oldest first for a time series
    return [{"ts": r["ts"], "price": r["new"]} for r in rows]


def summary(limit: int = 1000) -> dict:
    """Aggregate: total changes, per-provider change counts, recent movement count."""
    rows = get_backend().read(limit=limit)
    if not rows:
        return {"total_changes": 0, "message": "No price history recorded yet."}
    by_provider: dict[str, int] = defaultdict(int)
    for r in rows:
        by_provider[r.get("provider", "unknown")] += 1
    return {
        "total_changes": len(rows),
        "changes_by_provider": dict(by_provider),
        "most_recent": rows[0].get("ts"),
    }
