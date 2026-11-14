"""ST6: a provider reporting its prices.json model retired or unavailable fails
over to the next provider instead of ending the request; bad keys and other bad
requests still do not. Uses the real client over httpx.MockTransport."""

import asyncio
import json

import httpx
import pytest

import history
from client import is_model_unavailable_error, is_transient_error

FAKE = "sk-test-FAKE"
GROQ_KEY = "sk-test-FAKE-groq-retired-77"
GEMINI_KEY = "sk-test-FAKE-gemini-retired-88"

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "gemini-3.1-flash-lite:generateContent?key=" + GEMINI_KEY)


def _err(code, body=None, url=OPENAI_URL, text=None):
    req = httpx.Request("POST", url)
    if body is not None:
        resp = httpx.Response(code, json=body, request=req)
    elif text is not None:
        resp = httpx.Response(code, content=text.encode(), request=req)
    else:
        resp = httpx.Response(code, request=req)
    return httpx.HTTPStatusError(f"Client error '{code}'", request=req, response=resp)


# Real-world error shapes, abridged.
GROQ_DECOMMISSIONED = {"error": {
    "message": "The model `llama-3.1-8b-instant` has been decommissioned and is no "
               "longer supported.", "type": "invalid_request_error",
    "code": "model_decommissioned"}}
OPENAI_NOT_FOUND = {"error": {
    "message": "The model `gpt-x` does not exist or you do not have access to it.",
    "type": "invalid_request_error", "param": None, "code": "model_not_found"}}
ANTHROPIC_NOT_FOUND = {"type": "error", "error": {"type": "not_found_error",
                                                  "message": "model: claude-x"}}
GEMINI_NOT_FOUND = {"error": {"code": 404, "message": "models/x is not found for API "
                              "version v1beta", "status": "NOT_FOUND"}}
DEEPSEEK_400 = {"error": {"message": "Model Not Exist", "type": "invalid_request_error",
                          "param": None, "code": "invalid_request_error"}}
MISTRAL_400 = {"object": "error", "message": "Invalid model: mistral-x",
               "type": "invalid_model", "param": None, "code": "1500"}
PLAIN_400 = {"error": {"message": "max_tokens must be a positive integer",
                       "type": "invalid_request_error", "code": "invalid_value"}}


# ─── classifier ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("exc", [
    _err(404),                                              # bare 404, no body
    _err(404, OPENAI_NOT_FOUND),                            # OpenAI / Qwen / Grok
    _err(404, ANTHROPIC_NOT_FOUND, url=ANTHROPIC_URL),
    _err(404, GEMINI_NOT_FOUND, url=GEMINI_URL),
    _err(410, text="gone"),
    _err(400, GROQ_DECOMMISSIONED),                         # Groq retires as a 400
    _err(400, OPENAI_NOT_FOUND),
    _err(400, ANTHROPIC_NOT_FOUND, url=ANTHROPIC_URL),
    _err(400, GEMINI_NOT_FOUND, url=GEMINI_URL),
    _err(400, DEEPSEEK_400),                                # message only
    _err(400, MISTRAL_400),                                 # type invalid_model
    _err(400, {"error": "The model qwen-x is deprecated"}), # error as a string
    _err(400, text="model foo does not exist"),             # plain-text body
    _err(400, {"error": {"code": "MODEL_NOT_FOUND"}}),      # case-insensitive
])
def test_model_unavailable_shapes(exc):
    assert is_model_unavailable_error(exc) is True
    assert is_transient_error(exc) is False


@pytest.mark.parametrize("exc", [
    _err(401, OPENAI_NOT_FOUND),                 # bad key, even with a model message
    _err(403, GEMINI_NOT_FOUND, url=GEMINI_URL),
    _err(422, OPENAI_NOT_FOUND),
    _err(400),                                   # no body
    _err(400, PLAIN_400),                        # bad request, no model signal
    _err(400, text="not found"),                 # phrase without "model"
    _err(400, text="\xff\xfe binary"),
    _err(400, ["model_not_found"]),              # JSON, but not a dict
    _err(400, {"error": {"code": 1500, "message": None}}),
    _err(404, url="https://api.openai.com/v1/models"),   # not a completion endpoint
    _err(429, OPENAI_NOT_FOUND),                 # transient, handled elsewhere
    _err(503, OPENAI_NOT_FOUND),
    httpx.ConnectError("down"),
    ValueError("model does not exist"),
])
def test_not_model_unavailable(exc):
    assert is_model_unavailable_error(exc) is False


def test_unread_streaming_body_is_handled():
    req = httpx.Request("POST", OPENAI_URL)

    async def body():
        yield b'{"error": {"code": "model_not_found"}}'
    resp = httpx.Response(400, content=body(), request=req)    # never read
    exc = httpx.HTTPStatusError("x", request=req, response=resp)
    assert is_model_unavailable_error(exc) is False             # no crash


# ─── end to end through arbitrage_route_completion ────────────────────────────

def _ok_openai(text="ok"):
    return httpx.Response(200, json={
        "model": "served", "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1}})


def _ok_sse():
    lines = ['data: {"model":"served","choices":[{"delta":{"content":"ok"}}]}',
             'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1}}',
             'data: [DONE]']
    return httpx.Response(200, content=("\n".join(lines) + "\n").encode())


def _route(server, **kw):
    base = dict(messages=[{"role": "user", "content": "hello secret prompt"}],
                tier="tier_fast", health_aware=False, session="st6",
                api_keys={"groq": GROQ_KEY, "openai": FAKE})
    base.update(kw)
    raw = asyncio.run(server.arbitrage_route_completion(server.RouteCompletionInput(**base)))
    return raw, json.loads(raw)


def _groq_fails(response):
    """Groq (the tier_fast winner) answers with `response`, everyone else 200."""
    def handler(req):
        if req.url.host == "api.groq.com":
            return response()
        return _ok_openai()
    return handler


def test_404_fails_over_to_next_provider(route_env, mock_http):
    import providers
    http = mock_http(_groq_fails(lambda: httpx.Response(404, json=OPENAI_NOT_FOUND)))
    raw, out = _route(route_env.server)
    assert out["response"] == "ok"
    r = out["routing"]
    assert r["provider_id"] == "openai" and r["failed_over"] is True
    groq_model = providers.PROVIDERS["groq"].models["tier_fast"].model_id
    assert r["attempts"][0] == {"provider_id": "groq", "model_id": groq_model,
                                "outcome": "model_unavailable",
                                "error_class": r["attempts"][0]["error_class"]}
    assert r["attempts"][1]["outcome"] == "success"
    assert r["warnings"] == [f"groq model {groq_model} reported unavailable; "
                             f"update prices.json"]
    assert [q.url.host for q in http.requests] == ["api.groq.com", "api.openai.com"]


def test_groq_style_400_decommissioned_fails_over(route_env, mock_http):
    mock_http(_groq_fails(lambda: httpx.Response(400, json=GROQ_DECOMMISSIONED)))
    _, out = _route(route_env.server)
    assert out["routing"]["provider_id"] == "openai"
    assert [a["outcome"] for a in out["routing"]["attempts"]] == ["model_unavailable",
                                                                  "success"]


def test_anthropic_not_found_fails_over(route_env, mock_http):
    def handler(req):
        if req.url.host == "api.anthropic.com":
            return httpx.Response(404, json=ANTHROPIC_NOT_FOUND)
        return _ok_openai()
    mock_http(handler)
    _, out = _route(route_env.server, api_keys={"anthropic": FAKE, "openai": FAKE},
                    excluded_providers=[])
    attempts = out["routing"]["attempts"]
    assert {a["provider_id"] for a in attempts} == {"anthropic", "openai"}
    assert next(a for a in attempts if a["provider_id"] == "anthropic")["outcome"] \
        == "model_unavailable"
    assert out["routing"]["provider_id"] == "openai"
    assert any(w.startswith("anthropic model claude-") for w in out["routing"]["warnings"])


def test_gemini_not_found_fails_over_without_leaking_key(route_env, mock_http):
    def handler(req):
        if "generativelanguage" in req.url.host:
            return httpx.Response(400, json=GEMINI_NOT_FOUND)
        return _ok_openai()
    http = mock_http(handler)
    raw, out = _route(route_env.server, api_keys={"gemini": GEMINI_KEY, "openai": FAKE},
                      tier="tier_balanced")
    assert GEMINI_KEY in str(http.requests[0].url)       # the leak vector was live
    assert out["routing"]["provider_id"] == "openai"
    assert out["routing"]["attempts"][0]["outcome"] == "model_unavailable"
    hist = route_env.history.read_text()
    for blob in (raw, hist):
        assert GEMINI_KEY not in blob and GEMINI_KEY[-8:] not in blob
        assert "hello secret prompt" not in blob


@pytest.mark.parametrize("response", [
    lambda: httpx.Response(401, json=OPENAI_NOT_FOUND),
    lambda: httpx.Response(403, json={"error": {"message": "forbidden"}}),
    lambda: httpx.Response(400, json=PLAIN_400),
    lambda: httpx.Response(422, json={"detail": "unprocessable"}),
])
def test_bad_key_and_plain_bad_request_do_not_fail_over(route_env, mock_http, response):
    http = mock_http(_groq_fails(response))
    _, out = _route(route_env.server, max_failover=3)
    assert out["error"] == "All attempted providers failed."
    assert [a["outcome"] for a in out["attempts"]] == ["error"]
    assert "warnings" not in out["routing"]
    assert len(http.requests) == 1


def test_max_failover_zero_stops_at_retired_model(route_env, mock_http):
    http = mock_http(_groq_fails(lambda: httpx.Response(404, json=OPENAI_NOT_FOUND)))
    _, out = _route(route_env.server, max_failover=0)
    assert out["error"] == "All attempted providers failed."
    assert [a["outcome"] for a in out["attempts"]] == ["model_unavailable"]
    assert out["routing"]["warnings"][0].startswith("groq model ")   # still reported
    assert len(http.requests) == 1


def test_streaming_retired_model_before_first_token_fails_over(route_env, mock_http):
    def handler(req):
        if req.url.host == "api.groq.com":
            return httpx.Response(400, json=GROQ_DECOMMISSIONED)
        return _ok_sse()
    mock_http(handler)
    _, out = _route(route_env.server, stream=True)
    r = out["routing"]
    assert out["response"] == "ok" and r["streamed"] is True
    assert r["provider_id"] == "openai"
    assert [a["outcome"] for a in r["attempts"]] == ["model_unavailable", "success"]
    assert r["warnings"]


def test_history_logs_failed_attempt_at_zero_cost_without_key(route_env, mock_http):
    mock_http(_groq_fails(lambda: httpx.Response(404, json=OPENAI_NOT_FOUND)))
    raw, _ = _route(route_env.server)
    recs = history.read_history(limit=10)
    fail = next(r for r in recs if r["provider_id"] == "groq")
    assert fail["success"] is False and fail["transient"] is False
    assert fail["cost_usd"] == 0.0 and fail["saved_usd"] == 0.0
    ok = next(r for r in recs if r["provider_id"] == "openai")
    assert ok["success"] is True
    hist = route_env.history.read_text()
    for blob in (raw, hist):
        assert GROQ_KEY not in blob and GROQ_KEY[-8:] not in blob
        assert "hello secret prompt" not in blob


def test_retired_model_does_not_count_against_health(route_env, mock_http):
    s = route_env.server
    mock_http(_groq_fails(lambda: httpx.Response(404, json=OPENAI_NOT_FOUND)))
    for i in range(6):                                   # many sessions
        _route(s, session=f"caller-{i}")
    health = json.loads(asyncio.run(s.arbitrage_provider_health(s.ProviderHealthInput())))
    assert "groq" not in health["providers"] and health["unhealthy"] == []
