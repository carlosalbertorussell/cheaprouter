"""End-to-end key-leak guards: a caller's API key must never appear in a tool
response or in the routing-history store, even when the provider call fails in
ways whose error text embeds the request URL (Gemini sends the key as ?key=)."""

import asyncio
import json

import httpx
import pytest

GEMINI_KEY = "AIza-test-FAKE-GEMINI-KEY-123"
GROQ_KEY = "gsk_test_FAKE_GROQ_KEY_456"
MSGS = [{"role": "user", "content": "hello"}]


def _route(server, **kw):
    base = dict(messages=MSGS, tier="tier_fast", health_aware=False, session="leak-test")
    base.update(kw)
    return asyncio.run(server.arbitrage_route_completion(server.RouteCompletionInput(**base)))


def _assert_no_key(blob: str, *keys):
    for k in keys:
        assert k not in blob
        assert k[-8:] not in blob          # not even a tail fragment


def test_gemini_429_does_not_leak_key(route_env, mock_http):
    http = mock_http(lambda req: httpx.Response(429, json={"error": {"message": "quota"}}))
    out = _route(route_env.server, api_keys={"gemini": GEMINI_KEY})
    # sanity: the key really was in the failing request URL (the leak vector)
    assert GEMINI_KEY in str(http.requests[0].url)
    data = json.loads(out)
    assert data["error"] == "All attempted providers failed."
    assert data["attempts"][0]["provider_id"] == "gemini"
    assert data["attempts"][0]["outcome"] == "transient_error"
    _assert_no_key(out, GEMINI_KEY)
    hist = route_env.history.read_text()
    assert '"provider_id": "gemini"' in hist and '"success": false' in hist
    _assert_no_key(hist, GEMINI_KEY)


def test_connect_error_failover_does_not_leak_keys(route_env, mock_http):
    """Gemini connect error → fail over to Groq 429 → all fail. Neither key leaks."""
    def handler(req):
        if "generativelanguage" in req.url.host:
            raise httpx.ConnectError(f"All connection attempts failed for {req.url}", request=req)
        return httpx.Response(429, json={"error": f"rate limited for key {GROQ_KEY}"})
    mock_http(handler)
    out = _route(route_env.server, api_keys={"gemini": GEMINI_KEY, "groq": GROQ_KEY},
                 max_failover=3)
    data = json.loads(out)
    assert {a["provider_id"] for a in data["attempts"]} == {"gemini", "groq"}
    _assert_no_key(out, GEMINI_KEY, GROQ_KEY)
    _assert_no_key(route_env.history.read_text(), GEMINI_KEY, GROQ_KEY)


def test_successful_route_does_not_echo_keys(route_env, mock_http):
    mock_http(lambda req: httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": "hi"}]}}],
        "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 1}}))
    out = _route(route_env.server, api_keys={"gemini": GEMINI_KEY})
    assert json.loads(out)["response"] == "hi"
    _assert_no_key(out, GEMINI_KEY)
    _assert_no_key(route_env.history.read_text(), GEMINI_KEY)


def test_estimate_and_status_do_not_echo_keys(route_env):
    s = route_env.server
    est = asyncio.run(s.arbitrage_estimate_cost(s.EstimateCostInput(
        tier="tier_fast", input_tokens=10, output_tokens=10,
        api_keys={"gemini": GEMINI_KEY, "groq": GROQ_KEY})))
    st = asyncio.run(s.arbitrage_provider_status(s.ProviderStatusInput(
        api_keys={"gemini": GEMINI_KEY, "groq": GROQ_KEY})))
    for blob in (est, st):
        _assert_no_key(blob, GEMINI_KEY, GROQ_KEY)
    assert json.loads(st)["providers"]["gemini"]["key_provided"] is True
