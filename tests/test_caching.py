"""Tests for prompt-caching-aware cost modeling (S3)."""

import pytest
from providers import PROVIDERS
from pricing import estimate_all_providers, CostEstimate
from router import route


ALL_KEYS = {p: "k" for p in PROVIDERS}


def _est(pid, tier, inp, out, cached):
    prov = PROVIDERS[pid]
    return CostEstimate.compute(pid, prov, prov.models[tier], tier, inp, out, ALL_KEYS, cached)


# ─── cost model ───────────────────────────────────────────────────────────────

def test_no_cached_tokens_matches_plain_input():
    e = _est("deepseek", "tier_balanced", 1000, 500, 0)
    prov = PROVIDERS["deepseek"].models["tier_balanced"]
    expected_in = (1000 / 1_000_000) * prov.input_price_per_1m
    assert abs(e.input_cost_usd - expected_in) < 1e-9


def test_cached_tokens_cheaper_for_cache_provider():
    plain = _est("deepseek", "tier_balanced", 10000, 100, 0)
    cached = _est("deepseek", "tier_balanced", 10000, 100, 9000)
    assert cached.total_cost_usd < plain.total_cost_usd
    assert cached.cache_supported is True
    assert cached.cached_input_tokens == 9000


def test_cache_provider_splits_fresh_and_cached():
    m = PROVIDERS["deepseek"].models["tier_balanced"]
    e = _est("deepseek", "tier_balanced", 10000, 0, 4000)
    fresh = (6000 / 1_000_000) * m.input_price_per_1m
    cachd = (4000 / 1_000_000) * m.cached_input_price_per_1m
    assert abs(e.input_cost_usd - (fresh + cachd)) < 1e-9


def test_non_cache_provider_bills_cached_at_full_rate():
    # Groq has no cache price → cached tokens cost the same as fresh.
    plain = _est("groq", "tier_fast", 10000, 100, 0)
    cached = _est("groq", "tier_fast", 10000, 100, 9000)
    assert cached.input_cost_usd == plain.input_cost_usd
    assert cached.cache_supported is False


def test_cached_capped_at_input_tokens():
    # Asking for more cached than input must not go negative / overcount.
    e = _est("deepseek", "tier_fast", 1000, 10, 5000)
    assert e.cached_input_tokens == 1000   # capped


def test_cache_supported_flag_reported():
    assert _est("anthropic", "tier_fast", 100, 10, 0).cache_supported is True
    assert _est("openai", "tier_fast", 100, 10, 0).cache_supported is True
    assert _est("deepseek", "tier_fast", 100, 10, 0).cache_supported is True
    assert _est("groq", "tier_fast", 100, 10, 0).cache_supported is False
    assert _est("gemini", "tier_fast", 100, 10, 0).cache_supported is False


# ─── routing impact ───────────────────────────────────────────────────────────

def test_heavy_caching_can_flip_winner():
    # tier_fast: Groq wins with no cache; DeepSeek's cache read flips it under
    # heavy caching. This is the whole point of S3.
    no_cache = route(PROVIDERS, "tier_fast", 200000, 500, user_keys=ALL_KEYS,
                     health_aware=False, cached_input_tokens=0)
    heavy = route(PROVIDERS, "tier_fast", 200000, 500, user_keys=ALL_KEYS,
                  health_aware=False, cached_input_tokens=190000)
    assert no_cache.winner.provider_id == "groq"
    assert heavy.winner.provider_id == "deepseek"


def test_caching_does_not_change_winner_when_zero():
    a = route(PROVIDERS, "tier_balanced", 5000, 1000, user_keys=ALL_KEYS, health_aware=False)
    b = route(PROVIDERS, "tier_balanced", 5000, 1000, user_keys=ALL_KEYS,
              health_aware=False, cached_input_tokens=0)
    assert a.winner.provider_id == b.winner.provider_id


def test_estimate_all_threads_cached():
    ests = estimate_all_providers(PROVIDERS, "tier_balanced", 1000, 100, ALL_KEYS, 500)
    ds = next(e for e in ests if e.provider_id == "deepseek")
    assert ds.cached_input_tokens == 500


def test_to_dict_carries_cache_fields():
    d = _est("anthropic", "tier_fast", 100, 10, 50).to_dict()
    assert d["cached_input_tokens"] == 50
    assert d["cache_supported"] is True
