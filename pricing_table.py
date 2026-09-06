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
    for pid, prov in data["providers"].items():
        models = prov.get("models")
        if not isinstance(models, dict) or not models:
            raise PriceTableError(f"Provider '{pid}' has no models.")
        for tier, m in models.items():
            miss = _REQUIRED_MODEL - m.keys()
            if miss:
                raise PriceTableError(f"Provider '{pid}' tier '{tier}' missing fields: {sorted(miss)}")
            for f in ("input_price_per_1m", "output_price_per_1m"):
                if not isinstance(m[f], (int, float)) or m[f] < 0:
                    raise PriceTableError(f"Provider '{pid}' tier '{tier}' has invalid {f}: {m[f]!r}")

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
