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


def test_shipped_table_all_manual():
    """The real prices.json ships with every provider manual (honest default)."""
    rep = refresh.check_drift()   # real table
    assert rep["summary"]["manual"] == rep["summary"]["total"]
    assert rep["any_drift"] is False


def test_format_report_runs():
    rep = refresh.check_drift()
    out = refresh.format_drift_report(rep)
    assert "Price drift check" in out
