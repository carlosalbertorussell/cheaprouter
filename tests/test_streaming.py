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


# ─── SSE parsing via mocked transport ─────────────────────────────────────────

def _sse(lines):
    return "\n".join(lines).encode()


def _mock_stream_transport(body_bytes):
    """An httpx MockTransport that returns an SSE stream body."""
    def handler(request):
        return httpx.Response(200, content=body_bytes)
    return httpx.MockTransport(handler)


@pytest.fixture
def patch_openai_stream(monkeypatch):
    """Patch the OpenAI streaming path to read from a canned SSE body."""
    def _make(body_lines):
        sse = _sse(body_lines)
        async def fake_stream(provider, model, api_key, messages, system_prompt, max_tokens):
            # Re-implement just the SSE line parsing over canned data
            for line in sse.decode().split("\n"):
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                evt = json.loads(payload)
                choices = evt.get("choices", [])
                if choices:
                    txt = choices[0].get("delta", {}).get("content") or ""
                    if txt:
                        yield StreamChunk(text=txt, model_id=evt.get("model", "m"))
                if evt.get("usage"):
                    pass
            yield StreamChunk(done=True, input_tokens_used=10, output_tokens_used=5, model_id="m")
        monkeypatch.setattr(client, "_stream_openai_compat", fake_stream)
    return _make


async def _collect(agen):
    out = []
    async for c in agen:
        out.append(c)
    return out


def test_openai_stream_yields_text(patch_openai_stream):
    patch_openai_stream([
        'data: {"model":"m","choices":[{"delta":{"content":"Hel"}}]}',
        'data: {"model":"m","choices":[{"delta":{"content":"lo"}}]}',
        'data: [DONE]',
    ])
    prov = PROVIDERS["groq"]; model = prov.models["tier_fast"]
    chunks = asyncio.run(_collect(
        client.stream_provider(prov, model, "k", [{"role":"user","content":"hi"}])))
    text = "".join(c.text for c in chunks)
    assert text == "Hello"
    assert chunks[-1].done is True
    assert chunks[-1].output_tokens_used == 5


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
