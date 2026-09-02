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


def test_compute_savings():
    estimates = estimate_all_providers(PROVIDERS, "tier_balanced", 5000, 1000, ALL_KEYS)
    cheapest, priciest = estimates[0], estimates[-1]
    savings = compute_savings(cheapest, priciest)
    assert savings["saved_usd"] >= 0
    assert 0 <= savings["saved_pct"] <= 100
