"""Tests for automatic failover (S2)."""

import httpx
import pytest
from providers import PROVIDERS
from router import route
from client import is_transient_error


ALL_KEYS = {p: "k" for p in PROVIDERS}


# ─── error classification ─────────────────────────────────────────────────────

def _http_error(code):
    req = httpx.Request("POST", "https://x")
    resp = httpx.Response(code, request=req)
    return httpx.HTTPStatusError("err", request=req, response=resp)


def test_429_is_transient():
    assert is_transient_error(_http_error(429)) is True


def test_500_is_transient():
    assert is_transient_error(_http_error(500)) is True


def test_503_is_transient():
    assert is_transient_error(_http_error(503)) is True


def test_401_not_transient():
    assert is_transient_error(_http_error(401)) is False


def test_400_not_transient():
    assert is_transient_error(_http_error(400)) is False


def test_404_not_transient():
    assert is_transient_error(_http_error(404)) is False


def test_timeout_is_transient():
    assert is_transient_error(httpx.TimeoutException("slow")) is True


def test_connect_error_is_transient():
    assert is_transient_error(httpx.ConnectError("down")) is True


def test_unknown_error_not_transient():
    assert is_transient_error(ValueError("weird")) is False


# ─── ranked pool exposure (the failover sequence) ─────────────────────────────

def test_ranked_pool_present_and_ordered():
    d = route(PROVIDERS, "tier_fast", 1000, 500, user_keys=ALL_KEYS, health_aware=False)
    assert d.ranked_pool, "ranked_pool must be populated"
    # first entry is the winner
    assert d.ranked_pool[0] == d.winner.provider_id
    # cheapest-first: Groq leads tier_fast
    assert d.ranked_pool[0] == "groq"


def test_ranked_pool_only_eligible():
    # Only two keys → pool has exactly those two, in cost order.
    keys = {"groq": "k", "anthropic": "k"}
    d = route(PROVIDERS, "tier_fast", 1000, 500, user_keys=keys, health_aware=False)
    assert set(d.ranked_pool) == {"groq", "anthropic"}
    assert d.ranked_pool[0] == "groq"   # cheaper


def test_ranked_pool_in_decision_dict():
    d = route(PROVIDERS, "tier_fast", 1000, 500, user_keys=ALL_KEYS)
    assert "ranked_pool" in d.to_dict()


def test_deprioritized_provider_last_in_pool(tmp_path, monkeypatch):
    # Wire health onto an isolated store, make groq unhealthy, confirm it sinks
    # to the BACK of ranked_pool (so failover would try it last, not first).
    import history
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.setenv("ARBITRAGE_HISTORY_FILE", str(tmp_path / "h.jsonl"))
    for _ in range(5):
        history.log_decision(
            {"winner": {"provider_id": "groq", "provider_name": "Groq",
                        "region": "us", "total_cost_usd": 0.001},
             "tier": "tier_fast", "savings": {}},
            session="t", actual_cost_usd=0.001, error="RateLimitError: 429")
    d = route(PROVIDERS, "tier_fast", 1000, 500, user_keys=ALL_KEYS, health_aware=True)
    assert d.ranked_pool[-1] == "groq"       # sunk to the back
    assert d.ranked_pool[0] != "groq"        # not the first attempt


# ─── failover loop integration (mocked provider calls) ────────────────────────

import asyncio
import json
import httpx
import server
from client import CompletionResult


def _ok(text="hi"):
    return CompletionResult(text=text, input_tokens_used=10, output_tokens_used=5,
                            latency_ms=50, model_id="m", raw_response={})


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    import json as _j, importlib
    from datetime import date, timedelta
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.setenv("ARBITRAGE_HISTORY_FILE", str(tmp_path / "h.jsonl"))
    # Fresh-dated copy of the real price table so the S4a staleness guard permits routing.
    real = _j.loads(open("prices.json").read())
    real["verified_at"] = (date.today() - timedelta(days=1)).isoformat()
    pf = tmp_path / "prices.json"
    pf.write_text(_j.dumps(real))
    monkeypatch.setenv("ARBITRAGE_PRICES_FILE", str(pf))
    monkeypatch.delenv("ARBITRAGE_ALLOW_STALE_PRICES", raising=False)
    import pricing_table, providers, server as srv
    importlib.reload(pricing_table); importlib.reload(providers); importlib.reload(srv)
    yield
    monkeypatch.delenv("ARBITRAGE_PRICES_FILE", raising=False)
    importlib.reload(pricing_table); importlib.reload(providers); importlib.reload(srv)


def _run(params):
    return asyncio.run(server.arbitrage_route_completion(params))


def _params(**kw):
    base = dict(messages=[{"role": "user", "content": "hi"}],
                tier="tier_fast", api_keys={p: "k" for p in PROVIDERS},
                health_aware=False)
    base.update(kw)
    return server.RouteCompletionInput(**base)


def test_failover_succeeds_on_second_provider(isolate, monkeypatch):
    # First provider (groq) 429s; second must be tried and succeed.
    calls = []
    async def fake(provider, model, api_key, messages, system_prompt, max_tokens):
        calls.append(provider.name)
        if len(calls) == 1:
            req = httpx.Request("POST", "https://x")
            raise httpx.HTTPStatusError("rl", request=req,
                                        response=httpx.Response(429, request=req))
        return _ok("recovered")
    monkeypatch.setattr(server, "call_provider", fake)

    out = json.loads(_run(_params(max_failover=2)))
    assert out["response"] == "recovered"
    assert out["routing"]["failed_over"] is True
    assert len(calls) == 2


def test_non_transient_error_does_not_failover(isolate, monkeypatch):
    # 401 on first provider → stop, do not try others.
    calls = []
    async def fake(provider, model, api_key, messages, system_prompt, max_tokens):
        calls.append(provider.name)
        req = httpx.Request("POST", "https://x")
        raise httpx.HTTPStatusError("auth", request=req,
                                    response=httpx.Response(401, request=req))
    monkeypatch.setattr(server, "call_provider", fake)

    out = json.loads(_run(_params(max_failover=3)))
    assert "error" in out
    assert len(calls) == 1        # no failover on 401


def test_max_failover_zero_disables(isolate, monkeypatch):
    calls = []
    async def fake(provider, model, api_key, messages, system_prompt, max_tokens):
        calls.append(provider.name)
        req = httpx.Request("POST", "https://x")
        raise httpx.HTTPStatusError("rl", request=req,
                                    response=httpx.Response(429, request=req))
    monkeypatch.setattr(server, "call_provider", fake)

    out = json.loads(_run(_params(max_failover=0)))
    assert "error" in out
    assert len(calls) == 1        # only the winner attempted


def test_all_providers_fail_returns_error(isolate, monkeypatch):
    async def fake(provider, model, api_key, messages, system_prompt, max_tokens):
        req = httpx.Request("POST", "https://x")
        raise httpx.HTTPStatusError("rl", request=req,
                                    response=httpx.Response(503, request=req))
    monkeypatch.setattr(server, "call_provider", fake)

    out = json.loads(_run(_params(max_failover=2)))
    assert out["error"] == "All attempted providers failed."
    assert len(out["attempts"]) == 3   # winner + 2 failovers


def test_first_provider_success_no_failover(isolate, monkeypatch):
    calls = []
    async def fake(provider, model, api_key, messages, system_prompt, max_tokens):
        calls.append(provider.name)
        return _ok("first try")
    monkeypatch.setattr(server, "call_provider", fake)

    out = json.loads(_run(_params(max_failover=2)))
    assert out["response"] == "first try"
    assert out["routing"]["failed_over"] is False
    assert len(calls) == 1
