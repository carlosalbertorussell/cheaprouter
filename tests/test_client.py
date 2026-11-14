"""Provider protocol adapters in client.py — request shape, response parsing,
token usage and error propagation, against httpx.MockTransport (no network)."""

import asyncio
import json

import httpx
import pytest

import client

FAKE_KEY = "sk-test-FAKE-0000"
MSGS = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"},
        {"role": "user", "content": "again"}]


def _prov(pid):
    import providers
    p = providers.PROVIDERS[pid]
    return p, p.models["tier_fast"]


def _call(pid, system_prompt=None, max_tokens=77):
    p, m = _prov(pid)
    return asyncio.run(client.call_provider(p, m, FAKE_KEY, MSGS, system_prompt, max_tokens))


# ─── Anthropic ────────────────────────────────────────────────────────────────

def test_anthropic_request_shape_and_parsing(mock_http):
    p, m = _prov("anthropic")
    http = mock_http(lambda req: httpx.Response(200, json={
        "model": "served-model",
        "content": [{"type": "thinking", "text": "skip"}, {"type": "text", "text": "Hello!"}],
        "usage": {"input_tokens": 12, "output_tokens": 3},
    }))
    r = _call("anthropic", system_prompt="be brief")
    req = http.requests[0]
    assert req.method == "POST"
    assert str(req.url) == f"{p.base_url}/v1/messages"
    assert req.headers["x-api-key"] == FAKE_KEY
    assert "authorization" not in req.headers
    for k, v in p.extra_headers.items():          # e.g. anthropic-version
        assert req.headers[k] == v
    body = json.loads(req.content)
    assert body == {"model": m.model_id, "max_tokens": 77, "messages": MSGS,
                    "system": "be brief"}          # system is top-level, not a message
    assert http.timeouts == [client.REQUEST_TIMEOUT]
    assert r.text == "Hello!"                      # first text block only
    assert (r.input_tokens_used, r.output_tokens_used) == (12, 3)
    assert r.model_id == "served-model"
    assert r.latency_ms >= 0


def test_anthropic_omits_system_when_absent_and_defaults_usage(mock_http):
    _, m = _prov("anthropic")
    http = mock_http(lambda req: httpx.Response(200, json={"content": []}))
    r = _call("anthropic")
    assert "system" not in json.loads(http.requests[0].content)
    assert r.text == "" and r.input_tokens_used == 0 and r.output_tokens_used == 0
    assert r.model_id == m.model_id                # falls back to the configured id


# ─── OpenAI-compatible ────────────────────────────────────────────────────────

@pytest.mark.parametrize("pid", ["openai", "groq", "mistral", "deepseek", "qwen", "grok"])
def test_openai_compat_request_shape_and_parsing(mock_http, pid):
    p, m = _prov(pid)
    http = mock_http(lambda req: httpx.Response(200, json={
        "model": "served", "choices": [{"message": {"content": "Hi there"}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 4},
    }))
    r = _call(pid, system_prompt="sys")
    req = http.requests[0]
    assert str(req.url) == f"{p.base_url}/v1/chat/completions"
    assert req.headers["authorization"] == f"Bearer {FAKE_KEY}"
    body = json.loads(req.content)
    assert body["model"] == m.model_id and body["max_tokens"] == 77
    # system prompt is prepended as the first message
    assert body["messages"] == [{"role": "system", "content": "sys"}] + MSGS
    assert "stream" not in body
    assert r.text == "Hi there"
    assert (r.input_tokens_used, r.output_tokens_used) == (20, 4)
    assert r.model_id == "served"


def test_openai_compat_no_system_message_when_absent(mock_http):
    http = mock_http(lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]}))
    r = _call("groq")
    assert json.loads(http.requests[0].content)["messages"] == MSGS
    assert r.input_tokens_used == 0 and r.output_tokens_used == 0


# ─── Gemini ───────────────────────────────────────────────────────────────────

def test_gemini_request_shape_and_parsing(mock_http):
    p, m = _prov("gemini")
    http = mock_http(lambda req: httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": "Bonjour"}]}}],
        "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 2},
    }))
    r = _call("gemini", system_prompt="sys")
    req = http.requests[0]
    assert req.url.path == f"/v1beta/models/{m.model_id}:generateContent"
    assert str(req.url).startswith(p.base_url)
    assert req.url.params["key"] == FAKE_KEY       # Gemini authenticates via ?key=
    assert "authorization" not in req.headers and "x-api-key" not in req.headers
    body = json.loads(req.content)
    assert body["contents"] == [
        {"role": "user", "parts": [{"text": "hi"}]},
        {"role": "model", "parts": [{"text": "yo"}]},       # assistant → model
        {"role": "user", "parts": [{"text": "again"}]},
    ]
    assert body["generationConfig"] == {"maxOutputTokens": 77}
    assert body["systemInstruction"] == {"parts": [{"text": "sys"}]}
    assert r.text == "Bonjour"
    assert (r.input_tokens_used, r.output_tokens_used) == (9, 2)
    assert r.model_id == m.model_id


def test_gemini_omits_system_instruction_when_absent(mock_http):
    http = mock_http(lambda req: httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": "x"}]}}]}))
    _call("gemini")
    assert "systemInstruction" not in json.loads(http.requests[0].content)


# ─── errors propagate (so the failover loop can classify them) ────────────────

@pytest.mark.parametrize("pid", ["anthropic", "groq", "gemini"])
@pytest.mark.parametrize("status,transient", [(429, True), (503, True), (401, False), (400, False)])
def test_http_errors_propagate_as_status_errors(mock_http, pid, status, transient):
    mock_http(lambda req: httpx.Response(status, json={"error": "nope"}))
    with pytest.raises(httpx.HTTPStatusError) as ei:
        _call(pid)
    assert ei.value.response.status_code == status
    assert client.is_transient_error(ei.value) is transient


@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ReadTimeout])
def test_transport_errors_propagate_and_are_transient(mock_http, exc):
    def handler(req):
        raise exc("boom", request=req)
    mock_http(handler)
    with pytest.raises(exc) as ei:
        _call("groq")
    assert client.is_transient_error(ei.value) is True


def test_unknown_protocol_raises():
    from providers import ProviderConfig, ModelConfig
    p = ProviderConfig(name="X", region="us", base_url="https://x", api_key_env="X_KEY",
                       protocol="carrier-pigeon")
    m = ModelConfig(model_id="m", input_price_per_1m=1, output_price_per_1m=1, context_window=1)
    with pytest.raises(ValueError, match="Unknown protocol"):
        asyncio.run(client.call_provider(p, m, FAKE_KEY, MSGS))
