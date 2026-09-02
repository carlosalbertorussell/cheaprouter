"""Tests for the routing decision engine."""

import json
import pytest
from providers import PROVIDERS
from router import route


ALL_KEYS = {p: "test-key" for p in PROVIDERS}


def test_winner_is_cheapest_eligible():
    d = route(PROVIDERS, "tier_fast", 1000, 500, user_keys=ALL_KEYS)
    assert d.winner is not None
    # Winner cost must be <= every other eligible estimate
    eligible = [e for e in d.all_estimates if e.is_configured]
    assert d.winner.total_cost_usd == min(e.total_cost_usd for e in eligible)


def test_no_keys_means_no_winner():
    d = route(PROVIDERS, "tier_fast", 1000, 500, user_keys={})
    assert d.winner is None
    assert len(d.excluded) == len(PROVIDERS)


def test_partial_keys_limits_pool():
    keys = {"groq": "k", "deepseek": "k"}
    d = route(PROVIDERS, "tier_balanced", 5000, 1000, user_keys=keys)
    assert d.winner.provider_id in ("groq", "deepseek")


def test_latency_sensitive_excludes_cn():
    d = route(PROVIDERS, "tier_fast", 1000, 500, user_keys=ALL_KEYS, latency_sensitive=True)
    latency_excluded = [e["provider_id"] for e in d.excluded if "latency" in e["reason"]]
    assert "deepseek" in latency_excluded
    assert "qwen" in latency_excluded


def test_excluded_providers_respected():
    d = route(PROVIDERS, "tier_fast", 1000, 500, user_keys=ALL_KEYS,
              excluded_providers=["groq"])
    assert d.winner.provider_id != "groq"


def test_allowed_regions_filter():
    d = route(PROVIDERS, "tier_powerful", 10000, 2000, user_keys=ALL_KEYS,
              allowed_regions=["eu"])
    assert d.winner.region == "eu"


def test_savings_present_with_multiple_providers():
    d = route(PROVIDERS, "tier_balanced", 5000, 1000, user_keys=ALL_KEYS)
    assert d.savings is not None
    assert d.savings["saved_usd"] >= 0


def test_keys_never_leak_into_routing_output():
    secret_keys = {"anthropic": "sk-ant-SECRET123", "groq": "gsk_SECRET456"}
    d = route(PROVIDERS, "tier_fast", 1000, 500, user_keys=secret_keys)
    output = json.dumps(d.to_dict())
    assert "SECRET123" not in output
    assert "SECRET456" not in output


def test_invalid_tier_raises():
    with pytest.raises(ValueError):
        route(PROVIDERS, "bogus_tier", 1000, 500, user_keys=ALL_KEYS)
