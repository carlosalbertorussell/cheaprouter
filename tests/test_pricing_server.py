"""Tests for the pricing server read tools (SP1c)."""

import asyncio
import json
import pytest
import pricing_server as ps


def _run(coro):
    return asyncio.run(coro)


# ─── pricing_get ──────────────────────────────────────────────────────────────

def test_get_returns_price_and_provenance():
    out = json.loads(_run(ps.pricing_get(ps.PricingGetInput(provider="deepseek", tier="tier_fast"))))
    assert out["provider"] == "deepseek"
    assert out["model_id"] == "deepseek-v4-flash"
    assert out["price_per_1m_usd"]["input_price_per_1m"] > 0
    # provenance-first: the block is present and names the tier
    assert out["provenance"]["tier"] == "verified"
    assert "verified_at" in out["provenance"]


def test_get_includes_cache_when_present():
    out = json.loads(_run(ps.pricing_get(ps.PricingGetInput(provider="anthropic", tier="tier_fast"))))
    assert "cached_input_price_per_1m" in out["price_per_1m_usd"]  # Anthropic has cache pricing


def test_get_omits_cache_when_absent():
    out = json.loads(_run(ps.pricing_get(ps.PricingGetInput(provider="groq", tier="tier_fast"))))
    assert "cached_input_price_per_1m" not in out["price_per_1m_usd"]  # Groq has none


def test_get_unknown_provider():
    out = json.loads(_run(ps.pricing_get(ps.PricingGetInput(provider="nope", tier="tier_fast"))))
    assert "error" in out


def test_get_unknown_tier():
    out = json.loads(_run(ps.pricing_get(ps.PricingGetInput(provider="groq", tier="tier_bogus"))))
    assert "error" in out


# ─── pricing_list ─────────────────────────────────────────────────────────────

def test_list_all_providers():
    out = json.loads(_run(ps.pricing_list(ps.PricingListInput())))
    assert out["count"] == 8 * 3   # 8 providers x 3 tiers
    assert out["provenance"]["tier"] == "verified"


def test_list_tier_filter():
    out = json.loads(_run(ps.pricing_list(ps.PricingListInput(tier="tier_fast"))))
    assert out["count"] == 8
    assert all(r["tier"] == "tier_fast" for r in out["prices"])


# ─── pricing_drift ────────────────────────────────────────────────────────────

def test_drift_returns_report(monkeypatch):
    # mock the feed so no live fetch; make openai 'match'
    import refresh
    monkeypatch.setattr(refresh, "_fetch_json",
                        lambda url: {"data": [{"id": "openai/gpt-4o-mini",
                                               "pricing": {"prompt": "0.00000015", "completion": "0.0000006"}}]})
    out = json.loads(_run(ps.pricing_drift(ps.PricingDriftInput())))
    assert "drift" in out
    assert out["provenance"]["tier"] == "verified"
    assert "never auto-updates" in out["note"]


def test_drift_provider_filter(monkeypatch):
    import refresh
    monkeypatch.setattr(refresh, "_fetch_json", lambda url: {"data": []})
    out = json.loads(_run(ps.pricing_drift(ps.PricingDriftInput(provider="groq"))))
    ids = [p["provider_id"] for p in out["drift"]["providers"]]
    assert ids == ["groq"]


# ─── pricing_history ──────────────────────────────────────────────────────────

def test_history_empty_ok(monkeypatch, tmp_path):
    import price_history
    monkeypatch.setattr(price_history, "get_backend",
                        lambda: price_history.FileHistoryBackend(str(tmp_path / "h.jsonl")))
    out = json.loads(_run(ps.pricing_history(ps.PricingHistoryInput())))
    assert out["count"] == 0


def test_history_returns_changes(monkeypatch, tmp_path):
    import price_history
    be = price_history.FileHistoryBackend(str(tmp_path / "h.jsonl"))
    be.append(price_history.build_entry(provider="deepseek", model_id="deepseek-v4-flash",
              tier="tier_fast", field="input_price_per_1m", old=0.27, new=0.22,
              provenance_tier="verified", source="src"))
    monkeypatch.setattr(price_history, "get_backend", lambda: be)
    out = json.loads(_run(ps.pricing_history(ps.PricingHistoryInput(provider="deepseek"))))
    assert out["count"] == 1
    assert out["changes"][0]["new"] == 0.22


def test_history_trajectory(monkeypatch, tmp_path):
    import price_history
    be = price_history.FileHistoryBackend(str(tmp_path / "h.jsonl"))
    for old,new,ts in [(0.30,0.27,"2026-01-01T00:00:00Z"),(0.27,0.22,"2026-03-01T00:00:00Z")]:
        be.append(price_history.build_entry(provider="d", model_id="m", tier="tier_fast",
                  field="input_price_per_1m", old=old, new=new, provenance_tier="verified", ts=ts))
    monkeypatch.setattr(price_history, "get_backend", lambda: be)
    out = json.loads(_run(ps.pricing_history(ps.PricingHistoryInput(
        provider="d", model_id="m", trajectory_only=True))))
    assert [p["price"] for p in out["points"]] == [0.27, 0.22]   # oldest->newest
