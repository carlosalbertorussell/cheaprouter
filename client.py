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

import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from providers import ProviderConfig, ModelConfig

REQUEST_TIMEOUT = 120.0


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
