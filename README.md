# cheaprouter

![CI](https://github.com/carlosalbertorussell/cheaprouter/actions/workflows/ci.yml/badge.svg)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)

An MCP server that routes LLM completion requests to the cheapest available provider,
selecting dynamically based on token price, region, latency, and availability.

**BYOK** — bring your own provider keys, passed per request. cheaprouter holds no
credentials and stores no content.

**Docs:** [CONNECT](CONNECT.md) · [MCPize tool reference](MCPIZE_DOCS.md) · [Architecture](ARCHITECTURE.md) · [Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md) · [Roadmap](SPRINTS.md)

## Providers

| Provider | Region | Tier fast | Tier balanced | Tier powerful |
|----------|--------|-----------|---------------|---------------|
| Anthropic | US | claude-haiku-4-5 | claude-sonnet-4-6 | claude-opus-4-6 |
| OpenAI | US | gpt-4o-mini | gpt-4o | o3 |
| Google Gemini | US | gemini-2.0-flash | gemini-1.5-pro | gemini-2.5-pro |
| Groq | US | llama-3.1-8b-instant | llama-3.3-70b | llama-3.3-70b |
| Mistral AI | EU | mistral-small | mistral-medium | mistral-large |
| DeepSeek | CN | deepseek-chat (V3) | deepseek-chat (V3) | deepseek-reasoner (R1) |
| Alibaba Qwen | CN | qwen-turbo | qwen-plus | qwen-max |
| xAI Grok | US | grok-4.1-fast | grok-4.3 | grok-4 |

> **Note on CN providers (DeepSeek, Qwen):** Latency from Buenos Aires is ~390ms.
> Pass `latency_sensitive: true` to route_completion to exclude them automatically
> when response time matters more than cost.

## Setup

```bash
git clone <repo>
cd cheaprouter
pip install -r requirements.txt
cp .env.example .env
# edit .env — add API keys for the providers you want to use
```

The server works with any subset of providers. Unconfigured providers (no API key)
are silently excluded from routing.

## Running

**HTTP — default (MCPize cloud / remote clients):**
```bash
python server.py
# Listens on port 8000
```

**stdio — local only (Claude Desktop / local MCP clients):**
```bash
python server.py --stdio
```

## Deploy to MCPize

```bash
npm install -g mcpize
mcpize login

# From your GitHub repo (MCPize reads mcpize.yaml automatically):
mcpize deploy
```

No secrets to configure — cheaprouter is BYOK. Users supply their own provider
keys per request in the `api_keys` parameter; the server holds no credentials.
MCPize handles hosting, SSL, and scaling, giving you a public HTTPS endpoint you
can connect to from Claude.ai or any MCP-compatible client. See [CONNECT.md](CONNECT.md).

> **Note on history:** The routing history file lives at `/tmp/routing_history.jsonl`
> on MCPize and resets on each redeploy. For persistent spend tracking, override
> `ARBITRAGE_HISTORY_FILE` to an external path or pipe logs to a storage service.

## Claude Desktop config (local stdio)

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "token-arbitrage": {
      "command": "python",
      "args": ["/path/to/cheaprouter/server.py", "--stdio"],
      "env": {
        "ANTHROPIC_API_KEY": "sk-ant-...",
        "OPENAI_API_KEY": "sk-...",
        "GROQ_API_KEY": "gsk_...",
        "DEEPSEEK_API_KEY": "sk-..."
      }
    }
  }
}
```

## Tools

### `arbitrage_get_pricing`
Full price comparison table for a tier + token volume.
```json
{ "tier": "tier_balanced", "input_tokens": 5000, "output_tokens": 1000 }
```

### `arbitrage_estimate_cost`
Pre-flight routing decision without executing the request.
```json
{
  "tier": "tier_fast",
  "input_tokens": 2000,
  "output_tokens": 500,
  "latency_sensitive": false,
  "excluded_providers": ["openai"]
}
```

### `arbitrage_route_completion`
Route and execute a completion on the cheapest eligible provider.
```json
{
  "messages": [{"role": "user", "content": "Summarise this in one sentence."}],
  "tier": "tier_fast",
  "latency_sensitive": false,
  "estimated_output_tokens": 100
}
```

### `arbitrage_provider_status`
Check which providers are configured and their model/price details.
```json
{ "provider_id": "deepseek" }
```

### `arbitrage_get_history`
Past routing decisions with spend and savings tracking.
```json
{ "limit": 50, "summary_only": true }
```

## Routing logic

Priority order:
1. **Price** — always the primary signal; providers sorted cheapest-first
2. **Availability** — providers without an API key are excluded
3. **Latency** — when `latency_sensitive=True`, providers above `LATENCY_THRESHOLD_MS`
   (default 300ms) are excluded; mainly affects DeepSeek and Qwen from LATAM
4. **Region** — optional `allowed_regions` or global `BLOCKED_REGIONS` env var

## Pricing notes

Token prices are hardcoded in `providers.py` and reflect approximate list prices
as of mid-2025. Prices change frequently — verify against official pricing pages:
- https://www.anthropic.com/pricing
- https://openai.com/pricing
- https://ai.google.dev/pricing
- https://groq.com/pricing/
- https://mistral.ai/technology/
- https://api-docs.deepseek.com/quick_start/pricing
- https://www.alibabacloud.com/help/en/model-studio/models
- https://docs.x.ai/docs/models

## Project structure

```
cheaprouter/
├── server.py        — FastMCP server, all tool definitions
├── providers.py     — Provider registry: models, pricing, regions, BYOK key resolution
├── pricing.py       — Cost calculation and comparison tables
├── router.py        — Routing decision engine
├── client.py        — Async API client (Anthropic / OpenAI-compat / Gemini)
├── history.py       — spend analytics: sessions, spend report, budgets
├── storage.py       — pluggable history backend (Supabase / JSONL)
├── tests/           — pytest suite (pricing, routing, providers, key-leak guard)
├── Dockerfile       — container build
├── Makefile         — install / test / run shortcuts
├── pyproject.toml   — package metadata and dependencies
├── mcpize.yaml      — MCPize deployment config
├── requirements.txt
├── .env.example
├── CONNECT.md       — how to connect from MCP clients
├── MCPIZE_DOCS.md   — full tool reference for the MCPize listing
├── ARCHITECTURE.md  — design and request flow
├── CONTRIBUTING.md
├── CHANGELOG.md
├── SECURITY.md
├── LICENSE          — MIT
└── README.md
```

## License

MIT — see [LICENSE](LICENSE). Free to use, modify, and distribute.
