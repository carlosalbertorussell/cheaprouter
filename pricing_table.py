"""
Price table loader with staleness guard (S4a).

Loads prices.json — the single source of truth for provider pricing and model
metadata — validates it, and exposes its provenance so the rest of cheaprouter
can refuse to route on prices that are too old to defend.

Design guarantees:
  - FAILS LOUDLY. A missing or malformed prices.json raises PriceTableError at
    import time. There is deliberately no silent fallback to built-in defaults —
    routing on unknown-age prices is exactly the failure this module prevents.
  - Staleness is data, not opinion: verified_at and max_age_days live in the
    file, so re-verifying prices is a one-line edit that clears the guard.

Why this exists: S1 spend analytics computes savings from this table. A stale
table turns the Pro tier's headline number into an indefensible figure. The
guard makes the gap visible and blocks routing until prices are re-verified.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

def _prices_file() -> Path:
    return Path(os.getenv("ARBITRAGE_PRICES_FILE", str(Path(__file__).parent / "prices.json")))


def _allow_stale() -> bool:
    return os.getenv("ARBITRAGE_ALLOW_STALE_PRICES", "").strip() in ("1", "true", "TRUE", "yes")


_REQUIRED_TOP = {"schema_version", "verified_at", "max_age_days", "providers"}
_REQUIRED_MODEL = {"model_id", "input_price_per_1m", "output_price_per_1m", "context_window"}


class PriceTableError(RuntimeError):
    """Raised when the price table is missing, malformed, or invalid."""


def _parse_verified_at(raw: str) -> date:
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (ValueError, TypeError) as exc:
        raise PriceTableError(f"verified_at must be an ISO date 'YYYY-MM-DD', got {raw!r}") from exc


def tier_models(entry) -> list:
    """The model objects of one `models.<tier>` value (SC3a).

    A tier is either one model object (the original form) or a non-empty list of
    model objects. Both normalise to a list here, so every consumer of the raw
    table iterates a tier the same way. Validation is validate_providers()'s job;
    this only adapts the shape (a dict becomes a one-item list)."""
    if isinstance(entry, dict):
        return [entry]
    if isinstance(entry, list):
        return list(entry)
    raise PriceTableError(f"tier value must be a model object or a list of them, got {type(entry).__name__}")


def find_model(entry, model_id: str) -> Optional[dict]:
    """The model object with this model_id in one `models.<tier>` value, or None."""
    for m in tier_models(entry):
        if m.get("model_id") == model_id:
            return m
    return None


def validate_providers(providers: dict) -> None:
    """Validate every provider's models (and its refresh ids). Raises
    PriceTableError on the first problem; never repairs anything."""
    for pid, prov in providers.items():
        models = prov.get("models")
        if not isinstance(models, dict) or not models:
            raise PriceTableError(f"Provider '{pid}' has no models.")
        for tier, entry in models.items():
            if isinstance(entry, list):
                if not entry:
                    raise PriceTableError(f"Provider '{pid}' tier '{tier}' is an empty list; "
                                          f"list at least one model or drop the tier.")
            elif not isinstance(entry, dict):
                raise PriceTableError(f"Provider '{pid}' tier '{tier}' must be a model object "
                                      f"or a non-empty list of model objects.")
            seen: set = set()
            for i, m in enumerate(tier_models(entry)):
                where = f"Provider '{pid}' tier '{tier}'" + (f" model #{i}" if isinstance(entry, list) else "")
                if not isinstance(m, dict):
                    raise PriceTableError(f"{where} is not a model object.")
                miss = _REQUIRED_MODEL - m.keys()
                if miss:
                    raise PriceTableError(f"{where} missing fields: {sorted(miss)}")
                if not isinstance(m["model_id"], str) or not m["model_id"]:
                    raise PriceTableError(f"{where} has invalid model_id: {m['model_id']!r}")
                for f in ("input_price_per_1m", "output_price_per_1m"):
                    if not isinstance(m[f], (int, float)) or m[f] < 0:
                        raise PriceTableError(f"{where} has invalid {f}: {m[f]!r}")
                if m["model_id"] in seen:
                    raise PriceTableError(f"Provider '{pid}' tier '{tier}' lists model_id "
                                          f"'{m['model_id']}' more than once.")
                seen.add(m["model_id"])
        _validate_refresh_ids(pid, prov)


def _validate_refresh_ids(pid: str, prov: dict) -> None:
    """refresh.ids maps a tier to one OpenRouter id (a string) or, per model, a
    {model_id: openrouter_id} object. A string is only allowed for a tier with a
    single model: for several it would be ambiguous."""
    ids = (prov.get("refresh") or {}).get("ids")
    if ids is None:
        return
    if not isinstance(ids, dict):
        raise PriceTableError(f"Provider '{pid}' refresh.ids must be an object.")
    for tier, ref in ids.items():
        if tier not in prov["models"]:
            raise PriceTableError(f"Provider '{pid}' refresh.ids names tier '{tier}', "
                                  f"which has no models.")
        model_ids = [m["model_id"] for m in tier_models(prov["models"][tier])]
        if isinstance(ref, str):
            if len(model_ids) > 1:
                raise PriceTableError(
                    f"Provider '{pid}' refresh.ids['{tier}'] is one id but the tier lists "
                    f"{len(model_ids)} models; map each model: {{model_id: openrouter_id}}.")
        elif isinstance(ref, dict):
            for mid, or_id in ref.items():
                if mid not in model_ids:
                    raise PriceTableError(f"Provider '{pid}' refresh.ids['{tier}'] names "
                                          f"model_id '{mid}', which tier '{tier}' does not list.")
                if not isinstance(or_id, str) or not or_id:
                    raise PriceTableError(f"Provider '{pid}' refresh.ids['{tier}']['{mid}'] "
                                          f"must be a non-empty string.")
        else:
            raise PriceTableError(f"Provider '{pid}' refresh.ids['{tier}'] must be a string "
                                  f"or a {{model_id: openrouter_id}} object.")


def openrouter_id(ids: dict, tier: str, model_id: str, n_models: int = 1) -> Optional[str]:
    """The OpenRouter id refresh.ids gives one model of a tier, or None when the
    model is not mapped (it is then not checked against the feed). A plain string
    on a tier of several models is ambiguous and raises, as validation does."""
    ref = (ids or {}).get(tier)
    if isinstance(ref, str):
        if n_models > 1:
            raise PriceTableError(f"refresh.ids['{tier}'] is one id for {n_models} models; "
                                  f"map each model: {{model_id: openrouter_id}}.")
        return ref
    if isinstance(ref, dict):
        return ref.get(model_id)
    return None


def _load_raw() -> dict:
    prices_file = _prices_file()
    if not prices_file.exists():
        raise PriceTableError(
            f"Price table not found at {prices_file}. cheaprouter will not route "
            f"without a validated price table — refusing to start rather than guess prices."
        )
    try:
        data = json.loads(prices_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PriceTableError(f"Price table at {prices_file} is not valid JSON: {exc}") from exc

    missing = _REQUIRED_TOP - data.keys()
    if missing:
        raise PriceTableError(f"Price table missing required keys: {sorted(missing)}")

    if not isinstance(data["providers"], dict) or not data["providers"]:
        raise PriceTableError("Price table 'providers' must be a non-empty object.")

    # Validate every model entry so a truncated file can't slip through.
    validate_providers(data["providers"])

    # Validate the date up front.
    _parse_verified_at(data["verified_at"])
    return data


# Loaded once at import. A bad file raises here — loud, immediate, no fallback.
_TABLE: dict = _load_raw()
VERIFIED_AT: date = _parse_verified_at(_TABLE["verified_at"])
MAX_AGE_DAYS: int = int(_TABLE["max_age_days"])


def raw_table() -> dict:
    """The full validated price table dict (for building the provider registry)."""
    return _TABLE


def age_days(today: Optional[date] = None) -> int:
    """Days since the prices were last verified."""
    ref = today or datetime.now(timezone.utc).date()
    return (ref - VERIFIED_AT).days


def is_stale(today: Optional[date] = None) -> bool:
    """True when the table is older than max_age_days."""
    return age_days(today) > MAX_AGE_DAYS


def price_table_status(today: Optional[date] = None) -> dict:
    """Provenance block attached to every tool response."""
    return {
        "verified_at": VERIFIED_AT.isoformat(),
        "age_days": age_days(today),
        "max_age_days": MAX_AGE_DAYS,
        "stale": is_stale(today),
        "allow_stale_override": _allow_stale(),
        "tier": "verified",          # the committed table is the VERIFIED tier (SP1a)
    }


def table_provenance():
    """
    Provenance of the whole committed table (SP1a). The committed prices.json is the
    VERIFIED tier by construction — a human ran the verification and stamped
    verified_at/verified_by. Returns a provenance.Provenance.
    """
    from provenance import table_provenance as _tp
    return _tp(_TABLE)


def pricing_urls() -> dict:
    return _TABLE.get("pricing_urls", {})
