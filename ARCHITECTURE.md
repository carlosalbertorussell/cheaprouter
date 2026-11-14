# Architecture

cheaprouter is a stateless BYOK routing layer in front of eight LLM providers.
It holds no provider credentials and stores no request content. The value it adds is the
routing decision: given a task tier and token estimate, which provider is cheapest
right now, subject to the caller's latency and region constraints.

## Module map

```
server.py     — FastMCP router server; defines its 10 tools and their input schemas
router.py     — routing decision engine (price → availability → latency → region)
pricing.py    — cost calculation and comparison-table rendering
providers.py  — provider registry, built from the price table; regions, key resolution
pricing_table.py — loads/validates prices.json; staleness guard (S4a); tier-aware (SP1a)
provenance.py — the three trust tiers: verified / proxy / observed (SP1a; pricing-server spine)
price_history.py — append-only trajectory record; versioned-file backend, git = audit log (SP1b)
pricing_server.py — SECOND MCP entrypoint: serves verified price truth (5 tools: get/list/drift/history/observed), provenance-first (SP1c)
refresh.py    — drift check vs fetchable sources; OpenRouter feed for 6 providers (S4b/SC2)
prices.json   — versioned price table with provenance (verified_at, max_age_days)
client.py     — async HTTP client; three wire protocols; transient-error classifier (S2); SSE streaming layer (S5)
history.py    — spend analytics: sessions, spend report, budgets (over storage.py)
storage.py    — pluggable history backend (Upstash Redis when configured, JSONL fallback)
health.py     — per-provider health from history; deprioritizes failing providers (S8)
tokens.py     — input-token counting for cost routing (one tiktoken o200k_base encoder for all providers, S9)
```

## Request flow: `arbitrage_route_completion`

```
caller (MCP client)
   │  messages + api_keys + tier + constraints
   ▼
server.py  ── counts input tokens (tokens.py: tiktoken o200k_base, S9)
   │
   ▼
router.py  ── estimate_all_providers()  → ranks by cost
   │          applies exclusions: no-key, excluded_providers,
   │          blocked/allowed regions, latency threshold
   │          health: unhealthy providers sink behind all healthy ones
   │          ranked pool = failover order; winner = first
   ▼
providers.py ── resolve_api_key(pid, api_keys)   [BYOK, never stored;
   │             env-var fallback only over stdio or ARBITRAGE_ALLOW_ENV_KEYS=1]
   │
   ▼
client.py  ── call_provider() dispatches by protocol:
   │            anthropic  → POST /v1/messages
   │            openai     → POST /v1/chat/completions  (OpenAI/Groq/Mistral/DeepSeek/Qwen/Grok)
   │            gemini     → POST /v1beta/models/{model}:generateContent
   ▼
history.py ── log_decision()  [metadata only: provider, tokens, cost, latency — no keys, no content;
   │           a failed attempt is logged with cost 0 and saved 0]
   ▼
caller ◀── response + routing audit + actual cost + savings
           (actual cost and savings use the cache-aware formula at the actual
            token counts, for the provider that answered, vs. the most
            expensive eligible provider)
```

## Capability tiers

Rather than exposing 24 model names, cheaprouter groups each provider's models
into three tiers. A tier is a price bucket of comparable models, not a claim that
they are equivalent; the router picks the cheapest in the tier you ask for.

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
5. **Health** (S8) — providers failing recently are sunk behind *all* healthy ones, whatever their price; never excluded, so a blip can't strand a caller. Health is scored on the server's global history, shared across users, but counts only provider-side transient failures (never a caller's bad key or bad request), caps each session's contribution, and needs failures from at least two sessions (ST5)
6. **Failover** (S2) — on a transient error the next provider in this order is tried, up to `max_failover`

## BYOK & privacy invariants

- API keys arrive per request in `api_keys`, are used for that one call, and are never persisted or logged
- Request content (`messages`) is forwarded to the chosen provider and never stored
- History records carry only: timestamp, hashed session, provider, tier, token counts, cost, savings, latency, success/error class, transient-failure flag
- A session token is a grouping key, not access control. `arbitrage_get_history` and `arbitrage_spend_report` return only the caller's records: a token reads its own, no token reads only the shared `anon` session. An all-sessions view needs the operator opt-in `ARBITRAGE_ALLOW_GLOBAL_READS=1`
- The server holds no provider credentials. Over HTTP it ignores provider key env vars, so it can never spend an operator's keys; the env fallback works only over stdio or with `ARBITRAGE_ALLOW_ENV_KEYS=1`

## Transport

- **Local:** `python server.py --stdio` (stdio, for Claude Desktop)
- **Cloud (MCPize):** default `streamable-http` at `/mcp`; host `0.0.0.0`, port from `PORT` env (default 8081), both set in the FastMCP constructor

## Deliberate non-features

- No server-side key storage — BYOK is the security model, not a limitation
- History is shared — it holds no content or keys. It is durable with Upstash and falls back to a JSONL file that resets on redeploy
- Budgets are alerts, not enforcement — a request over budget still runs
- No endpoint auth in the server — delegated to the MCP client and MCPize platform layer
