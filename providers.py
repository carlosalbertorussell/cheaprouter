"""
Provider registry for cheaprouter.

BYOK model: API keys are supplied per-request by the caller, not stored
server-side. resolve_api_key() checks the user-supplied dict first, then
falls back to environment variables for local development convenience.

All pricing is in USD per 1M tokens. Prices should be verified periodically
against official provider pricing pages as they change frequently.

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


PROVIDERS: dict[str, ProviderConfig] = {

    "anthropic": ProviderConfig(
        name="Anthropic",
        region="us",
        base_url="https://api.anthropic.com",
        api_key_env="ANTHROPIC_API_KEY",
        protocol="anthropic",
        extra_headers={"anthropic-version": "2023-06-01"},
        models={
            TIER_FAST: ModelConfig(
                model_id="claude-haiku-4-5-20251001",
                input_price_per_1m=0.80,
                output_price_per_1m=4.00,
                context_window=200_000,
            ),
            TIER_BALANCED: ModelConfig(
                model_id="claude-sonnet-4-6",
                input_price_per_1m=3.00,
                output_price_per_1m=15.00,
                context_window=200_000,
            ),
            TIER_POWERFUL: ModelConfig(
                model_id="claude-opus-4-6",
                input_price_per_1m=15.00,
                output_price_per_1m=75.00,
                context_window=200_000,
            ),
        },
    ),

    "openai": ProviderConfig(
        name="OpenAI",
        region="us",
        base_url="https://api.openai.com",
        api_key_env="OPENAI_API_KEY",
        protocol="openai",
        models={
            TIER_FAST: ModelConfig(
                model_id="gpt-4o-mini",
                input_price_per_1m=0.15,
                output_price_per_1m=0.60,
                context_window=128_000,
            ),
            TIER_BALANCED: ModelConfig(
                model_id="gpt-4o",
                input_price_per_1m=2.50,
                output_price_per_1m=10.00,
                context_window=128_000,
            ),
            TIER_POWERFUL: ModelConfig(
                model_id="o3",
                input_price_per_1m=10.00,
                output_price_per_1m=40.00,
                context_window=200_000,
            ),
        },
    ),

    "gemini": ProviderConfig(
        name="Google Gemini",
        region="us",
        base_url="https://generativelanguage.googleapis.com",
        api_key_env="GEMINI_API_KEY",
        protocol="gemini",
        models={
            TIER_FAST: ModelConfig(
                model_id="gemini-2.0-flash",
                input_price_per_1m=0.10,
                output_price_per_1m=0.40,
                context_window=1_048_576,
            ),
            TIER_BALANCED: ModelConfig(
                model_id="gemini-1.5-pro",
                input_price_per_1m=1.25,
                output_price_per_1m=5.00,
                context_window=2_097_152,
            ),
            TIER_POWERFUL: ModelConfig(
                model_id="gemini-2.5-pro",
                input_price_per_1m=3.50,
                output_price_per_1m=14.00,
                context_window=1_048_576,
            ),
        },
    ),

    "groq": ProviderConfig(
        name="Groq",
        region="us",
        base_url="https://api.groq.com/openai",
        api_key_env="GROQ_API_KEY",
        protocol="openai",
        models={
            TIER_FAST: ModelConfig(
                model_id="llama-3.1-8b-instant",
                input_price_per_1m=0.05,
                output_price_per_1m=0.08,
                context_window=128_000,
            ),
            TIER_BALANCED: ModelConfig(
                model_id="llama-3.3-70b-versatile",
                input_price_per_1m=0.59,
                output_price_per_1m=0.79,
                context_window=128_000,
            ),
            TIER_POWERFUL: ModelConfig(
                model_id="llama-3.3-70b-versatile",
                input_price_per_1m=0.59,
                output_price_per_1m=0.79,
                context_window=128_000,
            ),
        },
    ),

    "mistral": ProviderConfig(
        name="Mistral AI",
        region="eu",
        base_url="https://api.mistral.ai",
        api_key_env="MISTRAL_API_KEY",
        protocol="openai",
        models={
            TIER_FAST: ModelConfig(
                model_id="mistral-small-latest",
                input_price_per_1m=0.10,
                output_price_per_1m=0.30,
                context_window=32_000,
            ),
            TIER_BALANCED: ModelConfig(
                model_id="mistral-medium-latest",
                input_price_per_1m=0.40,
                output_price_per_1m=1.20,
                context_window=128_000,
            ),
            TIER_POWERFUL: ModelConfig(
                model_id="mistral-large-latest",
                input_price_per_1m=2.00,
                output_price_per_1m=6.00,
                context_window=128_000,
            ),
        },
    ),

    "deepseek": ProviderConfig(
        name="DeepSeek",
        region="cn",
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        protocol="openai",
        models={
            TIER_FAST: ModelConfig(
                model_id="deepseek-chat",
                input_price_per_1m=0.27,
                output_price_per_1m=1.10,
                context_window=64_000,
            ),
            TIER_BALANCED: ModelConfig(
                model_id="deepseek-chat",
                input_price_per_1m=0.27,
                output_price_per_1m=1.10,
                context_window=64_000,
            ),
            TIER_POWERFUL: ModelConfig(
                model_id="deepseek-reasoner",
                input_price_per_1m=0.55,
                output_price_per_1m=2.19,
                context_window=64_000,
            ),
        },
    ),

    "qwen": ProviderConfig(
        name="Alibaba Qwen",
        region="cn",
        base_url="https://dashscope.aliyuncs.com/compatible-mode",
        api_key_env="DASHSCOPE_API_KEY",
        protocol="openai",
        models={
            TIER_FAST: ModelConfig(
                model_id="qwen-turbo",
                input_price_per_1m=0.05,
                output_price_per_1m=0.20,
                context_window=1_000_000,
            ),
            TIER_BALANCED: ModelConfig(
                model_id="qwen-plus",
                input_price_per_1m=0.40,
                output_price_per_1m=1.20,
                context_window=131_072,
            ),
            TIER_POWERFUL: ModelConfig(
                model_id="qwen-max",
                input_price_per_1m=1.60,
                output_price_per_1m=6.40,
                context_window=32_000,
            ),
        },
    ),
}


def get_provider(provider_id: str) -> Optional[ProviderConfig]:
    return PROVIDERS.get(provider_id)
