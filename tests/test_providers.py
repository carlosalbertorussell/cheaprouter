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


def test_resolve_key_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "env-key")
    key = resolve_api_key("groq", {})
    assert key == "env-key"


def test_resolve_key_user_overrides_env(monkeypatch):
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
        for model in prov.models.values():
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
