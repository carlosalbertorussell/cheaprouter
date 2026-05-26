"""
Routing decision engine for cheaprouter.

Selects the cheapest available provider given a tier and constraints.
Priority order (configurable via env):
  1. Price (always primary)
  2. Availability (provider must have API key configured)
  3. Latency (exclude high-latency providers when latency_sensitive=True)
  4. Region preference (optional allow/block lists)

Latency sensitivity: when True, providers with latency > LATENCY_THRESHOLD_MS
are excluded from the routing pool entirely. This mainly affects Chinese providers
(DeepSeek, Qwen) which add ~390ms RTT from Buenos Aires.
"""

import os
from dataclasses import dataclass
from typing import Optional

from providers import ProviderConfig, VALID_TIERS, resolve_api_key
from pricing import CostEstimate, estimate_all_providers, compute_savings

# Providers with latency above this threshold are excluded when latency_sensitive=True
LATENCY_THRESHOLD_MS = int(os.getenv("LATENCY_THRESHOLD_MS", "300"))

# Providers in these regions are always blocked (comma-separated env var)
# e.g. BLOCKED_REGIONS=cn  would disable DeepSeek and Qwen globally
_blocked_regions_env = os.getenv("BLOCKED_REGIONS", "")
GLOBALLY_BLOCKED_REGIONS: set[str] = {
    r.strip() for r in _blocked_regions_env.split(",") if r.strip()
}


@dataclass
class RoutingDecision:
    winner: Optional[CostEstimate]
    all_estimates: list[CostEstimate]
    excluded: list[dict]          # providers that were filtered out and why
    savings: Optional[dict]       # vs. most expensive configured alternative
    tier: str
    latency_sensitive: bool

    def to_dict(self) -> dict:
        return {
            "winner": self.winner.to_dict() if self.winner else None,
            "all_estimates": [e.to_dict() for e in self.all_estimates],
            "excluded": self.excluded,
            "savings": self.savings,
            "tier": self.tier,
            "latency_sensitive": self.latency_sensitive,
        }


def route(
    providers: dict[str, ProviderConfig],
    tier: str,
    input_tokens: int,
    output_tokens: int,
    user_keys: dict[str, str] = None,
    latency_sensitive: bool = False,
    excluded_providers: Optional[list[str]] = None,
    allowed_regions: Optional[list[str]] = None,
) -> RoutingDecision:
    """
    Select the cheapest available provider for the given tier and constraints.

    Args:
        providers:           Full provider registry (from providers.py)
        tier:                Capability tier (tier_fast | tier_balanced | tier_powerful)
        input_tokens:        Estimated input token count
        output_tokens:       Estimated output token count
        user_keys:           BYOK dict of {provider_id: api_key} from the caller
        latency_sensitive:   Exclude providers above LATENCY_THRESHOLD_MS
        excluded_providers:  Provider IDs to skip (e.g. ["openai", "groq"])
        allowed_regions:     If set, only consider providers in these regions

    Returns:
        RoutingDecision with winner, full ranking, exclusion reasons, and savings.
    """
    if user_keys is None:
        user_keys = {}
    if tier not in VALID_TIERS:
        raise ValueError(f"Invalid tier '{tier}'. Valid: {sorted(VALID_TIERS)}")

    excluded_ids = set(excluded_providers or [])
    all_estimates = estimate_all_providers(providers, tier, input_tokens, output_tokens, user_keys)

    pool: list[CostEstimate] = []
    excluded: list[dict] = []

    for est in all_estimates:
        reason = _exclusion_reason(
            est,
            excluded_ids=excluded_ids,
            latency_sensitive=latency_sensitive,
            allowed_regions=allowed_regions,
        )
        if reason:
            excluded.append({"provider_id": est.provider_id, "reason": reason})
        else:
            pool.append(est)

    if not pool:
        return RoutingDecision(
            winner=None,
            all_estimates=all_estimates,
            excluded=excluded,
            savings=None,
            tier=tier,
            latency_sensitive=latency_sensitive,
        )

    winner = pool[0]  # already sorted cheapest-first by estimate_all_providers

    # Compute savings vs. most expensive configured competitor in the pool
    savings = None
    if len(pool) > 1:
        most_expensive = max(pool, key=lambda e: e.total_cost_usd)
        if most_expensive.total_cost_usd > winner.total_cost_usd:
            savings = compute_savings(winner, most_expensive)

    return RoutingDecision(
        winner=winner,
        all_estimates=all_estimates,
        excluded=excluded,
        savings=savings,
        tier=tier,
        latency_sensitive=latency_sensitive,
    )


def _exclusion_reason(
    est: CostEstimate,
    excluded_ids: set[str],
    latency_sensitive: bool,
    allowed_regions: Optional[list[str]],
) -> Optional[str]:
    """Return an exclusion reason string, or None if the provider is eligible."""
    if not est.is_configured:
        return "no API key provided for this provider"
    if est.provider_id in excluded_ids:
        return "explicitly excluded by caller"
    if est.region in GLOBALLY_BLOCKED_REGIONS:
        return f"region '{est.region}' is globally blocked (BLOCKED_REGIONS env)"
    if allowed_regions and est.region not in allowed_regions:
        return f"region '{est.region}' not in allowed_regions={allowed_regions}"
    if latency_sensitive and est.latency_ms > LATENCY_THRESHOLD_MS:
        return (
            f"latency {est.latency_ms}ms exceeds threshold {LATENCY_THRESHOLD_MS}ms "
            f"(latency_sensitive=True)"
        )
    return None
