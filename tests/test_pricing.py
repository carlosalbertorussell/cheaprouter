"""Tests for cost calculation and comparison."""

import pytest
from providers import PROVIDERS, VALID_TIERS
from pricing import estimate_all_providers, compute_savings, CostEstimate


ALL_KEYS = {p: "test-key" for p in PROVIDERS}


def test_estimate_all_providers_sorted_cheapest_first():
    estimates = estimate_all_providers(PROVIDERS, "tier_fast", 1000, 500, ALL_KEYS)
    costs = [e.total_cost_usd for e in estimates]
    assert costs == sorted(costs), "Estimates must be sorted cheapest-first"


def test_estimate_covers_all_providers():
    estimates = estimate_all_providers(PROVIDERS, "tier_balanced", 1000, 500, ALL_KEYS)
    assert len(estimates) == len(PROVIDERS)


def test_cost_scales_with_tokens():
    small = estimate_all_providers(PROVIDERS, "tier_fast", 1000, 500, ALL_KEYS)[0]
    large = estimate_all_providers(PROVIDERS, "tier_fast", 10000, 5000, ALL_KEYS)[0]
    assert large.total_cost_usd > small.total_cost_usd


def test_input_output_costs_sum_to_total():
    e = estimate_all_providers(PROVIDERS, "tier_powerful", 3000, 1500, ALL_KEYS)[0]
    assert abs((e.input_cost_usd + e.output_cost_usd) - e.total_cost_usd) < 1e-9


def test_invalid_tier_raises():
    with pytest.raises(ValueError):
        estimate_all_providers(PROVIDERS, "tier_nonexistent", 1000, 500, ALL_KEYS)


def test_all_tiers_valid():
    for tier in VALID_TIERS:
        estimates = estimate_all_providers(PROVIDERS, tier, 1000, 500, ALL_KEYS)
        assert len(estimates) > 0


def _est(pid, name, total):
    return CostEstimate(provider_id=pid, provider_name=name, region="us", tier="tier_fast",
                        model_id="m", input_tokens=1, output_tokens=1, input_cost_usd=total,
                        output_cost_usd=0.0, total_cost_usd=total, latency_ms=1,
                        is_configured=True)


def test_compute_savings():
    s = compute_savings(_est("a", "Cheap", 0.25), _est("b", "Dear", 1.0))
    assert s == {"saved_usd": 0.75, "saved_pct": 75.0, "vs_provider": "Dear", "vs_model_id": "m",
                 "vs_cost_usd": 1.0}


def test_compute_savings_zero_baseline_and_rounding():
    assert compute_savings(_est("a", "A", 0.0), _est("b", "B", 0.0))["saved_pct"] == 0.0
    s = compute_savings(_est("a", "A", 1.0), _est("b", "B", 3.0))
    assert s["saved_usd"] == 2.0 and s["saved_pct"] == 66.67


def test_cost_estimate_exact_with_cache_split():
    from providers import ProviderConfig, ModelConfig
    prov = ProviderConfig(name="P", region="eu", base_url="https://p", api_key_env="P_KEY_UNSET",
                          protocol="openai")
    model = ModelConfig(model_id="m", input_price_per_1m=2.0, output_price_per_1m=8.0,
                        context_window=1, cached_input_price_per_1m=0.5)
    e = CostEstimate.compute("p", prov, model, "tier_fast", 1_000_000, 250_000, {},
                             cached_input_tokens=400_000)
    # fresh 600k * $2 = 1.2, cached 400k * $0.5 = 0.2 → 1.4; output 250k * $8 = 2.0
    assert (e.input_cost_usd, e.output_cost_usd, e.total_cost_usd) == (1.4, 2.0, 3.4)
    assert e.cached_input_tokens == 400_000 and e.cache_supported is True
    assert e.latency_ms == 220 and e.is_configured is False


def test_format_pricing_table_rows():
    from pricing import format_pricing_table
    a = _est("a", "Cheap", 0.25); a.region = "eu"
    b = _est("b", "Dear", 1.0); b.is_configured = False
    out = format_pricing_table([a, b]).splitlines()
    assert out[0].startswith("| # | Provider | Region | Model |")
    assert out[2] == ("| 1 | Cheap | EU | `m` | $0.250000 | $0.000000 | **$0.250000** "
                      "| ~1ms | ✅ |")
    assert out[3].startswith("| 2 | Dear | US |") and out[3].endswith("| ⚠️ no key |")
