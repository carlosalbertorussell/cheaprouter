"""Tests for the price-table loader and staleness guard (S4a)."""

import json
import importlib
from datetime import date, timedelta
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent


def _write_table(tmp_path, verified_at, max_age_days=45, providers=None):
    if providers is None:
        providers = {
            "groq": {
                "name": "Groq", "region": "us", "base_url": "https://api.groq.com/openai",
                "api_key_env": "GROQ_API_KEY", "protocol": "openai",
                "models": {"tier_fast": {"model_id": "x", "input_price_per_1m": 0.05,
                                         "output_price_per_1m": 0.08, "context_window": 128000},
                           "tier_balanced": {"model_id": "x", "input_price_per_1m": 0.59,
                                             "output_price_per_1m": 0.79, "context_window": 128000},
                           "tier_powerful": {"model_id": "x", "input_price_per_1m": 0.59,
                                             "output_price_per_1m": 0.79, "context_window": 128000}},
            }
        }
    f = tmp_path / "prices.json"
    f.write_text(json.dumps({
        "schema_version": 1, "verified_at": verified_at, "max_age_days": max_age_days,
        "providers": providers,
    }))
    return f


def _load(monkeypatch, path):
    monkeypatch.setenv("ARBITRAGE_PRICES_FILE", str(path))
    monkeypatch.delenv("ARBITRAGE_ALLOW_STALE_PRICES", raising=False)
    import pricing_table
    return importlib.reload(pricing_table)


# ─── loading & validation ─────────────────────────────────────────────────────

def test_loads_valid_table(tmp_path, monkeypatch):
    f = _write_table(tmp_path, "2025-06-30")
    pt = _load(monkeypatch, f)
    assert pt.VERIFIED_AT == date(2025, 6, 30)
    assert pt.MAX_AGE_DAYS == 45


def test_missing_file_raises(tmp_path, monkeypatch):
    import pricing_table
    monkeypatch.setenv("ARBITRAGE_PRICES_FILE", str(tmp_path / "nope.json"))
    with pytest.raises(pricing_table.PriceTableError):
        pricing_table._load_raw()


def test_malformed_json_raises(tmp_path, monkeypatch):
    import pricing_table
    f = tmp_path / "prices.json"
    f.write_text("{ not valid json ")
    monkeypatch.setenv("ARBITRAGE_PRICES_FILE", str(f))
    with pytest.raises(pricing_table.PriceTableError):
        pricing_table._load_raw()


def test_missing_required_key_raises(tmp_path, monkeypatch):
    import pricing_table
    f = tmp_path / "prices.json"
    f.write_text(json.dumps({"schema_version": 1, "providers": {}}))  # no verified_at/max_age
    monkeypatch.setenv("ARBITRAGE_PRICES_FILE", str(f))
    with pytest.raises(pricing_table.PriceTableError):
        pricing_table._load_raw()


def test_truncated_model_raises(tmp_path, monkeypatch):
    import pricing_table
    bad = {"groq": {"name": "Groq", "region": "us", "base_url": "u",
                    "api_key_env": "GROQ_API_KEY", "protocol": "openai",
                    "models": {"tier_fast": {"model_id": "x"}}}}  # missing prices
    f = _write_table(tmp_path, "2025-06-30", providers=bad)
    monkeypatch.setenv("ARBITRAGE_PRICES_FILE", str(f))
    with pytest.raises(pricing_table.PriceTableError):
        pricing_table._load_raw()


def test_negative_price_raises(tmp_path, monkeypatch):
    import pricing_table
    bad = {"groq": {"name": "Groq", "region": "us", "base_url": "u",
                    "api_key_env": "GROQ_API_KEY", "protocol": "openai",
                    "models": {"tier_fast": {"model_id": "x", "input_price_per_1m": -1,
                                             "output_price_per_1m": 0.08, "context_window": 1}}}}
    f = _write_table(tmp_path, "2025-06-30", providers=bad)
    monkeypatch.setenv("ARBITRAGE_PRICES_FILE", str(f))
    with pytest.raises(pricing_table.PriceTableError):
        pricing_table._load_raw()


def test_bad_date_raises(tmp_path, monkeypatch):
    import pricing_table
    f = _write_table(tmp_path, "mid-2025")
    monkeypatch.setenv("ARBITRAGE_PRICES_FILE", str(f))
    with pytest.raises(pricing_table.PriceTableError):
        pricing_table._load_raw()


@pytest.fixture(autouse=True)
def _restore_real_table(monkeypatch):
    """Reset the module to the real (stale) table after each test here."""
    yield
    monkeypatch.delenv("ARBITRAGE_PRICES_FILE", raising=False)
    monkeypatch.delenv("ARBITRAGE_ALLOW_STALE_PRICES", raising=False)
    import pricing_table
    try:
        importlib.reload(pricing_table)
    except Exception:
        pass


# ─── staleness math ───────────────────────────────────────────────────────────

def test_fresh_table_not_stale(tmp_path, monkeypatch):
    recent = (date.today() - timedelta(days=10)).isoformat()
    pt = _load(monkeypatch, _write_table(tmp_path, recent, max_age_days=45))
    assert pt.is_stale() is False
    assert pt.age_days() == 10


def test_old_table_stale(tmp_path, monkeypatch):
    old = (date.today() - timedelta(days=100)).isoformat()
    pt = _load(monkeypatch, _write_table(tmp_path, old, max_age_days=45))
    assert pt.is_stale() is True


def test_boundary_exactly_max_age_not_stale(tmp_path, monkeypatch):
    edge = (date.today() - timedelta(days=45)).isoformat()
    pt = _load(monkeypatch, _write_table(tmp_path, edge, max_age_days=45))
    assert pt.is_stale() is False   # > max, not >=


def test_status_block_shape(tmp_path, monkeypatch):
    pt = _load(monkeypatch, _write_table(tmp_path, "2025-06-30"))
    st = pt.price_table_status()
    assert set(st) >= {"verified_at", "age_days", "max_age_days", "stale", "allow_stale_override"}


# ─── the shipped table (deterministic: never depends on today's date) ─────────
#
# The previous test asserted the committed prices.json was fresh *today*, which
# turned CI red on a fixed calendar date with no code change. Freshness of the
# committed table is a data signal owned by scripts/check_prices.py (the
# non-blocking price-freshness CI job), not a code-correctness test. Here we pin
# that the shipped table loads and validates, and that staleness is computed
# correctly against a fixed "now".

def _load_shipped(monkeypatch):
    monkeypatch.delenv("ARBITRAGE_PRICES_FILE", raising=False)
    monkeypatch.delenv("ARBITRAGE_ALLOW_STALE_PRICES", raising=False)
    import pricing_table
    return importlib.reload(pricing_table)


def test_shipped_prices_json_loads_and_validates(monkeypatch):
    pt = _load_shipped(monkeypatch)
    raw = json.loads((REPO / "prices.json").read_text())
    # The loader accepted it (no PriceTableError) and exposes the same data.
    assert pt._prices_file().resolve() == (REPO / "prices.json").resolve()
    assert pt.VERIFIED_AT == date.fromisoformat(raw["verified_at"])
    assert pt.MAX_AGE_DAYS == raw["max_age_days"]
    assert set(pt.raw_table()["providers"]) == set(raw["providers"])
    # Re-running the validator directly must also succeed.
    assert pt._load_raw()["providers"].keys() == raw["providers"].keys()


def test_shipped_table_staleness_at_fixed_dates(monkeypatch):
    pt = _load_shipped(monkeypatch)
    v, max_age = pt.VERIFIED_AT, pt.MAX_AGE_DAYS
    assert pt.age_days(v) == 0
    assert pt.is_stale(v) is False
    assert pt.is_stale(v + timedelta(days=max_age)) is False      # boundary: > not >=
    assert pt.is_stale(v + timedelta(days=max_age + 1)) is True
    st = pt.price_table_status(v + timedelta(days=max_age + 1))
    assert st["stale"] is True and st["age_days"] == max_age + 1


def test_shipped_table_expires_2026_12_29(monkeypatch):
    # SC1e: all 8 providers re-read on 2026-11-14 (six in SC1e, Groq/Qwen in SC1d);
    # with max_age_days 45 the table is fresh through 2026-12-29.
    pt = _load_shipped(monkeypatch)
    assert pt.VERIFIED_AT == date(2026, 11, 14)
    assert pt.MAX_AGE_DAYS == 45
    assert pt.is_stale(date(2026, 12, 29)) is False
    assert pt.is_stale(date(2026, 12, 30)) is True


def test_staleness_uses_monkeypatched_now(tmp_path, monkeypatch):
    """With no explicit `today`, is_stale()/age_days() read the UTC clock. Pin it."""
    from datetime import datetime as _dt, timezone as _tz
    pt = _load(monkeypatch, _write_table(tmp_path, "2026-01-01", max_age_days=45))

    def _clock(y, m, d):
        class _FixedDT(_dt):
            @classmethod
            def now(cls, tz=None):
                return _dt(y, m, d, 12, 0, tzinfo=tz or _tz.utc)
        return _FixedDT

    monkeypatch.setattr(pt, "datetime", _clock(2026, 2, 15))   # 45 days later
    assert pt.age_days() == 45 and pt.is_stale() is False
    monkeypatch.setattr(pt, "datetime", _clock(2026, 2, 16))   # 46 days later
    assert pt.age_days() == 46 and pt.is_stale() is True
    assert pt.price_table_status()["stale"] is True
