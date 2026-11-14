"""ST6: routing order is deterministic and independent of the order of
providers in prices.json — cost, then latency, then provider_id."""

import random

import pytest

import providers
from pricing import estimate_all_providers, ranking_key
from pricing_table import raw_table
from router import route

TIERS = ["tier_fast", "tier_balanced", "tier_powerful"]


def _shuffled_registry(seed):
    table = raw_table()
    items = list(table["providers"].items())
    random.Random(seed).shuffle(items)
    return providers._build_providers({**table, "providers": dict(items)})


def _keys(reg):
    return {pid: "sk-test-FAKE" for pid in reg}


def test_balanced_tie_is_decided_by_latency_not_file_order():
    reg = providers._build_providers()
    est = {e.provider_id: e for e in estimate_all_providers(reg, "tier_balanced", 1000, 500)}
    # The shipped exact tie: same cost, Groq (us) answers sooner than DeepSeek (cn).
    assert est["groq"].total_cost_usd == est["deepseek"].total_cost_usd
    assert est["groq"].latency_ms < est["deepseek"].latency_ms
    for seed in range(25):
        reg = _shuffled_registry(seed)
        d = route(reg, "tier_balanced", 1000, 500, user_keys=_keys(reg), health_aware=False)
        assert d.winner.provider_id == "groq"
        assert d.ranked_pool[:2] == ["groq", "deepseek"]


@pytest.mark.parametrize("tier", TIERS)
@pytest.mark.parametrize("cached", [0, 800])
def test_full_ranking_is_independent_of_file_order(tier, cached):
    reg = providers._build_providers()
    ref = route(reg, tier, 1000, 500, user_keys=_keys(reg), health_aware=False,
                cached_input_tokens=cached).ranked_pool
    for seed in range(10):
        reg = _shuffled_registry(seed)
        d = route(reg, tier, 1000, 500, user_keys=_keys(reg), health_aware=False,
                  cached_input_tokens=cached)
        assert d.ranked_pool == ref
        assert [e.provider_id for e in d.all_estimates] == \
            [e.provider_id for e in sorted(d.all_estimates, key=ranking_key)]


def test_provider_id_is_the_final_key():
    # Same cost and same region latency: alphabetical provider_id decides.
    table = raw_table()
    for order in (["openai", "anthropic"], ["anthropic", "openai"]):
        reg = providers._build_providers(
            {**table, "providers": {p: table["providers"][p] for p in order}})
        est = estimate_all_providers(reg, "tier_fast", 1000, 500)
        assert est[0].total_cost_usd == est[1].total_cost_usd     # shipped tie
        assert est[0].latency_ms == est[1].latency_ms
        assert [e.provider_id for e in est] == ["anthropic", "openai"]


def test_cost_still_dominates_latency():
    reg = providers._build_providers()
    est = estimate_all_providers(reg, "tier_fast", 1000, 500)
    costs = [e.total_cost_usd for e in est]
    assert costs == sorted(costs)
