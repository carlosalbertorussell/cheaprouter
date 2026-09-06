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


# ─── the shipped table is current (verified 2026-09-05 by SC1) ────────────────

def test_shipped_prices_json_is_fresh():
    """After the SC1 catalog refresh the committed prices.json is current; the
    guard must NOT flag it stale. (Will re-stale after max_age_days — that's the
    guard working; re-verify prices when it does.)"""
    import importlib, pricing_table
    import os
    os.environ.pop("ARBITRAGE_PRICES_FILE", None)
    pt = importlib.reload(pricing_table)
    assert pt.is_stale() is False
    assert pt.age_days() <= pt.MAX_AGE_DAYS
