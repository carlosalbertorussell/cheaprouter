"""
Price refresh path for cheaprouter (S4b).

The S4a guard makes stale prices refuse; S4b is how you clear it without editing
JSON by hand — while never inventing a number.

Reality: there is no universal pricing API. So each provider declares a fetch
strategy in prices.json under "refresh":

    "refresh": {"strategy": "json_api", "url": "...", "map": {...}}   # fetchable
    "refresh": {"strategy": "manual"}                                # human-entered

check_drift() fetches every json_api provider, compares the fetched price against
the current table, and returns a structured report: matches, drifts (with old vs
new), fetch failures, and the manual providers that always need human eyes.

Crucially this module NEVER writes prices.json and NEVER advances verified_at.
It proposes; a human disposes. propose_updated_table() returns a *candidate* dict
for review — applying it is a deliberate, separate, human step. This keeps the
handover's hard rule intact: no invented or silently-guessed prices.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

import httpx

from pricing_table import raw_table

FETCH_TIMEOUT = 15.0


@dataclass
class ProviderDrift:
    provider_id: str
    strategy: str
    status: str                       # "match" | "drift" | "fetch_failed" | "manual"
    detail: str = ""
    changes: list = field(default_factory=list)   # [{tier, field, old, new}]

    def to_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "strategy": self.strategy,
            "status": self.status,
            "detail": self.detail,
            "changes": self.changes,
        }


def _fetch_json(url: str) -> Optional[dict]:
    """GET a JSON pricing document. Returns None on any failure (best-effort)."""
    try:
        with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True) as c:
            r = c.get(url, headers={"Accept": "application/json"})
            r.raise_for_status()
            return r.json()
    except Exception:
        return None


# ─── OpenRouter shared feed (SC2) ─────────────────────────────────────────────
#
# OpenRouter (https://openrouter.ai/api/v1/models) exposes one public JSON feed
# covering hundreds of models — the programmatic price source S4b was built for.
# Two things make it different from the generic json_api path:
#   1. ONE endpoint serves ALL providers, so we fetch it once and index by model
#      id, rather than fetching per-provider.
#   2. Its prices are PER-TOKEN strings (e.g. "0.0000025"); our table is
#      per-1M-tokens, so every value is multiplied by 1_000_000.
# A model opts in with refresh: {"strategy": "openrouter", "ids": {tier: "<or-id>"}}.

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_PER_TOKEN_TO_PER_1M = 1_000_000


def _index_openrouter(doc: dict) -> dict[str, dict]:
    """Index an OpenRouter /models response by model id → pricing dict."""
    out: dict[str, dict] = {}
    for m in (doc or {}).get("data", []):
        mid = m.get("id")
        if mid:
            out[mid] = m.get("pricing", {}) or {}
    return out


def _or_price(pricing: dict, key: str) -> Optional[float]:
    """Pull one OpenRouter price (per-token string) and convert to per-1M float."""
    raw = pricing.get(key)
    if raw is None:
        return None
    try:
        return float(raw) * _PER_TOKEN_TO_PER_1M
    except (TypeError, ValueError):
        return None


# Map our table field → OpenRouter pricing key
_OR_FIELD_KEYS = {
    "input_price_per_1m": "prompt",
    "output_price_per_1m": "completion",
    "cached_input_price_per_1m": "input_cache_read",
}


def _extract_price(doc: dict, path: list) -> Optional[float]:
    """
    Walk a dotted/keyed path into a fetched JSON doc to pull one price value.
    path is a list of keys/indices, e.g. ["data", "gpt-4o", "input"].
    Returns None if the path doesn't resolve to a number.
    """
    cur = doc
    try:
        for key in path:
            cur = cur[key]
        return float(cur)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def check_drift(table: Optional[dict] = None) -> dict:
    """
    Compare fetchable provider prices against the current table.

    Returns a report dict:
      {
        "checked_at": <ISO>,
        "providers": [ProviderDrift...],
        "summary": {matches, drifts, fetch_failures, manual, total},
        "any_drift": bool,
      }

    Does not mutate anything. json_api providers are fetched; manual providers
    are reported as needing human verification, never auto-passed.
    """
    from datetime import datetime, timezone
    t = table or raw_table()
    results: list[ProviderDrift] = []

    # Fetch the OpenRouter feed once if any provider uses the openrouter strategy.
    _or_index: Optional[dict] = None
    _or_fetch_failed = False
    if any((p.get("refresh") or {}).get("strategy") == "openrouter"
           for p in t["providers"].values()):
        _or_doc = _fetch_json(OPENROUTER_MODELS_URL)
        if _or_doc is None:
            _or_fetch_failed = True
        else:
            _or_index = _index_openrouter(_or_doc)

    for pid, prov in t["providers"].items():
        refresh = prov.get("refresh") or {"strategy": "manual"}
        strategy = refresh.get("strategy", "manual")

        if strategy == "manual":
            results.append(ProviderDrift(pid, strategy, "manual",
                           detail="No programmatic source; requires human verification."))
            continue

        if strategy == "openrouter":
            if _or_fetch_failed or _or_index is None:
                results.append(ProviderDrift(pid, strategy, "fetch_failed",
                               detail=f"Could not fetch or parse {OPENROUTER_MODELS_URL}"))
                continue
            ids = refresh.get("ids", {})   # {tier: "<openrouter model id>"}
            changes = []
            missing = []
            for tier, model in prov["models"].items():
                or_id = ids.get(tier)
                if not or_id:
                    continue
                pricing = _or_index.get(or_id)
                if pricing is None:
                    missing.append(or_id)
                    continue
                for field_name in ("input_price_per_1m", "output_price_per_1m",
                                   "cached_input_price_per_1m"):
                    if field_name not in model:
                        continue  # e.g. no cache price in our table for this model
                    fetched = _or_price(pricing, _OR_FIELD_KEYS[field_name])
                    if fetched is None:
                        continue
                    current = float(model[field_name])
                    # OpenRouter values are already per-1M after conversion; compare
                    # with a small relative tolerance to avoid float-noise drifts.
                    if abs(fetched - current) > max(1e-9, 1e-6 * current):
                        changes.append({"tier": tier, "field": field_name,
                                        "old": current, "new": round(fetched, 8)})
            if missing:
                detail = f"model id(s) not found in feed: {missing}"
                if changes:
                    results.append(ProviderDrift(pid, strategy, "drift",
                                   detail=f"{len(changes)} price(s) differ; " + detail, changes=changes))
                else:
                    results.append(ProviderDrift(pid, strategy, "fetch_failed", detail=detail))
            elif changes:
                results.append(ProviderDrift(pid, strategy, "drift",
                               detail=f"{len(changes)} price(s) differ from the table.",
                               changes=changes))
            else:
                results.append(ProviderDrift(pid, strategy, "match",
                               detail="Fetched prices match the table."))
            continue

        if strategy == "json_api":
            url = refresh.get("url")
            price_map = refresh.get("map", {})  # {tier: {field: [json path]}}
            doc = _fetch_json(url) if url else None
            if doc is None:
                results.append(ProviderDrift(pid, strategy, "fetch_failed",
                               detail=f"Could not fetch or parse {url}"))
                continue

            changes = []
            for tier, model in prov["models"].items():
                tier_map = price_map.get(tier, {})
                for field_name in ("input_price_per_1m", "output_price_per_1m"):
                    path = tier_map.get(field_name)
                    if not path:
                        continue
                    fetched = _extract_price(doc, path)
                    if fetched is None:
                        continue
                    current = float(model[field_name])
                    if abs(fetched - current) > 1e-9:
                        changes.append({"tier": tier, "field": field_name,
                                        "old": current, "new": fetched})
            if changes:
                results.append(ProviderDrift(pid, strategy, "drift",
                               detail=f"{len(changes)} price(s) differ from the table.",
                               changes=changes))
            else:
                results.append(ProviderDrift(pid, strategy, "match",
                               detail="Fetched prices match the table."))
            continue

        results.append(ProviderDrift(pid, strategy, "manual",
                       detail=f"Unknown strategy {strategy!r}; treated as manual."))

    summary = {
        "matches": sum(1 for r in results if r.status == "match"),
        "drifts": sum(1 for r in results if r.status == "drift"),
        "fetch_failures": sum(1 for r in results if r.status == "fetch_failed"),
        "manual": sum(1 for r in results if r.status == "manual"),
        "total": len(results),
    }
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "providers": [r.to_dict() for r in results],
        "summary": summary,
        "any_drift": summary["drifts"] > 0,
    }


def propose_updated_table(report: dict, table: Optional[dict] = None) -> dict:
    """
    Build a CANDIDATE price table applying only the drifts found by check_drift().

    This is a proposal for human review — it is NOT written to disk and does NOT
    set verified_at (the caller must do that deliberately, only after verifying).
    Manual and fetch-failed providers are left untouched.
    """
    import copy
    t = copy.deepcopy(table or raw_table())
    for prov_report in report["providers"]:
        if prov_report["status"] != "drift":
            continue
        pid = prov_report["provider_id"]
        for ch in prov_report["changes"]:
            t["providers"][pid]["models"][ch["tier"]][ch["field"]] = ch["new"]
    # Deliberately do NOT touch verified_at here. A human sets it on confirmation.
    t["_candidate"] = True
    return t


def format_drift_report(report: dict) -> str:
    """Human-readable summary of a drift report."""
    s = report["summary"]
    lines = [
        f"Price drift check — {report['checked_at']}",
        f"  matches: {s['matches']}  drifts: {s['drifts']}  "
        f"fetch-failed: {s['fetch_failures']}  manual: {s['manual']}  (of {s['total']})",
        "",
    ]
    for p in report["providers"]:
        tag = {"match": "✓", "drift": "≠", "fetch_failed": "✗", "manual": "…"}.get(p["status"], "?")
        lines.append(f"  {tag} {p['provider_id']:10s} [{p['strategy']}] {p['detail']}")
        for ch in p.get("changes", []):
            lines.append(f"        {ch['tier']}.{ch['field']}: {ch['old']} → {ch['new']}")
    if s["drifts"]:
        lines.append("")
        lines.append("Review the proposed changes, verify against the provider's pricing page,")
        lines.append("then update prices.json and set verified_at to today. Nothing was changed.")
    return "\n".join(lines)


# ─── Observed-vs-verified validation (SP1d) ───────────────────────────────────
#
# Compares OBSERVED-tier history (what real completions actually cost, from the
# router) against the VERIFIED table. Observed matching verified is a confidence
# signal; observed materially diverging is a drift trigger — "reality disagrees,
# go verify" — stronger than a feed comparison because it's real spend.
#
# This NEVER changes any price. Observed data challenges the verified tier; it
# does not promote into it (provenance.can_promote is always False). The output is
# a report a human (or SC8) acts on.

OBSERVED_DRIFT_THRESHOLD = 0.15   # >15% divergence flags a re-verify


def observed_validation(table: Optional[dict] = None, limit: int = 1000) -> dict:
    """
    Compare recent observed unit-costs to the verified table.

    Returns { checked_at, findings: [...], summary }. Each finding classifies a
    provider/model/field as 'confirms' (observed ~ verified) or 'diverges'
    (observed materially off → re-verify signal). Providers with no observed data
    simply don't appear. Never mutates the table.
    """
    from datetime import datetime, timezone
    import price_history as ph
    t = table or raw_table()

    # index verified prices: (provider, model_id, field) -> price
    verified = {}
    for pid, prov in t["providers"].items():
        for tier, m in prov.get("models", {}).items():
            mid = m.get("model_id")
            for field in ("input_price_per_1m", "output_price_per_1m"):
                if field in m:
                    verified[(pid, mid, field)] = float(m[field])

    findings = []
    for e in ph.observed_entries(limit=limit):
        key = (e.get("provider"), e.get("model_id"), e.get("field"))
        v = verified.get(key)
        if v is None:
            continue
        obs = float(e["new"])
        if v <= 0:
            continue
        rel = abs(obs - v) / v
        findings.append({
            "provider": e["provider"], "model_id": e["model_id"], "field": e["field"],
            "verified": v, "observed": round(obs, 8), "rel_diff": round(rel, 4),
            "status": "diverges" if rel > OBSERVED_DRIFT_THRESHOLD else "confirms",
            "ts": e.get("ts"),
        })

    diverges = [f for f in findings if f["status"] == "diverges"]
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "findings": findings,
        "summary": {
            "observed_points": len(findings),
            "confirms": len(findings) - len(diverges),
            "diverges": len(diverges),
            "threshold": OBSERVED_DRIFT_THRESHOLD,
        },
        "any_divergence": bool(diverges),
        "note": "Observed cost validates or challenges the verified table; it never becomes a published price.",
    }
