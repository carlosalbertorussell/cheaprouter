"""Hosted key safety (ST4): provider env-var keys never stand in for a caller's
api_keys over HTTP. They count only over stdio (local) or with the explicit
ARBITRAGE_ALLOW_ENV_KEYS=1 opt-in. Caller keys always win."""

import asyncio
import json

import pytest

import history
from client import CompletionResult

ENV_KEY = "sk-test-FAKE-operator-env"
CALLER_KEY = "sk-test-FAKE-caller"


@pytest.fixture
def env(route_env, monkeypatch):
    """route_env plus operator env keys set for every provider, opt-in off,
    and the process marked as HTTP (the default for `python server.py`)."""
    import json as _json
    from pathlib import Path
    import providers
    real = _json.loads((Path(__file__).resolve().parent.parent / "prices.json").read_text())
    for p in real["providers"].values():
        monkeypatch.setenv(p["api_key_env"], ENV_KEY)
    monkeypatch.delenv("ARBITRAGE_ALLOW_ENV_KEYS", raising=False)
    monkeypatch.setattr(providers, "_local_transport", False)
    return route_env


def _run(coro):
    return asyncio.run(coro)


def _recording_call(calls):
    async def fake(provider, model, api_key, messages, system_prompt, max_tokens):
        calls.append((provider.name, api_key))
        return CompletionResult(text="ok", input_tokens_used=10, output_tokens_used=5,
                                latency_ms=1, model_id="m", raw_response={})
    return fake


def _route(server, **kw):
    base = dict(messages=[{"role": "user", "content": "hi"}], tier="tier_fast",
                api_keys={}, health_aware=False)
    base.update(kw)
    return json.loads(_run(server.arbitrage_route_completion(server.RouteCompletionInput(**base))))


# ─── HTTP default: env keys are ignored everywhere ───────────────────────────

def test_http_route_with_env_keys_only_has_no_eligible_providers(env, monkeypatch, mock_http):
    calls = []
    monkeypatch.setattr(env.server, "call_provider", _recording_call(calls))
    http = mock_http(lambda req: pytest.fail(f"unexpected request {req.url}"))
    out = _route(env.server)
    assert out["error"].startswith("No eligible providers found.")
    assert all(e["reason"] == "no API key provided for this provider" for e in out["excluded"])
    assert len(out["excluded"]) == 8
    assert calls == [] and http.requests == []
    assert history.read_history(limit=5) == []


def test_http_streaming_route_with_env_keys_only_makes_no_request(env, monkeypatch, mock_http):
    started = []

    async def fake_stream(*a, **kw):
        started.append(1)
        raise AssertionError("must not stream")
    monkeypatch.setattr(env.server, "_run_streaming", fake_stream)
    http = mock_http(lambda req: pytest.fail(f"unexpected request {req.url}"))
    out = _route(env.server, stream=True)
    assert out["error"].startswith("No eligible providers found.")
    assert started == [] and http.requests == []


def test_http_estimate_cost_ignores_env_keys(env):
    s = env.server
    out = json.loads(_run(s.arbitrage_estimate_cost(s.EstimateCostInput(
        tier="tier_fast", input_tokens=1000, output_tokens=500))))
    assert out["error"].startswith("No eligible providers found.")


def test_http_provider_status_ignores_env_keys(env):
    s = env.server
    out = json.loads(_run(s.arbitrage_provider_status(s.ProviderStatusInput())))
    assert out["summary"]["keys_provided"] == 0
    assert not any(p["key_provided"] for p in out["providers"].values())


def test_http_get_pricing_ignores_env_keys(env):
    s = env.server
    out = _run(s.arbitrage_get_pricing(s.GetPricingInput(tier="tier_fast")))
    assert "**No api_keys supplied**" in out
    assert "✅" not in out


# ─── caller keys always win ───────────────────────────────────────────────────

@pytest.mark.parametrize("opt_in", [False, True])
def test_caller_key_used_and_wins_over_env(env, monkeypatch, opt_in):
    if opt_in:
        monkeypatch.setenv("ARBITRAGE_ALLOW_ENV_KEYS", "1")
    calls = []
    monkeypatch.setattr(env.server, "call_provider", _recording_call(calls))
    # Exclude the rest so groq answers even when env keys make them eligible.
    out = _route(env.server, api_keys={"groq": CALLER_KEY}, excluded_providers=[
        "anthropic", "openai", "gemini", "mistral", "deepseek", "qwen", "grok"])
    assert out["routing"]["provider_id"] == "groq"
    assert calls == [("Groq", CALLER_KEY)]


# ─── opt-in and stdio enable the fallback ─────────────────────────────────────

def test_opt_in_env_var_enables_fallback(env, monkeypatch):
    monkeypatch.setenv("ARBITRAGE_ALLOW_ENV_KEYS", "1")
    calls = []
    monkeypatch.setattr(env.server, "call_provider", _recording_call(calls))
    out = _route(env.server)
    assert "error" not in out
    assert calls == [(out["routing"]["provider"], ENV_KEY)]


@pytest.mark.parametrize("value", ["", "0", "no", "false"])
def test_opt_in_requires_an_explicit_true_value(env, monkeypatch, value):
    import providers
    monkeypatch.setenv("ARBITRAGE_ALLOW_ENV_KEYS", value)
    assert providers.env_key_fallback_allowed() is False
    assert providers.resolve_api_key("groq", {}) is None


def test_stdio_main_enables_fallback(env, monkeypatch):
    import providers
    runs = []
    monkeypatch.setattr(env.server.mcp, "run", lambda transport: runs.append(transport))
    env.server.main(["server.py", "--stdio"])
    assert runs == ["stdio"]
    assert providers.env_key_fallback_allowed() is True
    calls = []
    monkeypatch.setattr(env.server, "call_provider", _recording_call(calls))
    out = _route(env.server)
    assert "error" not in out
    assert calls == [(out["routing"]["provider"], ENV_KEY)]


def test_http_main_keeps_fallback_off(env, monkeypatch):
    import providers
    runs = []
    monkeypatch.setattr(env.server.mcp, "run", lambda transport: runs.append(transport))
    env.server.main(["server.py"])
    assert runs == ["streamable-http"]
    assert providers.env_key_fallback_allowed() is False
    assert providers.resolve_api_key("groq", {}) is None
