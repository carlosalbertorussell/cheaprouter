"""Tests for provider registry and BYOK key resolution."""

import os
from providers import (
    PROVIDERS, VALID_TIERS, resolve_api_key, configured_providers, get_provider
)


def test_eight_providers():
    assert len(PROVIDERS) == 8
    expected = {"anthropic", "openai", "gemini", "groq", "mistral", "deepseek", "qwen", "grok"}
    assert set(PROVIDERS.keys()) == expected


def test_every_provider_has_all_tiers():
    for pid, prov in PROVIDERS.items():
        assert set(prov.models.keys()) == VALID_TIERS, f"{pid} missing a tier"


def test_resolve_key_prefers_user_keys():
    key = resolve_api_key("anthropic", {"anthropic": "user-key"})
    assert key == "user-key"


def test_resolve_key_ignores_env_by_default(monkeypatch):
    # Default (HTTP / hosted): operator env keys never stand in for the caller's.
    import providers
    monkeypatch.setattr(providers, "_local_transport", False)
    monkeypatch.delenv("ARBITRAGE_ALLOW_ENV_KEYS", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "env-key")
    assert resolve_api_key("groq", {}) is None


def test_resolve_key_falls_back_to_env_when_allowed(monkeypatch):
    monkeypatch.setenv("ARBITRAGE_ALLOW_ENV_KEYS", "1")
    monkeypatch.setenv("GROQ_API_KEY", "env-key")
    key = resolve_api_key("groq", {})
    assert key == "env-key"


def test_resolve_key_user_overrides_env(monkeypatch):
    monkeypatch.setenv("ARBITRAGE_ALLOW_ENV_KEYS", "1")
    monkeypatch.setenv("GROQ_API_KEY", "env-key")
    key = resolve_api_key("groq", {"groq": "user-key"})
    assert key == "user-key"


def test_resolve_key_none_when_absent(monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    assert resolve_api_key("mistral", {}) is None


def test_configured_providers_only_keyed():
    keys = {"openai": "k", "grok": "k"}
    configured = configured_providers(keys)
    assert set(configured.keys()) >= {"openai", "grok"}


def test_valid_pricing_positive():
    for prov in PROVIDERS.values():
        for model in (m for ms in prov.models.values() for m in ms):
            assert model.input_price_per_1m > 0
            assert model.output_price_per_1m > 0


def test_regions_valid():
    for prov in PROVIDERS.values():
        assert prov.region in ("us", "eu", "cn")


def test_protocols_valid():
    for prov in PROVIDERS.values():
        assert prov.protocol in ("anthropic", "openai", "gemini")


def test_get_provider():
    assert get_provider("grok").name == "xAI Grok"
    assert get_provider("nonexistent") is None


def test_groq_off_models_shut_down_2026_08_16():
    # SC1d: llama-3.1-8b-instant / llama-3.3-70b-versatile were shut down for
    # free and developer accounts (console.groq.com/docs/deprecations).
    ids = {m.model_id for ms in PROVIDERS["groq"].models.values() for m in ms}
    assert ids == {"openai/gpt-oss-20b", "openai/gpt-oss-120b"}
    fast = PROVIDERS["groq"].model_for("tier_fast")
    assert (fast.input_price_per_1m, fast.output_price_per_1m,
            fast.cached_input_price_per_1m) == (0.075, 0.3, 0.037)
    for tier in ("tier_balanced", "tier_powerful"):
        m = PROVIDERS["groq"].model_for(tier)
        assert (m.input_price_per_1m, m.output_price_per_1m,
                m.cached_input_price_per_1m) == (0.15, 0.6, 0.075)
        assert m.context_window == 131072


def test_qwen_uses_singapore_endpoint():
    # SC1d: the table encodes Singapore prices; dashscope-intl is the Singapore
    # host, dashscope.aliyuncs.com is China (Beijing)
    # (alibabacloud.com/help/en/model-studio/base-url). client.py appends
    # /v1/chat/completions, giving the documented .../compatible-mode/v1 path.
    assert PROVIDERS["qwen"].base_url == "https://dashscope-intl.aliyuncs.com/compatible-mode"


def test_openai_cache_read_rates_modelled_sc1e():
    # SC1e (2026-11-14): developers.openai.com/api/docs/pricing publishes cache-read
    # rates; only cache-read is modelled (no cache-write). Prices unchanged.
    want = {"tier_fast": ("gpt-6-luna", 0.1, 0.5, 0.01),
            "tier_balanced": ("gpt-6.1-terra", 1.0, 5.0, 0.05),
            "tier_powerful": ("gpt-6.1-sol", 2.0, 10.0, 0.1)}
    for tier, row in want.items():
        m = PROVIDERS["openai"].model_for(tier)
        assert (m.model_id, m.input_price_per_1m, m.output_price_per_1m,
                m.cached_input_price_per_1m) == row


def test_context_windows_sc1e():
    # SC1e (2026-11-14), from each provider's model pages.
    want = {
        "anthropic": {"tier_fast": 1000000, "tier_balanced": 1000000, "tier_powerful": 1000000},
        "openai": {"tier_fast": 1050000, "tier_balanced": 1050000, "tier_powerful": 1050000},
        # Mistral Small 4 page says "256k"; 256000 is the conservative reading.
        "mistral": {"tier_fast": 256000, "tier_balanced": 1000000, "tier_powerful": 1000000},
        "grok": {"tier_fast": 1000000, "tier_balanced": 1000000, "tier_powerful": 500000},
        "gemini": {"tier_fast": 1048576, "tier_balanced": 1048576, "tier_powerful": 1048576},
    }
    for pid, tiers in want.items():
        for tier, ctx in tiers.items():
            assert PROVIDERS[pid].model_for(tier).context_window == ctx, (pid, tier)
