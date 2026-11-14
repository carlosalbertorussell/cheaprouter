---
name: cheaprouter
description: Route an LLM completion to the cheapest provider within a chosen capability tier across 8 providers (Anthropic, OpenAI, Gemini, Groq, Mistral, DeepSeek, Qwen, Grok). Use when cost per token matters, when comparing provider prices, or when you want automatic failover to the next provider in cost order. BYOK — the caller supplies their own provider API keys per request; the server stores no keys and no content.
---

# cheaprouter — cheapest-capable LLM routing (BYOK)

cheaprouter is an MCP server that, given a prompt and a **capability tier**, sends the
completion to the **cheapest provider you hold a key for in that tier** — across
Anthropic, OpenAI, Google Gemini, Groq, Mistral, DeepSeek, Alibaba Qwen and xAI Grok.
A tier is a price bucket of comparable models, not a quality guarantee: pick the tier
the task needs and exclude providers you don't want. The caller
passes their own provider API keys per request (`api_keys`); the server stores no keys
and no message content.

## When to reach for it
- You want the cheapest provider in a tier, not a fixed provider.
- You want to estimate or compare per-token cost across providers before sending.
- You want automatic failover to the next provider on a transient error.
- You care about data residency — route only within allowed regions.

## Capability tiers
Pick by how hard the task is, not by provider:
- `tier_fast` — simple, high-volume, cost-sensitive work.
- `tier_balanced` — most everyday tasks.
- `tier_powerful` — complex reasoning, long context.

## Core tools
**Routing (the router head):**
- `arbitrage_route_completion` — route to the cheapest eligible provider and run the
  completion; reports the actual cost and the savings of the provider that answered
  vs the priciest eligible option (both at the actual token counts), and whether it
  failed over. Supports `allowed_regions`, `excluded_providers`,
  `latency_sensitive`, `health_aware`, optional `stream`, and cached-input hints.
- `arbitrage_estimate_cost` — estimate cost per provider before sending.
- `arbitrage_count_tokens` — input-token count with one tiktoken `o200k_base` encoder
  for every provider (close to, not identical with, each provider's own count).
- `arbitrage_get_pricing` / `arbitrage_provider_status` — current prices / which
  providers have a key supplied. Nothing checks that a provider is reachable before
  routing; failover handles errors after the fact.
- `arbitrage_provider_health` — recent success rate from the server's shared history,
  used to deprioritize flaky providers.
- `arbitrage_spend_report` / `arbitrage_set_budget` / `arbitrage_get_history` —
  per-session spend analytics and budget alerts (alerts only; nothing is blocked).
  Without `session`, history and spend reports cover every session.
- `arbitrage_check_price_drift` — compare table prices against a programmatic feed.

**Price truth (the pricing head, provenance-first):**
- `pricing_get` / `pricing_list` — verified price for a model / the whole table, each
  response leading with provenance (tier, verified_at, source, staleness).
- `pricing_drift` — where a proxy feed disagrees with the verified table (a re-verify
  signal, never an auto-update).
- `pricing_history` — how a price has moved over time (the citable trajectory).
- `pricing_observed` — compares the verified table with the unit prices the router
  reports for real completions (today that is the router's own table rate).

## BYOK — keys
Pass an `api_keys` object per request, supplying only the providers you want eligible.
Keys are used for that request and are never stored or logged. There is no server-held
provider credential: a hosted server ignores provider key environment variables.

## Prices are versioned and staleness-guarded
Prices live in a versioned table with a `verified_at` date; routing and estimation
**refuse** rather than route on prices older than the max age, so you never silently
pay against stale numbers. Provenance is explicit per the three tiers —
`verified` (human-checked, citable), `proxy` (aggregator feed), `observed` (empirical
from real routing) — and they are never blended.

## Example
> Route this summarization prompt to the cheapest fast-tier provider in the US or EU,
> with my OpenAI and Mistral keys.

→ `arbitrage_route_completion` with `tier="tier_fast"`,
`allowed_regions=["us","eu"]`, `api_keys={"openai": "...", "mistral": "..."}`.
