"""Tests for streaming completions (S5)."""

import asyncio
import json
import httpx
import pytest

import client
import server
from client import stream_supported, StreamChunk, CompletionResult
from providers import PROVIDERS


# ─── support matrix ───────────────────────────────────────────────────────────

def test_stream_supported_matrix():
    assert stream_supported("anthropic") is True
    assert stream_supported("openai") is True
    assert stream_supported("gemini") is False


def test_gemini_provider_not_stream_supported():
    assert stream_supported(PROVIDERS["gemini"].protocol) is False


# ─── SSE parsing: the REAL parsers over a mocked transport ────────────────────

FAKE_KEY = "sk-test-FAKE-stream"


def _sse(lines):
    return ("\n".join(lines) + "\n").encode()


async def _collect(agen):
    out = []
    async for c in agen:
        out.append(c)
    return out


def _stream(pid, system_prompt=None, max_tokens=64):
    import providers
    prov = providers.PROVIDERS[pid]
    model = prov.models["tier_fast"]
    return asyncio.run(_collect(client.stream_provider(
        prov, model, FAKE_KEY, [{"role": "user", "content": "hi"}], system_prompt, max_tokens)))


def test_openai_stream_yields_text(mock_http):
    import providers
    prov = providers.PROVIDERS["groq"]
    http = mock_http(lambda req: httpx.Response(200, content=_sse([
        ': keep-alive comment',
        'data: {"model":"served","choices":[{"delta":{"role":"assistant"}}]}',
        'data: {"model":"served","choices":[{"delta":{"content":"Hel"}}]}',
        'data: not-json',
        'data: {"model":"served","choices":[{"delta":{"content":"lo"}}]}',
        'data: {"model":"served","choices":[],"usage":{"prompt_tokens":11,"completion_tokens":2}}',
        'data: [DONE]',
    ])))
    chunks = _stream("groq", system_prompt="sys", max_tokens=64)
    req = http.requests[0]
    assert str(req.url) == f"{prov.base_url}/v1/chat/completions"
    assert req.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert req.headers["accept"] == "text/event-stream"
    body = json.loads(req.content)
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["max_tokens"] == 64
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert [c.text for c in chunks if c.text] == ["Hel", "lo"]
    final = chunks[-1]
    assert final.done is True
    assert (final.input_tokens_used, final.output_tokens_used) == (11, 2)
    assert final.model_id == "served"
    assert sum(1 for c in chunks if c.done) == 1


def test_anthropic_stream_parses_events(mock_http):
    import providers
    prov = providers.PROVIDERS["anthropic"]
    http = mock_http(lambda req: httpx.Response(200, content=_sse([
        'event: message_start',
        'data: {"type":"message_start","message":{"model":"served-a","usage":{"input_tokens":25}}}',
        'data: {"type":"content_block_start","index":0}',
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi"}}',
        'data: {"type":"ping"}',
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":" there"}}',
        'data: {"type":"message_delta","usage":{"output_tokens":7}}',
        'data: {"type":"message_stop"}',
        'data: {"type":"content_block_delta","delta":{"text":"AFTER-STOP"}}',
    ])))
    chunks = _stream("anthropic", system_prompt="sys")
    req = http.requests[0]
    assert str(req.url) == f"{prov.base_url}/v1/messages"
    assert req.headers["x-api-key"] == FAKE_KEY
    body = json.loads(req.content)
    assert body["stream"] is True and body["system"] == "sys"
    texts = [c.text for c in chunks if c.text]
    assert texts == ["Hi", " there"]               # nothing after message_stop
    final = chunks[-1]
    assert final.done and (final.input_tokens_used, final.output_tokens_used) == (25, 7)
    assert final.model_id == "served-a"


def test_stream_http_error_raises_before_first_chunk(mock_http):
    mock_http(lambda req: httpx.Response(429, content=b"slow down"))
    with pytest.raises(httpx.HTTPStatusError) as ei:
        _stream("groq")
    assert ei.value.response.status_code == 429


def test_stream_provider_rejects_gemini():
    with pytest.raises(ValueError, match="Streaming not supported"):
        _stream("gemini")


# ─── the REAL server._run_streaming ───────────────────────────────────────────

def _run_streaming(pid):
    import providers
    prov = providers.PROVIDERS[pid]
    return asyncio.run(server._run_streaming(
        prov, prov.models["tier_fast"], FAKE_KEY, [{"role": "user", "content": "hi"}], None, 64))


def test_run_streaming_assembles_result_and_ttft(mock_http):
    mock_http(lambda req: httpx.Response(200, content=_sse([
        'data: {"model":"served","choices":[{"delta":{"content":"a"}}]}',
        'data: {"model":"served","choices":[{"delta":{"content":"b"}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}',
        'data: [DONE]',
    ])))
    result, ttft, started = _run_streaming("groq")
    assert started is True
    assert result.text == "ab"
    assert (result.input_tokens_used, result.output_tokens_used) == (5, 2)
    assert result.model_id == "served"
    assert 0 <= ttft <= result.latency_ms


def test_run_streaming_no_text_reports_latency_as_ttft(mock_http):
    mock_http(lambda req: httpx.Response(200, content=_sse(['data: [DONE]'])))
    result, ttft, started = _run_streaming("groq")
    assert started is False and result.text == "" and ttft == result.latency_ms


def _dropping_body(first_lines):
    async def body():
        for line in first_lines:
            yield (line + "\n").encode()
        raise httpx.ReadError("peer closed connection mid-body")
    return body


def test_run_streaming_mid_body_disconnect_marks_started(mock_http):
    body = _dropping_body(['data: {"choices":[{"delta":{"content":"partial"}}]}', ''])
    mock_http(lambda req: httpx.Response(200, content=body()))
    with pytest.raises(httpx.ReadError) as ei:
        _run_streaming("groq")
    assert getattr(ei.value, "_cheaprouter_stream_started") is True


def test_run_streaming_pre_stream_error_marks_not_started(mock_http):
    def handler(req):
        raise httpx.ConnectError("refused", request=req)
    mock_http(handler)
    with pytest.raises(httpx.ConnectError) as ei:
        _run_streaming("groq")
    assert getattr(ei.value, "_cheaprouter_stream_started") is False


# ─── route_completion streaming path ──────────────────────────────────────────

@pytest.fixture
def isolate(tmp_path, monkeypatch):
    import importlib, json as _j
    from datetime import date, timedelta
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.setenv("ARBITRAGE_HISTORY_FILE", str(tmp_path / "h.jsonl"))
    real = _j.loads(open("prices.json").read())
    real["verified_at"] = (date.today() - timedelta(days=1)).isoformat()
    pf = tmp_path / "prices.json"; pf.write_text(_j.dumps(real))
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
    base = dict(messages=[{"role":"user","content":"hi"}], tier="tier_fast",
                api_keys={p:"k" for p in PROVIDERS}, health_aware=False)
    base.update(kw)
    return server.RouteCompletionInput(**base)


def test_streaming_success_reports_ttft(isolate, monkeypatch):
    async def fake_run_streaming(provider, model, api_key, messages, system_prompt, max_tokens):
        cr = CompletionResult(text="streamed hello", input_tokens_used=10,
                              output_tokens_used=5, latency_ms=100, model_id="m", raw_response={})
        return cr, 20, True   # ttft=20ms, started=True
    monkeypatch.setattr(server, "_run_streaming", fake_run_streaming)
    out = json.loads(_run(_params(stream=True)))
    assert out["response"] == "streamed hello"
    assert out["routing"]["streamed"] is True
    assert out["time_to_first_token_ms"] == 20


def test_stream_falls_back_for_unsupported_provider(isolate, monkeypatch):
    # Force routing to gemini (no streaming) by giving only its key; stream=True
    # must fall back to a normal call, not error.
    called = {"normal": 0}
    async def fake_call(provider, model, api_key, messages, system_prompt, max_tokens):
        called["normal"] += 1
        return CompletionResult(text="normal", input_tokens_used=1, output_tokens_used=1,
                                latency_ms=10, model_id="m", raw_response={})
    monkeypatch.setattr(server, "call_provider", fake_call)
    p = _params(stream=True, api_keys={"gemini":"k"})
    out = json.loads(_run(p))
    assert out["routing"]["streamed"] is False
    assert called["normal"] == 1


def test_mid_stream_error_does_not_failover(isolate, monkeypatch):
    # First provider raises AFTER streaming started → must be terminal, no failover.
    calls = []
    async def fake_run_streaming(provider, model, api_key, messages, system_prompt, max_tokens):
        calls.append(provider.name)
        exc = httpx.ReadError("connection dropped mid-stream")
        setattr(exc, "_cheaprouter_stream_started", True)
        raise exc
    monkeypatch.setattr(server, "_run_streaming", fake_run_streaming)
    out = json.loads(_run(_params(stream=True, max_failover=3)))
    assert "error" in out
    assert len(calls) == 1   # no failover after tokens started


def test_pre_stream_error_does_failover(isolate, monkeypatch):
    # Error BEFORE first token (started=False) → failover proceeds.
    calls = []
    async def fake_run_streaming(provider, model, api_key, messages, system_prompt, max_tokens):
        calls.append(provider.name)
        if len(calls) == 1:
            exc = httpx.ConnectError("cannot connect")
            setattr(exc, "_cheaprouter_stream_started", False)
            raise exc
        return CompletionResult(text="recovered", input_tokens_used=1, output_tokens_used=1,
                                latency_ms=10, model_id="m", raw_response={}), 15, True
    monkeypatch.setattr(server, "_run_streaming", fake_run_streaming)
    out = json.loads(_run(_params(stream=True, max_failover=2)))
    assert out["response"] == "recovered"
    assert len(calls) == 2   # failed over once


# ─── route_completion with the real streaming stack (only HTTP is mocked) ─────

def _ok_sse():
    return httpx.Response(200, content=_sse([
        'data: {"model":"served","choices":[{"delta":{"content":"ok"}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1}}',
        'data: [DONE]',
    ]))


def _stream_keys():
    """Keys only for providers whose wire format can stream, so any failover
    target also streams (independent of how prices rank them)."""
    import providers
    return {p: FAKE_KEY for p, c in providers.PROVIDERS.items() if stream_supported(c.protocol)}


def _first_pid():
    import router, providers
    from tokens import count_message_tokens
    n_in = count_message_tokens([{"role": "user", "content": "hi"}])   # what route_completion uses
    d = router.route(providers.PROVIDERS, "tier_fast", n_in, 500,
                     user_keys=_stream_keys(), health_aware=False)
    return d.ranked_pool


def test_route_real_stream_mid_body_disconnect_is_terminal(isolate, mock_http):
    import providers
    first = _first_pid()[0]
    first_host = httpx.URL(providers.PROVIDERS[first].base_url).host

    def handler(req):
        if req.url.host == first_host:
            body = _dropping_body(['data: {"choices":[{"delta":{"content":"par"}}]}'])
            return httpx.Response(200, content=body())
        return _ok_sse()
    http = mock_http(handler)
    out = json.loads(_run(_params(stream=True, max_failover=3,
                                  api_keys=_stream_keys())))
    assert out["error"] == "All attempted providers failed."
    assert [a["outcome"] for a in out["attempts"]] == ["mid_stream_error"]
    assert len(http.requests) == 1                  # never tried a second provider


def test_route_real_stream_pre_stream_error_fails_over(isolate, mock_http):
    import providers
    pool = _first_pid()
    first_host = httpx.URL(providers.PROVIDERS[pool[0]].base_url).host

    def handler(req):
        if req.url.host == first_host:
            return httpx.Response(503, content=b"down")
        return _ok_sse()
    mock_http(handler)
    out = json.loads(_run(_params(stream=True, max_failover=2,
                                  api_keys=_stream_keys())))
    assert out["response"] == "ok"
    assert out["routing"]["streamed"] is True
    assert out["routing"]["failed_over"] is True
    assert [a["outcome"] for a in out["routing"]["attempts"]] == ["transient_error", "success"]
    assert out["routing"]["provider_id"] == pool[1]
    assert out["time_to_first_token_ms"] >= 0
    assert out["cost"]["actual_input_tokens"] == 3 and out["cost"]["actual_output_tokens"] == 1
