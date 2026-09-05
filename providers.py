"""
Provider registry for cheaprouter.

BYOK model: API keys are supplied per-request by the caller, not stored
server-side. resolve_api_key() checks the user-supplied dict first, then
falls back to environment variables for local development convenience.

Pricing and model metadata are loaded from prices.json via pricing_table.py,
which enforces a staleness guard (S4a). This module builds the PROVIDERS registry
from that validated table; the public surface is unchanged.

Capability tiers map semantically equivalent models across providers:
  - tier_fast:     Small/fast models, cheap, good for simple tasks
  - tier_balanced: Mid-range models, best quality/cost ratio for most tasks
  - tier_powerful: Flagship models, maximum capability, highest cost
"""

import os
from dataclasses import dataclass, field
from typing import Optional

TIER_FAST = "tier_fast"
TIER_BALANCED = "tier_balanced"
TIER_POWERFUL = "tier_powerful"

VALID_TIERS = {TIER_FAST, TIER_BALANCED, TIER_POWERFUL}

# Approximate latency in ms from Buenos Aires to each provider region.
REGION_LATENCY_MS = {
    "us": 180,
    "eu": 220,
    "cn": 390,
}


@dataclass
class ModelConfig:
    model_id: str
    input_price_per_1m: float
    output_price_per_1m: float
    context_window: int
    supports_system_prompt: bool = True


@dataclass
class ProviderConfig:
    name: str
    region: str
    base_url: str
    api_key_env: str     # env var name — local dev fallback only
    protocol: str        # "anthropic" | "openai" | "gemini"
    models: dict[str, ModelConfig] = field(default_factory=dict)
    enabled: bool = True
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def latency_ms(self) -> int:
        return REGION_LATENCY_MS.get(self.region, 250)


# ─── Key resolution (BYOK) ────────────────────────────────────────────────────

def resolve_api_key(provider_id: str, user_keys: dict[str, str]) -> Optional[str]:
    """
    Resolve an API key for a provider. Priority:
      1. user_keys dict passed per-request (BYOK — community users)
      2. Environment variable (local dev fallback only)

    Never stored; called fresh on every request. Keys are never logged.
    """
    provider = PROVIDERS.get(provider_id)
    if not provider:
        return None
    return user_keys.get(provider_id) or os.getenv(provider.api_key_env) or None


def configured_providers(user_keys: dict[str, str]) -> dict[str, ProviderConfig]:
    """Return only providers that have a resolvable key (BYOK or env fallback)."""
    return {
        pid: prov
        for pid, prov in PROVIDERS.items()
        if prov.enabled and resolve_api_key(pid, user_keys)
    }


# ─── Build the registry from the validated price table (S4a) ──────────────────
#
# Prices and model metadata now live in prices.json, loaded and staleness-checked
# by pricing_table.py. providers.py builds the same PROVIDERS registry it always
# exposed, so every downstream import (PROVIDERS, resolve_api_key,
# configured_providers, ModelConfig, ProviderConfig, tier constants,
# REGION_LATENCY_MS) is unchanged in name and signature. Only the source of the
# numbers moved — from literals here to the versioned table.

from pricing_table import raw_table


def _build_providers() -> dict[str, ProviderConfig]:
    table = raw_table()
    registry: dict[str, ProviderConfig] = {}
    for pid, p in table["providers"].items():
        models = {
            tier: ModelConfig(
                model_id=m["model_id"],
                input_price_per_1m=float(m["input_price_per_1m"]),
                output_price_per_1m=float(m["output_price_per_1m"]),
                context_window=int(m["context_window"]),
            )
            for tier, m in p["models"].items()
        }
        registry[pid] = ProviderConfig(
            name=p["name"],
            region=p["region"],
            base_url=p["base_url"],
            api_key_env=p["api_key_env"],
            protocol=p["protocol"],
            models=models,
            extra_headers=p.get("extra_headers", {}),
        )
    return registry


PROVIDERS: dict[str, ProviderConfig] = _build_providers()


def get_provider(provider_id: str) -> Optional[ProviderConfig]:
    return PROVIDERS.get(provider_id)
