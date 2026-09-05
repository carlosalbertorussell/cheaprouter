# Architecture

cheaprouter is a stateless BYOK routing layer in front of eight LLM providers.
It holds no credentials and stores no request content. The value it adds is the
routing decision: given a task tier and token estimate, which provider is cheapest
right now, subject to the caller's latency and region constraints.

## Module map

```
server.py     — FastMCP server; defines the 5 tools and their input schemas
router.py     — routing decision engine (price → availability → latency → region)
pricing.py    — cost calculation and comparison-table rendering
providers.py  — provider registry: models, per-tier pricing, regions, key resolution
client.py     — async HTTP client; three wire protocols
history.py    — spend analytics: sessions, spend report, budgets (over storage.py)
storage.py    — pluggable history backend (Upstash Redis when configured, JSONL fallback)
```

## Request flow: `arbitrage_route_completion`

```
caller (MCP client)
   │  messages + api_keys + tier + constraints
   ▼
server.py  ── estimates input tokens (chars/4)
   │
   ▼
router.py  ── estimate_all_providers()  → ranks by cost
   │          applies exclusions: no-key, excluded_providers,
   │          blocked/allowed regions, latency threshold
   │          winner = cheapest eligible
   ▼
providers.py ── resolve_api_key(winner, api_keys)   [BYOK, never stored]
   │
   ▼
client.py  ── call_provider() dispatches by protocol:
   │            anthropic  → POST /v1/messages
   │            openai     → POST /v1/chat/completions  (OpenAI/Groq/Mistral/DeepSeek/Qwen/Grok)
   │            gemini     → POST /v1beta/models/{model}:generateContent
   ▼
history.py ── log_decision()  [metadata only: provider, tokens, cost, latency — no keys, no content]
   │
   ▼
caller ◀── response + routing audit + actual cost + savings
```

## Capability tiers

Rather than exposing 24 model names, cheaprouter maps three tiers across all
providers so callers compare like with like:

| Tier | Intent |
|------|--------|
| `tier_fast` | High-volume, cost-sensitive, simple tasks |
| `tier_balanced` | Everyday tasks — best quality/cost ratio |
| `tier_powerful` | Complex reasoning, long context |

## Routing priority

1. **Price** — primary sort key; cheapest eligible provider wins
2. **Availability** — providers without a supplied key are excluded
3. **Latency** — when `latency_sensitive=true`, providers above the threshold (default 300ms) are dropped
4. **Region** — `allowed_regions` / `BLOCKED_REGIONS` constrain the pool

## BYOK & privacy invariants

- API keys arrive per request in `api_keys`, are used for that one call, and are never persisted or logged
- Request content (`messages`) is forwarded to the chosen provider and never stored
- History records carry only: timestamp, provider, token counts, cost, latency
- The server itself holds no credentials — there is nothing to exfiltrate from it

## Transport

- **Local:** `python server.py --stdio` (stdio, for Claude Desktop)
- **Cloud (MCPize):** default `streamable-http`; host `0.0.0.0`, port from `PORT` env, both set in the FastMCP constructor

## Deliberate non-features

- No server-side key storage — BYOK is the security model, not a limitation
- History is shared and ephemeral — it holds no content or keys, and resets on redeploy
- No endpoint auth in the server — delegated to the MCP client and MCPize platform layer
