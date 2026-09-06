"""
Pricing utilities for cheaprouter.

Cost calculation, comparison tables, and savings reporting.
"""

from dataclasses import dataclass
from typing import Optional
from providers import ProviderConfig, ModelConfig, VALID_TIERS, resolve_api_key


@dataclass
class CostEstimate:
    provider_id: str
    provider_name: str
    region: str
    tier: str
    model_id: str
    input_tokens: int
    output_tokens: int
    input_cost_usd: float
    output_cost_usd: float
    total_cost_usd: float
    latency_ms: int
    is_configured: bool
    cached_input_tokens: int = 0          # of input_tokens, how many were cache hits (S3)
    cache_supported: bool = False         # does this model have a cache-hit price?

    @classmethod
    def compute(
        cls,
        provider_id: str,
        provider: ProviderConfig,
        model: ModelConfig,
        tier: str,
        input_tokens: int,
        output_tokens: int,
        user_keys: dict[str, str],
        cached_input_tokens: int = 0,
    ) -> "CostEstimate":
        # Split input into fresh vs cached. Cached tokens are billed at the model's
        # cache-hit price when it has one; otherwise they fall back to the full input
        # price (a provider with no cache discount is never wrongly advantaged). (S3)
        cached = max(0, min(cached_input_tokens, input_tokens))
        fresh = input_tokens - cached
        cache_supported = model.cached_input_price_per_1m is not None
        cache_rate = model.cached_input_price_per_1m if cache_supported else model.input_price_per_1m

        input_cost = (
            (fresh / 1_000_000) * model.input_price_per_1m
            + (cached / 1_000_000) * cache_rate
        )
        output_cost = (output_tokens / 1_000_000) * model.output_price_per_1m
        return cls(
            provider_id=provider_id,
            provider_name=provider.name,
            region=provider.region,
            tier=tier,
            model_id=model.model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_cost_usd=round(input_cost, 8),
            output_cost_usd=round(output_cost, 8),
            total_cost_usd=round(input_cost + output_cost, 8),
            latency_ms=provider.latency_ms,
            is_configured=bool(resolve_api_key(provider_id, user_keys)),
            cached_input_tokens=cached,
            cache_supported=cache_supported,
        )

    def to_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "provider_name": self.provider_name,
            "region": self.region,
            "tier": self.tier,
            "model_id": self.model_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "input_cost_usd": self.input_cost_usd,
            "output_cost_usd": self.output_cost_usd,
            "total_cost_usd": self.total_cost_usd,
            "latency_ms": self.latency_ms,
            "is_configured": self.is_configured,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_supported": self.cache_supported,
        }


def estimate_all_providers(
    providers: dict,
    tier: str,
    input_tokens: int,
    output_tokens: int,
    user_keys: dict[str, str] = None,
    cached_input_tokens: int = 0,
) -> list[CostEstimate]:
    """Compute cost estimates for all providers at a given tier, sorted cheapest first."""
    if user_keys is None:
        user_keys = {}
    if tier not in VALID_TIERS:
        raise ValueError(f"Invalid tier '{tier}'. Must be one of: {sorted(VALID_TIERS)}")

    estimates = []
    for provider_id, provider in providers.items():
        model = provider.models.get(tier)
        if model is None:
            continue
        estimates.append(
            CostEstimate.compute(
                provider_id=provider_id,
                provider=provider,
                model=model,
                tier=tier,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                user_keys=user_keys,
                cached_input_tokens=cached_input_tokens,
            )
        )

    return sorted(estimates, key=lambda e: e.total_cost_usd)


def compute_savings(chosen: CostEstimate, baseline: CostEstimate) -> dict:
    """
    Compute savings of chosen provider vs. a baseline (typically the most expensive).
    Returns absolute savings in USD and percentage.
    """
    saved_usd = baseline.total_cost_usd - chosen.total_cost_usd
    pct = (saved_usd / baseline.total_cost_usd * 100) if baseline.total_cost_usd > 0 else 0.0
    return {
        "saved_usd": round(saved_usd, 8),
        "saved_pct": round(pct, 2),
        "vs_provider": baseline.provider_name,
        "vs_cost_usd": baseline.total_cost_usd,
    }


def format_pricing_table(estimates: list[CostEstimate]) -> str:
    """Render a markdown table of all provider estimates for a given tier."""
    lines = [
        "| # | Provider | Region | Model | Input cost | Output cost | Total | Latency | Configured |",
        "|---|----------|--------|-------|-----------|------------|-------|---------|------------|",
    ]
    for i, e in enumerate(estimates, 1):
        configured = "✅" if e.is_configured else "⚠️ no key"
        lines.append(
            f"| {i} | {e.provider_name} | {e.region.upper()} | `{e.model_id}` "
            f"| ${e.input_cost_usd:.6f} | ${e.output_cost_usd:.6f} "
            f"| **${e.total_cost_usd:.6f}** | ~{e.latency_ms}ms | {configured} |"
        )
    return "\n".join(lines)
