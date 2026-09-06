"""Tests for the price refresh / drift path (S4b)."""

import copy
import pytest
import refresh


BASE_TABLE = {
    "schema_version": 1, "verified_at": "2025-06-30", "max_age_days": 45,
    "providers": {
        "manualco": {
            "name": "ManualCo", "region": "us", "base_url": "u",
            "api_key_env": "X", "protocol": "openai",
            "refresh": {"strategy": "manual"},
            "models": {"tier_fast": {"model_id": "m", "input_price_per_1m": 1.0,
                                     "output_price_per_1m": 2.0, "context_window": 1000}},
        },
        "apico": {
            "name": "ApiCo", "region": "us", "base_url": "u",
            "api_key_env": "Y", "protocol": "openai",
            "refresh": {"strategy": "json_api", "url": "https://x/prices.json",
                        "map": {"tier_fast": {"input_price_per_1m": ["fast", "in"],
                                              "output_price_per_1m": ["fast", "out"]}}},
            "models": {"tier_fast": {"model_id": "a", "input_price_per_1m": 0.50,
                                     "output_price_per_1m": 1.00, "context_window": 1000}},
        },
    },
}


def test_manual_provider_reported_manual():
    t = copy.deepcopy(BASE_TABLE)
    # apico has no fetch → force fetch failure by removing url
    t["providers"]["apico"]["refresh"]["url"] = None
    rep = refresh.check_drift(t)
    by = {p["provider_id"]: p for p in rep["providers"]}
    assert by["manualco"]["status"] == "manual"


def test_json_api_match(monkeypatch):
    t = copy.deepcopy(BASE_TABLE)
    # Fetched prices equal the table → match
    monkeypatch.setattr(refresh, "_fetch_json",
                        lambda url: {"fast": {"in": 0.50, "out": 1.00}})
    rep = refresh.check_drift(t)
    by = {p["provider_id"]: p for p in rep["providers"]}
    assert by["apico"]["status"] == "match"
    assert rep["any_drift"] is False


def test_json_api_drift_detected(monkeypatch):
    t = copy.deepcopy(BASE_TABLE)
    # Fetched input price differs → drift
    monkeypatch.setattr(refresh, "_fetch_json",
                        lambda url: {"fast": {"in": 0.75, "out": 1.00}})
    rep = refresh.check_drift(t)
    by = {p["provider_id"]: p for p in rep["providers"]}
    assert by["apico"]["status"] == "drift"
    assert rep["any_drift"] is True
    ch = by["apico"]["changes"][0]
    assert ch["old"] == 0.50 and ch["new"] == 0.75


def test_fetch_failure_reported(monkeypatch):
    t = copy.deepcopy(BASE_TABLE)
    monkeypatch.setattr(refresh, "_fetch_json", lambda url: None)
    rep = refresh.check_drift(t)
    by = {p["provider_id"]: p for p in rep["providers"]}
    assert by["apico"]["status"] == "fetch_failed"


def test_propose_applies_only_drifts(monkeypatch):
    t = copy.deepcopy(BASE_TABLE)
    monkeypatch.setattr(refresh, "_fetch_json",
                        lambda url: {"fast": {"in": 0.75, "out": 1.00}})
    rep = refresh.check_drift(t)
    candidate = refresh.propose_updated_table(rep, t)
    # apico input updated to fetched value
    assert candidate["providers"]["apico"]["models"]["tier_fast"]["input_price_per_1m"] == 0.75
    # manualco untouched
    assert candidate["providers"]["manualco"]["models"]["tier_fast"]["input_price_per_1m"] == 1.0
    # marked as candidate, verified_at NOT advanced
    assert candidate["_candidate"] is True
    assert candidate["verified_at"] == "2025-06-30"


def test_propose_never_touches_verified_at(monkeypatch):
    t = copy.deepcopy(BASE_TABLE)
    monkeypatch.setattr(refresh, "_fetch_json",
                        lambda url: {"fast": {"in": 9.99, "out": 9.99}})
    rep = refresh.check_drift(t)
    candidate = refresh.propose_updated_table(rep, t)
    assert candidate["verified_at"] == t["verified_at"]   # unchanged


def test_extract_price_bad_path():
    assert refresh._extract_price({"a": {"b": 1}}, ["a", "x"]) is None
    assert refresh._extract_price({"a": "notnum"}, ["a"]) is None


def test_shipped_table_refresh_strategies():
    """After SC2 the shipped prices.json wires 6 providers to the OpenRouter feed
    and keeps Groq + Qwen manual (documented reasons). Assert the configuration
    without a live fetch."""
    from pricing_table import raw_table
    t = raw_table()
    strat = {pid: (p.get("refresh") or {}).get("strategy", "manual")
             for pid, p in t["providers"].items()}
    assert strat["groq"] == "manual"
    assert strat["qwen"] == "manual"
    openrouter = [pid for pid, s in strat.items() if s == "openrouter"]
    assert len(openrouter) == 6
    # every openrouter provider declares an ids map
    for pid in openrouter:
        assert t["providers"][pid]["refresh"].get("ids")


def test_format_report_runs():
    rep = refresh.check_drift()
    out = refresh.format_drift_report(rep)
    assert "Price drift check" in out


# ─── OpenRouter shared feed (SC2) ─────────────────────────────────────────────

import refresh as _refresh_mod


def _or_table(cache=False):
    """Minimal table with two openrouter-strategy providers + one manual."""
    fast = {"model_id": "a", "input_price_per_1m": 0.15, "output_price_per_1m": 0.60,
            "context_window": 1000}
    if cache:
        fast["cached_input_price_per_1m"] = 0.075
    return {
        "schema_version": 1, "verified_at": "2026-09-05", "max_age_days": 45,
        "providers": {
            "openai": {
                "name": "OpenAI", "region": "us", "base_url": "u",
                "api_key_env": "OPENAI_API_KEY", "protocol": "openai",
                "refresh": {"strategy": "openrouter",
                            "ids": {"tier_fast": "openai/gpt-4o-mini"}},
                "models": {"tier_fast": dict(fast)},
            },
            "groq": {
                "name": "Groq", "region": "us", "base_url": "u",
                "api_key_env": "GROQ_API_KEY", "protocol": "openai",
                "refresh": {"strategy": "manual"},
                "models": {"tier_fast": {"model_id": "g", "input_price_per_1m": 0.05,
                                         "output_price_per_1m": 0.08, "context_window": 1000}},
            },
        },
    }


def _or_feed(prompt="0.00000015", completion="0.00000060", cache=None):
    """An OpenRouter /models response. Prices are per-TOKEN strings."""
    pricing = {"prompt": prompt, "completion": completion}
    if cache is not None:
        pricing["input_cache_read"] = cache
    return {"data": [{"id": "openai/gpt-4o-mini", "pricing": pricing}]}


def test_openrouter_unit_conversion_match(monkeypatch):
    # 0.00000015/token * 1e6 = 0.15/1M — matches the table exactly.
    monkeypatch.setattr(_refresh_mod, "_fetch_json", lambda url: _or_feed())
    rep = _refresh_mod.check_drift(_or_table())
    by = {p["provider_id"]: p for p in rep["providers"]}
    assert by["openai"]["status"] == "match"
    assert by["groq"]["status"] == "manual"       # manual provider untouched
    assert rep["any_drift"] is False


def test_openrouter_drift_detected(monkeypatch):
    # prompt 0.0000003/token = 0.30/1M vs table 0.15 → drift
    monkeypatch.setattr(_refresh_mod, "_fetch_json",
                        lambda url: _or_feed(prompt="0.0000003"))
    rep = _refresh_mod.check_drift(_or_table())
    by = {p["provider_id"]: p for p in rep["providers"]}
    assert by["openai"]["status"] == "drift"
    ch = next(c for c in by["openai"]["changes"] if c["field"] == "input_price_per_1m")
    assert ch["old"] == 0.15 and abs(ch["new"] - 0.30) < 1e-9


def test_openrouter_cache_price_checked(monkeypatch):
    # table cache 0.075; feed says 0.0000001/token = 0.10/1M → drift on cache
    monkeypatch.setattr(_refresh_mod, "_fetch_json",
                        lambda url: _or_feed(cache="0.0000001"))
    rep = _refresh_mod.check_drift(_or_table(cache=True))
    by = {p["provider_id"]: p for p in rep["providers"]}
    assert by["openai"]["status"] == "drift"
    assert any(c["field"] == "cached_input_price_per_1m" for c in by["openai"]["changes"])


def test_openrouter_missing_id_is_fetch_failed(monkeypatch):
    # feed doesn't contain our model id → fetch_failed, never bad data
    monkeypatch.setattr(_refresh_mod, "_fetch_json",
                        lambda url: {"data": [{"id": "someone/else", "pricing": {"prompt": "0.1"}}]})
    rep = _refresh_mod.check_drift(_or_table())
    by = {p["provider_id"]: p for p in rep["providers"]}
    assert by["openai"]["status"] == "fetch_failed"


def test_openrouter_feed_fetch_failure(monkeypatch):
    monkeypatch.setattr(_refresh_mod, "_fetch_json", lambda url: None)
    rep = _refresh_mod.check_drift(_or_table())
    by = {p["provider_id"]: p for p in rep["providers"]}
    assert by["openai"]["status"] == "fetch_failed"
    assert by["groq"]["status"] == "manual"       # manual unaffected by feed outage


def test_openrouter_feed_fetched_once(monkeypatch):
    # Two openrouter providers must share ONE fetch, not one per provider.
    calls = {"n": 0}
    def fake(url):
        calls["n"] += 1
        return _or_feed()
    monkeypatch.setattr(_refresh_mod, "_fetch_json", fake)
    t = _or_table()
    # add a second openrouter provider
    t["providers"]["gemini"] = {
        "name": "Gemini", "region": "us", "base_url": "u",
        "api_key_env": "GEMINI_API_KEY", "protocol": "gemini",
        "refresh": {"strategy": "openrouter", "ids": {"tier_fast": "openai/gpt-4o-mini"}},
        "models": {"tier_fast": {"model_id": "x", "input_price_per_1m": 0.15,
                                 "output_price_per_1m": 0.60, "context_window": 1000}},
    }
    _refresh_mod.check_drift(t)
    assert calls["n"] == 1        # single shared fetch


def test_or_price_conversion_helpers():
    assert _refresh_mod._or_price({"prompt": "0.0000025"}, "prompt") == 2.5
    assert _refresh_mod._or_price({}, "prompt") is None
    assert _refresh_mod._or_price({"prompt": "bad"}, "prompt") is None


def test_index_openrouter():
    idx = _refresh_mod._index_openrouter(_or_feed())
    assert "openai/gpt-4o-mini" in idx
    assert idx["openai/gpt-4o-mini"]["prompt"] == "0.00000015"
