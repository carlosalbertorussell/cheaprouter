"""Tests for provider health tracking and health-aware routing (S8)."""

import pytest
import history
from providers import PROVIDERS
from router import route


@pytest.fixture
def local_backend(tmp_path, monkeypatch):
    f = tmp_path / "h.jsonl"
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.setenv("ARBITRAGE_HISTORY_FILE", str(f))
    return f


def _decision(pid, name):
    return {"winner": {"provider_id": pid, "provider_name": name, "region": "us",
                       "total_cost_usd": 0.001}, "tier": "tier_fast", "savings": {}}


def _log(pid, name, success):
    err = None if success else "RateLimitError: 429"
    history.log_decision(_decision(pid, name), session="t",
                         actual_cost_usd=0.001, error=err)


ALL_KEYS = {p: "k" for p in PROVIDERS}


# ─── health scoring ───────────────────────────────────────────────────────────

def test_no_history_means_no_snapshot(local_backend):
    import health
    assert health.provider_health() == {}


def test_healthy_provider_scored(local_backend):
    import health
    for _ in range(5):
        _log("groq", "Groq", True)
    snap = health.provider_health()
    assert snap["groq"]["score"] == 1.0
    assert snap["groq"]["healthy"] is True


def test_failing_provider_marked_unhealthy(local_backend):
    import health
    for _ in range(5):
        _log("groq", "Groq", False)
    snap = health.provider_health()
    assert snap["groq"]["score"] == 0.0
    assert snap["groq"]["healthy"] is False


def test_below_min_samples_stays_healthy(local_backend, monkeypatch):
    import health
    # 2 failures only, min_samples default 3 → not enough to condemn
    _log("groq", "Groq", False)
    _log("groq", "Groq", False)
    snap = health.provider_health()
    assert snap["groq"]["samples"] == 2
    assert snap["groq"]["healthy"] is True


def test_mixed_success_rate(local_backend):
    import health
    for _ in range(3):
        _log("openai", "OpenAI", True)
    for _ in range(1):
        _log("openai", "OpenAI", False)
    snap = health.provider_health()
    assert snap["openai"]["score"] == 0.75   # 3/4
    assert snap["openai"]["healthy"] is True  # above 0.5 threshold


# ─── health-aware routing ─────────────────────────────────────────────────────

def test_unhealthy_provider_deprioritized_not_excluded(local_backend):
    # Groq is the cheapest at tier_fast. Make it unhealthy; it must still be in
    # the pool but no longer the winner.
    for _ in range(5):
        _log("groq", "Groq", False)
    d = route(PROVIDERS, "tier_fast", 1000, 500, user_keys=ALL_KEYS, health_aware=True)
    assert d.winner.provider_id != "groq"          # deprioritized
    assert "groq" in d.deprioritized
    # still eligible — not in excluded
    assert all(e["provider_id"] != "groq" for e in d.excluded)


def test_health_aware_off_keeps_cheapest(local_backend):
    for _ in range(5):
        _log("groq", "Groq", False)
    d = route(PROVIDERS, "tier_fast", 1000, 500, user_keys=ALL_KEYS, health_aware=False)
    assert d.winner.provider_id == "groq"          # pure cost order
    assert d.deprioritized == []


def test_all_unhealthy_still_returns_a_winner(local_backend):
    # If every provider is unhealthy, routing must still pick one (cheapest).
    for pid in ("groq", "qwen", "gemini", "mistral", "openai", "grok", "deepseek", "anthropic"):
        for _ in range(5):
            _log(pid, pid, False)
    d = route(PROVIDERS, "tier_fast", 1000, 500, user_keys=ALL_KEYS, health_aware=True)
    assert d.winner is not None                     # never stranded
    assert d.winner.provider_id == "groq"           # cheapest among all-unhealthy


def test_healthy_cheaper_beats_healthy_pricier(local_backend):
    # Sanity: with everyone healthy, health-aware routing == cost order.
    _log("groq", "Groq", True)
    d = route(PROVIDERS, "tier_fast", 1000, 500, user_keys=ALL_KEYS, health_aware=True)
    assert d.winner.provider_id == "groq"


def test_deprioritized_in_decision_dict(local_backend):
    for _ in range(5):
        _log("groq", "Groq", False)
    d = route(PROVIDERS, "tier_fast", 1000, 500, user_keys=ALL_KEYS, health_aware=True)
    assert "deprioritized" in d.to_dict()
    assert "groq" in d.to_dict()["deprioritized"]
