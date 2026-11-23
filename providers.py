"""
Provider registry for cheaprouter.

BYOK model: API keys are supplied per-request by the caller, not stored
server-side. resolve_api_key() checks the user-supplied dict first. It falls
back to provider environment variables (ANTHROPIC_API_KEY, ...) only when that
is explicitly safe — see env_key_fallback_allowed(). Over HTTP (the hosted
default) the fallback is off, so an anonymous caller can never spend the
operator's keys.

Pricing and model metadata are loaded from prices.json via pricing_table.py,
which enforces a staleness guard (S4a). This module builds the PROVIDERS registry
from that validated table; the public surface is unchanged.

Capability tiers group comparable models across providers (a cost bucket,
not a claim that the models are equivalent):
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
    cached_input_price_per_1m: Optional[float] = None  # cache-hit input price (S3); None = provider has no cache discount


@dataclass
class ProviderConfig:
    name: str
    region: str
    base_url: str
    api_key_env: str     # env var name — stdio / opt-in fallback only
    protocol: str        # "anthropic" | "openai" | "gemini"
    # tier -> the models this provider offers in it, in prices.json order (SC3a).
    # A tier written as one object in prices.json is a one-item list here.
    models: dict[str, list[ModelConfig]] = field(default_factory=dict)
    enabled: bool = True
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def latency_ms(self) -> int:
        return REGION_LATENCY_MS.get(self.region, 250)

    def models_for(self, tier: str) -> list[ModelConfig]:
        """Every model this provider lists in a tier (empty when it has none)."""
        return list(self.models.get(tier) or [])

    def model_for(self, tier: str, model_id: Optional[str] = None) -> Optional[ModelConfig]:
        """One model of a tier: the one with `model_id`, or the first listed when
        model_id is None. None when the tier or the model is absent."""
        for m in self.models_for(tier):
            if model_id is None or m.model_id == model_id:
                return m
        return None


# ─── Key resolution (BYOK) ────────────────────────────────────────────────────

# The env-var key fallback rule (ST4). Provider env keys are used only when:
#   1. the server runs over stdio (a local, single-user client such as Claude
#      Desktop) — server.py calls allow_env_keys_for_local_transport() before
#      mcp.run(transport="stdio"); or
#   2. the operator sets ARBITRAGE_ALLOW_ENV_KEYS=1 (explicit opt-in, e.g. a
#      private HTTP deployment behind their own auth).
# Otherwise — including the default streamable-HTTP transport used on MCPize —
# only keys the caller passes in api_keys count.
ENV_KEYS_OPT_IN_VAR = "ARBITRAGE_ALLOW_ENV_KEYS"
_local_transport = False


def allow_env_keys_for_local_transport(enabled: bool = True) -> None:
    """Mark this process as serving a local (stdio) transport."""
    global _local_transport
    _local_transport = bool(enabled)


def is_local_transport() -> bool:
    """True once server.main() has marked this process as serving local stdio.
    Read at call time (health.min_sessions uses it too, ST6)."""
    return _local_transport


def env_key_fallback_allowed() -> bool:
    """True when provider env-var keys may stand in for missing caller keys."""
    if _local_transport:
        return True
    return os.getenv(ENV_KEYS_OPT_IN_VAR, "").strip() in ("1", "true", "TRUE", "yes")


def resolve_api_key(provider_id: str, user_keys: dict[str, str]) -> Optional[str]:
    """
    Resolve an API key for a provider. Priority:
      1. user_keys dict passed per-request (BYOK) — always wins
      2. Environment variable — only when env_key_fallback_allowed()

    Never stored; called fresh on every request. Keys are never logged.
    """
    provider = PROVIDERS.get(provider_id)
    if not provider:
        return None
    key = (user_keys or {}).get(provider_id)
    if key:
        return key
    if env_key_fallback_allowed():
        return os.getenv(provider.api_key_env) or None
    return None


def configured_providers(user_keys: dict[str, str]) -> dict[str, ProviderConfig]:
    """Return only providers that have a resolvable key (BYOK, or env when allowed)."""
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

from pricing_table import raw_table, tier_models, validate_providers


def _model_config(m: dict) -> ModelConfig:
    return ModelConfig(
        model_id=m["model_id"],
        input_price_per_1m=float(m["input_price_per_1m"]),
        output_price_per_1m=float(m["output_price_per_1m"]),
        context_window=int(m["context_window"]),
        cached_input_price_per_1m=(
            float(m["cached_input_price_per_1m"])
            if m.get("cached_input_price_per_1m") is not None else None
        ),
    )


def _build_providers(table: Optional[dict] = None) -> dict[str, ProviderConfig]:
    """Build the registry from a price table (default: the validated prices.json).
    A table passed in is validated the same way prices.json is (SC3a), so a
    synthetic table can't carry an empty or duplicated tier either."""
    table = raw_table() if table is None else table
    validate_providers(table["providers"])
    registry: dict[str, ProviderConfig] = {}
    for pid, p in table["providers"].items():
        models = {
            tier: [_model_config(m) for m in tier_models(entry)]
            for tier, entry in p["models"].items()
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
