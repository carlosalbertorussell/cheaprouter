# cheaprouter — Documentation

## Overview

cheaprouter is a BYOK (Bring Your Own Keys) MCP server that routes LLM completion
requests to the cheapest available provider in real time. You supply your own API
keys for whichever providers you have accounts with — cheaprouter compares prices,
checks availability, applies your constraints, and sends the request to the winner.

Your keys are used only for the duration of the request. They are never stored,
logged, or retained by the server in any form.

---

## Connecting

Once installed via MCPize, connect using your MCP client's remote server URL field.

**Claude Desktop** — add to `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "cheaprouter": {
      "type": "http",
      "url": "https://your-cheaprouter-endpoint.mcpize.com/mcp"
    }
  }
}
```

**Claude.ai** — go to Settings → Integrations → Add MCP Server and paste the endpoint URL.

---

## Providers

cheaprouter supports seven providers across three regions:

| Provider | Region | API key field |
|----------|--------|---------------|
| Anthropic | US | `anthropic` |
| OpenAI | US | `openai` |
| Google Gemini | US | `gemini` |
| Groq | US | `groq` |
| Mistral AI | EU | `mistral` |
| DeepSeek | CN | `deepseek` |
| Alibaba Qwen | CN | `qwen` |
| xAI Grok | US | `grok` |

You only need keys for the providers you want to use. Any provider without a key
is automatically excluded from routing.

---

## Capability Tiers

Rather than specifying model names directly, cheaprouter uses three tiers that map
to semantically equivalent models across all providers:

| Tier | Best for | Example models |
|------|----------|----------------|
| `tier_fast` | Simple tasks, high volume, cost-sensitive | Haiku, GPT-4o mini, Gemini Flash, Llama 8B, Qwen Turbo |
| `tier_balanced` | Most everyday tasks | Sonnet, GPT-4o, Gemini 1.5 Pro, Llama 70B, Qwen Plus |
| `tier_powerful` | Complex reasoning, long context | Opus, o3, Gemini 2.5 Pro, DeepSeek R1, Qwen Max |

---

## Tools

### `arbitrage_get_pricing`

Returns a full price comparison table across all providers for a given tier and
token volume. Pass your `api_keys` to see which providers you have access to —
pricing is shown for all providers regardless, so you can evaluate providers you
don't yet have keys for.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `tier` | string | ✅ | — | `tier_fast`, `tier_balanced`, or `tier_powerful` |
| `input_tokens` | integer | — | 1000 | Estimated input token count |
| `output_tokens` | integer | — | 500 | Estimated output token count |
| `api_keys` | object | — | `{}` | Your provider API keys |

**Example:**
```json
{
  "tier": "tier_balanced",
  "input_tokens": 5000,
  "output_tokens": 1000,
  "api_keys": {
    "anthropic": "sk-ant-...",
    "groq": "gsk_...",
    "deepseek": "sk-..."
  }
}
```

**Returns:** Markdown table sorted cheapest first, with input cost, output cost,
total cost, estimated latency, and availability per provider.

---

### `arbitrage_estimate_cost`

Pre-flight routing decision: given your keys and constraints, returns the
recommended provider and full cost breakdown without executing the request.
Use this to preview routing decisions before committing.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `tier` | string | ✅ | — | Capability tier |
| `input_tokens` | integer | ✅ | — | Estimated input tokens |
| `output_tokens` | integer | ✅ | — | Estimated output tokens |
| `api_keys` | object | ✅ | — | Your provider API keys |
| `latency_sensitive` | boolean | — | `false` | Exclude providers above 300ms latency |
| `excluded_providers` | array | — | `[]` | Provider IDs to skip, e.g. `["openai"]` |
| `allowed_regions` | array | — | `null` | Restrict to regions, e.g. `["us", "eu"]` |

**Example:**
```json
{
  "tier": "tier_fast",
  "input_tokens": 2000,
  "output_tokens": 500,
  "api_keys": { "groq": "gsk_...", "deepseek": "sk-..." },
  "latency_sensitive": true
}
```

**Returns:** JSON routing decision with winner, ranked alternatives, excluded
providers with reasons, and savings vs. the most expensive alternative.

---

### `arbitrage_route_completion`

Routes and executes a completion on the cheapest eligible provider. This is the
core tool — it selects the winner and makes the actual API call on your behalf.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `messages` | array | ✅ | — | `[{"role": "user", "content": "..."}]` |
| `api_keys` | object | ✅ | — | Your provider API keys |
| `tier` | string | — | `tier_fast` | Capability tier |
| `system_prompt` | string | — | `null` | Optional system prompt |
| `max_tokens` | integer | — | 2048 | Maximum output tokens |
| `latency_sensitive` | boolean | — | `false` | Exclude high-latency providers |
| `excluded_providers` | array | — | `[]` | Provider IDs to skip |
| `allowed_regions` | array | — | `null` | Restrict to these regions |
| `estimated_output_tokens` | integer | — | 500 | Used for pre-flight cost routing |

**Example:**
```json
{
  "messages": [
    { "role": "user", "content": "Summarise this report in three bullet points." }
  ],
  "tier": "tier_balanced",
  "api_keys": {
    "anthropic": "sk-ant-...",
    "openai": "sk-...",
    "groq": "gsk_...",
    "deepseek": "sk-..."
  },
  "system_prompt": "You are a concise summariser.",
  "latency_sensitive": false,
  "estimated_output_tokens": 200
}
```

**Returns:** JSON with the completion text, routing metadata (which provider was
selected, model, region), actual token usage, actual cost, and savings vs. the
most expensive eligible alternative.

---

### `arbitrage_provider_status`

Returns configuration and pricing details for one or all providers. Use this
to check which of your keys are recognised and what models are available per tier.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `api_keys` | object | — | `{}` | Your provider API keys |
| `provider_id` | string | — | `null` | Specific provider: `anthropic`, `openai`, `gemini`, `groq`, `mistral`, `deepseek`, `qwen`. If omitted, returns all. |

**Example:**
```json
{
  "api_keys": { "deepseek": "sk-..." },
  "provider_id": "deepseek"
}
```

**Returns:** JSON with name, key availability, region, latency estimate, and
model IDs with pricing per tier.

---

### `arbitrage_get_history`

Returns aggregated routing history — which providers were selected, actual token
usage, cost, latency, and cumulative savings. History is server-side and covers
all users; it records routing metadata only, never request content or API keys.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `limit` | integer | — | 20 | Number of recent records (max 100) |
| `summary_only` | boolean | — | `false` | Return aggregate stats only |

**Example:**
```json
{ "summary_only": true }
```

**Returns:** JSON with total records, successful/failed counts, total cost,
total savings, and per-provider usage counts. With `summary_only: false`,
includes individual routing records.

Pass `session` to scope history to your own records; omit it for the global view.

---

### `arbitrage_spend_report`

Breaks your spend down by provider, by tier, and by day, with total saved versus
the most expensive alternative at each routing decision. This is the analytics
view for tracking cost over time.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `session` | string | — | `null` | Scope the report to your session token. Omit for global spend. |
| `limit` | integer | — | 500 | Max records to aggregate |
| `since` | string | — | `null` | ISO date (e.g. `2026-09-01`); exclude older records |

**Example:**
```json
{ "session": "my-app-prod", "since": "2026-09-01" }
```

**Returns:** JSON with `spend_by_provider`, `spend_by_tier`, `spend_by_day`, and
totals for cost and savings.

> **Note:** durable spend across redeploys requires the Upstash backend
> (`UPSTASH_REDIS_REST_URL` + `UPSTASH_REDIS_REST_TOKEN`). With the local JSONL fallback the report
> reflects only the current container's history.

---

### `arbitrage_set_budget`

Sets a monthly USD spend budget for your session. Once set, every
`arbitrage_route_completion` call made with the same `session` token returns a
budget status, with an alert when you cross 80% or go over.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `session` | string | ✅ | — | Your session token — the budget is scoped to it |
| `monthly_usd` | number | ✅ | — | Monthly budget in USD |

**Example:**
```json
{ "session": "my-app-prod", "monthly_usd": 50.0 }
```

**Returns:** JSON confirming the stored budget and current month-to-date status.

---

### `arbitrage_provider_health`

Reports recent health per provider, derived from routing history. For each
provider seen recently, returns its success rate, how many records that's based
on, and whether it's currently healthy.

A provider is marked unhealthy only once it has enough recent samples (default 3)
and its success rate has dropped to or below the threshold (default 0.5). At that
point routing automatically **deprioritizes** it — moves it behind healthy
providers of similar price — but never excludes it, so a brief outage can't leave
you with no provider.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `window` | integer | — | 50 | How many recent records to score over |

**Example:**
```json
{ "window": 100 }
```

**Returns:** JSON mapping `provider_id` to `{score, samples, healthy}`, the list
of currently unhealthy providers, and the thresholds in effect.

---

## Sessions & spend tracking

cheaprouter never holds your identity. To track your own spend, pass a `session`
token — any opaque string you choose — on `arbitrage_route_completion`,
`arbitrage_get_history`, `arbitrage_spend_report`, and `arbitrage_set_budget`.
Reuse the same token across requests to accumulate spend under it. The server
stores only a one-way hash of the token, never the raw value, and one session
can never read another's records.

---

## Routing Logic

Providers are selected using the following priority order:

1. **Price** — always the primary signal; providers sorted cheapest-first for the given tier and token volume
2. **Key availability** — providers without a supplied API key are excluded
3. **Latency** — when `latency_sensitive: true`, providers with latency above 300ms are excluded (primarily affects DeepSeek and Qwen from outside Asia)
4. **Region** — `allowed_regions` restricts the eligible pool to specific regions; useful for data residency requirements
5. **Health** — when `health_aware: true` (default), providers failing more than half their recent calls are deprioritized (moved behind healthy providers of similar price). They are never excluded, so a blip can't strand you and an all-unhealthy pool still returns the cheapest option. `route_completion` reports any `deprioritized_providers` in its response.

---

## Pricing Notes

Token prices are embedded in the server and reflect approximate list prices as of
mid-2025. LLM pricing changes frequently. Verify current rates before making
budget-sensitive decisions:

- Anthropic: anthropic.com/pricing
- OpenAI: openai.com/pricing
- Google Gemini: ai.google.dev/pricing
- Groq: groq.com/pricing
- Mistral: mistral.ai/technology
- DeepSeek: api-docs.deepseek.com/quick_start/pricing
- Alibaba Qwen: alibabacloud.com/help/en/model-studio/models

---

## Privacy & Security

- API keys are passed per-request and used only for that request
- Keys are never stored, logged, or included in routing history
- Request content is never stored or logged
- History records contain only: timestamp, provider selected, token counts, cost, latency

---

## FAQ

**Do I need keys for all seven providers?**
No. cheaprouter works with any subset. Providers without a key are automatically
excluded from routing. Start with one or two and add more as you get accounts.

**Which providers give the best savings?**
It depends on the tier. At `tier_fast`, Groq and Qwen Turbo are typically the
cheapest. At `tier_balanced`, DeepSeek V3 often wins by a wide margin. At
`tier_powerful`, DeepSeek R1 undercuts Western flagship models significantly.

**What does `latency_sensitive` actually do?**
It excludes providers whose estimated round-trip latency exceeds 300ms. From
most of Europe and the Americas, this primarily excludes DeepSeek and Qwen
(hosted in China, ~390ms RTT). If response speed matters more than cost, set
this to `true`.

**Can I force a specific region?**
Yes — pass `allowed_regions: ["eu"]` to restrict routing to Mistral only, or
`["us"]` for Anthropic, OpenAI, Gemini, and Groq.

**Can I exclude a specific provider?**
Yes — pass `excluded_providers: ["openai"]` or any combination of provider IDs.

**Are there rate limits on cheaprouter itself?**
No. cheaprouter makes direct API calls to your chosen providers using your keys.
Any rate limits are those imposed by the providers on your account.
