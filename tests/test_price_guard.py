"""Tests that routing refuses on stale prices and the override downgrades it (S4a)."""

import asyncio
import json
import importlib
from datetime import date, timedelta

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def fresh_env(tmp_path, monkeypatch):
    """Isolate history to a temp file."""
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.setenv("ARBITRAGE_HISTORY_FILE", str(tmp_path / "h.jsonl"))


@pytest.fixture
def stale_table(tmp_path, monkeypatch):
    """
    Point ARBITRAGE_PRICES_FILE at an old-dated copy of the real table.

    The shipped prices.json is now current (SC1 refresh), so refusal behaviour
    can no longer rely on the real file being stale — we synthesise staleness.
    """
    real = json.loads(open("prices.json").read())
    real["verified_at"] = (date.today() - timedelta(days=400)).isoformat()
    f = tmp_path / "stale.json"
    f.write_text(json.dumps(real))
    monkeypatch.setenv("ARBITRAGE_PRICES_FILE", str(f))
    return f


def _reload_server():
    import pricing_table, providers, server
    importlib.reload(pricing_table)
    importlib.reload(providers)
    return importlib.reload(server)


def test_route_completion_refuses_when_stale(fresh_env, stale_table, monkeypatch):
    monkeypatch.delenv("ARBITRAGE_ALLOW_STALE_PRICES", raising=False)
    server = _reload_server()
    params = server.RouteCompletionInput(
        messages=[{"role": "user", "content": "hi"}], tier="tier_fast",
        api_keys={"groq": "k"})
    out = json.loads(_run(server.arbitrage_route_completion(params)))
    assert out["error"] == "price_table_stale"
    assert out["price_table"]["stale"] is True


def test_estimate_cost_refuses_when_stale(fresh_env, stale_table, monkeypatch):
    monkeypatch.delenv("ARBITRAGE_ALLOW_STALE_PRICES", raising=False)
    server = _reload_server()
    params = server.EstimateCostInput(
        tier="tier_fast", input_tokens=100, output_tokens=50, api_keys={"groq": "k"})
    out = json.loads(_run(server.arbitrage_estimate_cost(params)))
    assert out["error"] == "price_table_stale"


def test_override_downgrades_to_warning(fresh_env, stale_table, monkeypatch):
    monkeypatch.setenv("ARBITRAGE_ALLOW_STALE_PRICES", "1")
    server = _reload_server()
    params = server.EstimateCostInput(
        tier="tier_fast", input_tokens=100, output_tokens=50, api_keys={"groq": "k"})
    out = json.loads(_run(server.arbitrage_estimate_cost(params)))
    # not a refusal — real estimate came back, tagged stale
    assert "error" not in out or out.get("error") != "price_table_stale"
    assert out["price_table"]["stale"] is True


def test_provider_status_returns_data_when_stale(fresh_env, stale_table, monkeypatch):
    monkeypatch.delenv("ARBITRAGE_ALLOW_STALE_PRICES", raising=False)
    server = _reload_server()
    params = server.ProviderStatusInput()
    out = json.loads(_run(server.arbitrage_provider_status(params)))
    # status still returns data behind the stale flag — not a refusal
    assert "providers" in out
    assert out["price_table"]["stale"] is True


def test_fresh_table_allows_routing(fresh_env, tmp_path, monkeypatch):
    # Point at a freshly-dated copy of the real table so routing proceeds.
    import json as _j
    real = _j.loads(open("prices.json").read())
    real["verified_at"] = (date.today() - timedelta(days=5)).isoformat()
    f = tmp_path / "fresh.json"
    f.write_text(_j.dumps(real))
    monkeypatch.setenv("ARBITRAGE_PRICES_FILE", str(f))
    monkeypatch.delenv("ARBITRAGE_ALLOW_STALE_PRICES", raising=False)
    server = _reload_server()
    params = server.EstimateCostInput(
        tier="tier_fast", input_tokens=100, output_tokens=50, api_keys={"groq": "k"})
    out = json.loads(_run(server.arbitrage_estimate_cost(params)))
    assert out.get("error") != "price_table_stale"
    assert out["price_table"]["stale"] is False
    assert out["winner"]["provider_id"] == "groq"


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    """After each test, reload modules clean so the stale real table doesn't leak."""
    yield
    monkeypatch.delenv("ARBITRAGE_PRICES_FILE", raising=False)
    monkeypatch.delenv("ARBITRAGE_ALLOW_STALE_PRICES", raising=False)
    import importlib, pricing_table, providers, server
    importlib.reload(pricing_table); importlib.reload(providers); importlib.reload(server)
