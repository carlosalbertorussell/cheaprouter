"""
Async API client for cheaprouter.

BYOK model: api_key is passed explicitly on every call — never read from the
provider object or stored anywhere. Keys are never logged.

Handles two wire formats:
  - "anthropic"  →  POST /v1/messages
  - "openai"     →  POST /v1/chat/completions  (OpenAI, Groq, Mistral, DeepSeek, Qwen)
  - "gemini"     →  POST /v1beta/models/{model}:generateContent
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from providers import ProviderConfig, ModelConfig

REQUEST_TIMEOUT = 120.0


def is_transient_error(exc: Exception) -> bool:
    """
    True if an exception is worth failing over to another provider.

    Transient (retry on next provider): rate limits (429), server errors (5xx),
    timeouts, and connection errors — these are provider-specific and a different
    provider may well succeed.

    Non-transient (fail immediately, no failover): auth errors (401/403) and bad
    requests (400/404/422) — these reflect the key or the request itself and would
    fail identically everywhere, so retrying just wastes calls.
    """
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError,
                        httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 429 or 500 <= code <= 599:
            return True
        return False   # 4xx other than 429 → not transient
    return False       # unknown error type → don't fail over blindly


@dataclass
class CompletionResult:
    text: str
    input_tokens_used: int
    output_tokens_used: int
    latency_ms: int
    model_id: str
    raw_response: dict


async def call_provider(
    provider: ProviderConfig,
    model: ModelConfig,
    api_key: str,
    messages: list[dict],
    system_prompt: Optional[str] = None,
    max_tokens: int = 2048,
) -> CompletionResult:
    """
    Dispatch a completion request to the given provider.

    Args:
        provider:      ProviderConfig from the registry
        model:         ModelConfig for the target tier
        api_key:       Caller-supplied API key (BYOK — never stored or logged)
        messages:      List of {"role": "user"|"assistant", "content": str}
        system_prompt: Optional system prompt
        max_tokens:    Maximum output tokens
    """
    protocol = provider.protocol
    if protocol == "anthropic":
        return await _call_anthropic(provider, model, api_key, messages, system_prompt, max_tokens)
    elif protocol == "openai":
        return await _call_openai_compat(provider, model, api_key, messages, system_prompt, max_tokens)
    elif protocol == "gemini":
        return await _call_gemini(provider, model, api_key, messages, system_prompt, max_tokens)
    else:
        raise ValueError(f"Unknown protocol '{protocol}' for provider {provider.name}")


async def _call_anthropic(
    provider: ProviderConfig,
    model: ModelConfig,
    api_key: str,
    messages: list[dict],
    system_prompt: Optional[str],
    max_tokens: int,
) -> CompletionResult:
    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
        **provider.extra_headers,
    }
    body: dict[str, Any] = {
        "model": model.model_id,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system_prompt:
        body["system"] = system_prompt

    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        r = await client.post(f"{provider.base_url}/v1/messages", headers=headers, json=body)
        r.raise_for_status()
    latency_ms = int((time.monotonic() - t0) * 1000)

    data = r.json()
    text = next(
        (b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"),
        ""
    )
    usage = data.get("usage", {})
    return CompletionResult(
        text=text,
        input_tokens_used=usage.get("input_tokens", 0),
        output_tokens_used=usage.get("output_tokens", 0),
        latency_ms=latency_ms,
        model_id=data.get("model", model.model_id),
        raw_response=data,
    )


async def _call_openai_compat(
    provider: ProviderConfig,
    model: ModelConfig,
    api_key: str,
    messages: list[dict],
    system_prompt: Optional[str],
    max_tokens: int,
) -> CompletionResult:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        **provider.extra_headers,
    }
    full_messages = []
    if system_prompt:
        full_messages.append({"role": "system", "content": system_prompt})
    full_messages.extend(messages)

    body: dict[str, Any] = {
        "model": model.model_id,
        "max_tokens": max_tokens,
        "messages": full_messages,
    }

    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        r = await client.post(f"{provider.base_url}/v1/chat/completions", headers=headers, json=body)
        r.raise_for_status()
    latency_ms = int((time.monotonic() - t0) * 1000)

    data = r.json()
    text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    usage = data.get("usage", {})
    return CompletionResult(
        text=text,
        input_tokens_used=usage.get("prompt_tokens", 0),
        output_tokens_used=usage.get("completion_tokens", 0),
        latency_ms=latency_ms,
        model_id=data.get("model", model.model_id),
        raw_response=data,
    )


async def _call_gemini(
    provider: ProviderConfig,
    model: ModelConfig,
    api_key: str,
    messages: list[dict],
    system_prompt: Optional[str],
    max_tokens: int,
) -> CompletionResult:
    url = (
        f"{provider.base_url}/v1beta/models/{model.model_id}"
        f":generateContent?key={api_key}"
    )
    contents = [
        {"role": "user" if m["role"] == "user" else "model",
         "parts": [{"text": m["content"]}]}
        for m in messages
    ]
    body: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    if system_prompt:
        body["systemInstruction"] = {"parts": [{"text": system_prompt}]}

    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        r = await client.post(url, headers={"Content-Type": "application/json"}, json=body)
        r.raise_for_status()
    latency_ms = int((time.monotonic() - t0) * 1000)

    data = r.json()
    text = (
        data.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [{}])[0]
        .get("text", "")
    )
    usage = data.get("usageMetadata", {})
    return CompletionResult(
        text=text,
        input_tokens_used=usage.get("promptTokenCount", 0),
        output_tokens_used=usage.get("candidatesTokenCount", 0),
        latency_ms=latency_ms,
        model_id=model.model_id,
        raw_response=data,
    )


# ─── Streaming (S5) ───────────────────────────────────────────────────────────
#
# A streaming layer that consumes each provider's SSE stream and yields text
# chunks as they arrive. This lowers time-to-first-token and lets the caller show
# liveness. Supports the Anthropic and OpenAI-compatible wire formats (6 of 8
# providers); Gemini's streaming uses a different framing and is not covered here,
# so stream_supported() reports which providers can stream.
#
# Failover note (see server.py): failover is only safe BEFORE the first chunk.
# Once any token has been yielded the caller may have seen partial output, so a
# mid-stream error is terminal — never silently retried on another provider.

from dataclasses import dataclass as _dataclass


@_dataclass
class StreamChunk:
    text: str = ""                    # incremental text (may be empty on control frames)
    done: bool = False                # final frame
    input_tokens_used: int = 0        # populated on/near the final frame when available
    output_tokens_used: int = 0
    model_id: str = ""


def stream_supported(protocol: str) -> bool:
    """Whether the streaming layer supports a provider's wire format."""
    return protocol in ("anthropic", "openai")


async def stream_provider(
    provider: ProviderConfig,
    model: ModelConfig,
    api_key: str,
    messages: list[dict],
    system_prompt: Optional[str] = None,
    max_tokens: int = 2048,
):
    """
    Async generator yielding StreamChunk objects from a provider's SSE stream.

    Raises (before the first yielded chunk) the same httpx errors as call_provider,
    so pre-stream failover can classify them. Once the first chunk is yielded, an
    error propagates as-is and must be treated as terminal by the caller.
    """
    protocol = provider.protocol
    if protocol == "anthropic":
        gen = _stream_anthropic(provider, model, api_key, messages, system_prompt, max_tokens)
    elif protocol == "openai":
        gen = _stream_openai_compat(provider, model, api_key, messages, system_prompt, max_tokens)
    else:
        raise ValueError(f"Streaming not supported for protocol {protocol!r}")
    async for chunk in gen:
        yield chunk


async def _stream_anthropic(provider, model, api_key, messages, system_prompt, max_tokens):
    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        **provider.extra_headers,
    }
    body: dict[str, Any] = {
        "model": model.model_id,
        "max_tokens": max_tokens,
        "messages": messages,
        "stream": True,
    }
    if system_prompt:
        body["system"] = system_prompt

    in_tok = out_tok = 0
    model_id = model.model_id
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        async with client.stream("POST", f"{provider.base_url}/v1/messages",
                                 headers=headers, json=body) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    evt = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                etype = evt.get("type")
                if etype == "message_start":
                    usage = evt.get("message", {}).get("usage", {})
                    in_tok = usage.get("input_tokens", in_tok)
                    model_id = evt.get("message", {}).get("model", model_id)
                elif etype == "content_block_delta":
                    delta = evt.get("delta", {})
                    text = delta.get("text", "")
                    if text:
                        yield StreamChunk(text=text, model_id=model_id)
                elif etype == "message_delta":
                    out_tok = evt.get("usage", {}).get("output_tokens", out_tok)
                elif etype == "message_stop":
                    break
    yield StreamChunk(done=True, input_tokens_used=in_tok,
                      output_tokens_used=out_tok, model_id=model_id)


async def _stream_openai_compat(provider, model, api_key, messages, system_prompt, max_tokens):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        **provider.extra_headers,
    }
    full_messages = []
    if system_prompt:
        full_messages.append({"role": "system", "content": system_prompt})
    full_messages.extend(messages)
    body: dict[str, Any] = {
        "model": model.model_id,
        "max_tokens": max_tokens,
        "messages": full_messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    in_tok = out_tok = 0
    model_id = model.model_id
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        async with client.stream("POST", f"{provider.base_url}/v1/chat/completions",
                                 headers=headers, json=body) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    evt = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                model_id = evt.get("model", model_id)
                choices = evt.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    text = delta.get("content") or ""
                    if text:
                        yield StreamChunk(text=text, model_id=model_id)
                usage = evt.get("usage")
                if usage:
                    in_tok = usage.get("prompt_tokens", in_tok)
                    out_tok = usage.get("completion_tokens", out_tok)
    yield StreamChunk(done=True, input_tokens_used=in_tok,
                      output_tokens_used=out_tok, model_id=model_id)
