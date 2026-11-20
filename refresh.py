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


# ─── Freshness SLA: scheduled drift check (SC7) ───────────────────────────────
#
# Pure functions over a check_drift() report and a price table, so the daily job
# (scripts/drift_check.py, run on a schedule by GitHub Actions) is testable
# without the network. They classify each feed change as significant or minor,
# compute the table's expiry, fingerprint the significant set, render the PR body,
# and build a proposal that applies ONLY significant changes.
#
# Provenance holds throughout: nothing here touches verified_at/verified_by or
# marks a price verified. Only openrouter-strategy (SC2) changes are classified;
# manual providers, a drifting provider on any other strategy, and fetch-failed
# providers are listed for a human and never proposed.

import hashlib
from datetime import date, datetime, timedelta

DEFAULT_THRESHOLD_PCT = 5.0
DEFAULT_EXPIRY_WARN_DAYS = 10
FINGERPRINT_MARKER = "sc7-fingerprint"
_PROPOSABLE_STRATEGY = "openrouter"


def drift_pct(table_value: float, feed_value: float) -> Optional[float]:
    """|feed - table| / table * 100. None when the table price is 0 and the feed
    price is not (an infinite relative change)."""
    table_value, feed_value = float(table_value), float(feed_value)
    if table_value == 0:
        return None if feed_value != 0 else 0.0
    return abs(feed_value - table_value) / table_value * 100.0


def is_significant(table_value: float, feed_value: float, threshold_pct: float) -> bool:
    """Significant when the drift is >= threshold_pct (inclusive). A table price of
    0 with a non-zero feed price is always significant."""
    pct = drift_pct(table_value, feed_value)
    if pct is None:
        return True
    # Round away float noise so exactly N% counts as N% (0.10 -> 0.105 is 5%).
    return round(pct, 6) >= threshold_pct


def classify_drift(report: dict, table: dict,
                   threshold_pct: float = DEFAULT_THRESHOLD_PCT) -> dict:
    """
    Split a check_drift() report into significant and minor changes.

    Returns {"significant", "minor", "fetch_failed", "manual"}. Each change is
    {provider, tier, model_id, field, table, feed, pct}; pct is None when the
    table price is 0. Only openrouter-strategy providers are classified; a
    drifting provider on any other strategy is listed as manual.
    """
    significant, minor, fetch_failed, manual = [], [], [], []
    for p in report.get("providers", []):
        pid, status = p["provider_id"], p["status"]
        if status == "fetch_failed":
            fetch_failed.append(pid)
            continue
        if status == "manual" or (status == "drift" and p.get("strategy") != _PROPOSABLE_STRATEGY):
            manual.append(pid)
            continue
        if status != "drift":
            continue
        models = table["providers"].get(pid, {}).get("models", {})
        for ch in p.get("changes", []):
            old, new = float(ch["old"]), float(ch["new"])
            pct = drift_pct(old, new)
            item = {
                "provider": pid,
                "tier": ch["tier"],
                "model_id": models.get(ch["tier"], {}).get("model_id"),
                "field": ch["field"],
                "table": old,
                "feed": new,
                "pct": None if pct is None else round(pct, 2),
            }
            (significant if is_significant(old, new, threshold_pct) else minor).append(item)

    def key(c):
        return (c["provider"], c["tier"], c["field"])

    return {
        "significant": sorted(significant, key=key),
        "minor": sorted(minor, key=key),
        "fetch_failed": sorted(fetch_failed),
        "manual": sorted(manual),
    }


def expiry_status(table: dict, today: date,
                  expiry_warn_days: int = DEFAULT_EXPIRY_WARN_DAYS) -> dict:
    """expires_on = verified_at + max_age_days. Warns when days_to_expiry <=
    expiry_warn_days, which includes an already-expired table (negative days)."""
    verified = datetime.strptime(table["verified_at"], "%Y-%m-%d").date()
    max_age = int(table["max_age_days"])
    expires_on = verified + timedelta(days=max_age)
    days_left = (expires_on - today).days
    return {
        "verified_at": verified.isoformat(),
        "max_age_days": max_age,
        "expires_on": expires_on.isoformat(),
        "days_to_expiry": days_left,
        "expiry_warning": days_left <= expiry_warn_days,
    }


def drift_fingerprint(significant: list) -> str:
    """sha256 hex of the sorted significant set, one 'provider|tier|field|feed'
    line each. Independent of input order; changes only when a proposal does."""
    lines = sorted(f"{c['provider']}|{c['tier']}|{c['field']}|{float(c['feed'])!r}"
                   for c in significant)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def no_feed_signal(report: dict) -> bool:
    """True when no openrouter-strategy provider produced a usable result (every
    one is fetch_failed, or there are none): the check says nothing about drift."""
    ors = [p for p in report.get("providers", []) if p.get("strategy") == _PROPOSABLE_STRATEGY]
    return all(p["status"] == "fetch_failed" for p in ors)


def build_sla_report(report: dict, table: dict, today: date,
                     threshold_pct: float = DEFAULT_THRESHOLD_PCT,
                     expiry_warn_days: int = DEFAULT_EXPIRY_WARN_DAYS) -> dict:
    """The SC7 JSON document (contract shape) from a check_drift() report."""
    cls = classify_drift(report, table, threshold_pct)
    out = {"checked_at": report.get("checked_at")}
    out.update(expiry_status(table, today, expiry_warn_days))
    out.update({
        "threshold_pct": float(threshold_pct),
        "significant": cls["significant"],
        "minor": cls["minor"],
        "fetch_failed": cls["fetch_failed"],
        "manual": cls["manual"],
        "fingerprint": drift_fingerprint(cls["significant"]),
    })
    return out


def apply_significant(table: dict, significant: list) -> dict:
    """A deep copy of the table with ONLY the given (significant) changes applied.
    verified_at / verified_by are never touched and no _candidate key is added."""
    import copy
    t = copy.deepcopy(table)
    t.pop("_candidate", None)
    for c in significant:
        t["providers"][c["provider"]]["models"][c["tier"]][c["field"]] = c["feed"]
    return t


def dump_like(table: dict, original_text: str) -> str:
    """Serialise a table formatted exactly like prices.json (indent 2, key order
    kept, trailing newline as the original has it), so a diff shows only the
    changed numbers. Uses whichever ensure_ascii setting reproduces the original."""
    trailing = "\n" if original_text.endswith("\n") else ""
    original = json.loads(original_text)
    ensure_ascii = False
    if json.dumps(original, indent=2, ensure_ascii=True) + trailing == original_text:
        ensure_ascii = True
    return json.dumps(table, indent=2, ensure_ascii=ensure_ascii) + trailing


def _fmt_price(v) -> str:
    return f"{float(v):g}"


def _fmt_pct(c: dict) -> str:
    if c["pct"] is None:
        return "new (table 0)"
    sign = "+" if c["feed"] > c["table"] else "-"
    return f"{sign}{c['pct']:g}%"


def _change_rows(changes: list) -> list:
    if not changes:
        return ["None."]
    rows = ["| Provider | Tier | Model | Field | Table → Feed | Change |",
            "|---|---|---|---|---|---|"]
    for c in changes:
        rows.append(f"| {c['provider']} | {c['tier']} | `{c['model_id']}` | `{c['field']}` | "
                    f"{_fmt_price(c['table'])} → {_fmt_price(c['feed'])} | {_fmt_pct(c)} |")
    return rows


def render_drift_markdown(sla: dict, pricing_urls: dict) -> str:
    """The PR body (SC7 contract): significant changes, a per-change verification
    checklist with each provider's pricing URL, the proxy-provenance warning,
    minor drifts, fetch-failed and manual providers, expiry status, and the
    hidden fingerprint marker."""
    sig = sla["significant"]
    lines = [
        "## Price drift check (SC7)",
        "",
        f"Checked at `{sla['checked_at']}` against the OpenRouter feed. Threshold: "
        f"**{sla['threshold_pct']:g}%**. A change at or above it is significant and proposed "
        f"here; smaller drifts are listed as minor and never proposed.",
        "",
        "**These numbers come from the OpenRouter feed (proxy provenance), not from the "
        "providers. Check each one on the provider's own pricing page before merging, and "
        "update `verified_at` and `verified_by` in prices.json yourself. This PR leaves both "
        "unchanged and does not make any price verified.**",
        "",
        f"### Significant changes ({len(sig)})",
        "",
        *_change_rows(sig),
        "",
        "### Verification checklist",
        "",
    ]
    for c in sig:
        url = pricing_urls.get(c["provider"]) or "(no pricing URL in prices.json)"
        lines.append(f"- [ ] {c['provider']} {c['tier']} `{c['model_id']}` `{c['field']}`: "
                     f"{_fmt_price(c['table'])} → {_fmt_price(c['feed'])}, check on {url}")
    lines.append("- [ ] `verified_at` and `verified_by` updated by a human after checking")

    if sla["expiry_warning"]:
        when = (f"expires in {sla['days_to_expiry']} day(s)" if sla["days_to_expiry"] >= 0
                else f"expired {-sla['days_to_expiry']} day(s) ago")
        expiry = f"**Warning: {when}.**"
    else:
        expiry = f"OK, {sla['days_to_expiry']} day(s) left."
    lines += [
        "",
        f"### Minor drifts ({len(sla['minor'])}, below threshold, not proposed)",
        "",
        *_change_rows(sla["minor"]),
        "",
        "### Not checked against the feed",
        "",
        f"- Fetch failed (no signal, never treated as drift): {', '.join(sla['fetch_failed']) or 'none'}",
        f"- Manual (needs a manual check on the provider's page): {', '.join(sla['manual']) or 'none'}",
        "",
        "### Expiry",
        "",
        f"`verified_at` {sla['verified_at']} + {sla['max_age_days']} days: expires on "
        f"{sla['expires_on']}. {expiry}",
        "",
        f"<!-- {FINGERPRINT_MARKER}: {sla['fingerprint']} -->",
    ]
    return "\n".join(lines) + "\n"
