# cheaprouter

![CI](https://github.com/carlosalbertorussell/cheaprouter/actions/workflows/ci.yml/badge.svg)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)
[![MCPize](https://mcpize.com/badge/@crussell/cheaprouter)](https://mcpize.com/mcp/cheaprouter)

An MCP server that routes LLM completion requests to the cheapest available provider,
selecting dynamically based on token price, region, latency, and availability.

**BYOK** — bring your own provider keys, passed per request. cheaprouter holds no
provider credentials, stores no content, and a hosted server never falls back to
the operator's keys.

**Docs:** [CONNECT](CONNECT.md) · [MCPize tool reference](MCPIZE_DOCS.md) · [Architecture](ARCHITECTURE.md) · [Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md) · [Pipeline (superseded)](PIPELINE.md) · [Pricing blueprint](PRICING_SERVER_BLUEPRINT.md) · [Pricing strategy](PRICING_STRATEGY.md) · [Ticker vision](TICKER_VISION.md) · [Roadmap](SPRINTS.md) · [Review response](REVIEW_RESPONSE.md)

## Connect

Hosted on MCPize. One-line install:

```bash
npx -y mcpize connect @crussell/cheaprouter --client claude
```

Per client:

- **Claude Code:** `claude mcp add --transport http CheapRouter https://cheaprouter.mcpize.run/mcp`
- **Claude.ai:** Settings → Connectors → Add custom connector → `https://cheaprouter.mcpize.run/mcp`
- **Cursor:** `cursor mcp add CheapRouter https://cheaprouter.mcpize.run/mcp`
- **Windsurf:** `windsurf mcp add CheapRouter https://cheaprouter.mcpize.run/mcp`
- **Cline:** `cline mcp add CheapRouter https://cheaprouter.mcpize.run/mcp`

The MCP endpoint (streamable HTTP) is `https://cheaprouter.mcpize.run/mcp`.

> Hosted access currently requires a free MCPize account (OAuth sign-in). The code
> is MIT, so you can also run your own instance for open, no-account access.

## Providers

| Provider | Region | Tier fast | Tier balanced | Tier powerful |
|----------|--------|-----------|---------------|---------------|
| Anthropic | US | claude-haiku-5-5 | claude-sonnet-5-5 | claude-opus-5-5 |
| OpenAI | US | gpt-6-luna | gpt-6.1-terra | gpt-6.1-sol |
| Google Gemini | US | gemini-3.1-flash-lite | gemini-3.1-flash-lite | gemini-3.1-pro-preview |
| Groq | US | openai/gpt-oss-20b | openai/gpt-oss-120b | openai/gpt-oss-120b |
| Mistral AI | EU | mistral-small-latest | mistral-large-latest | mistral-large-latest |
| DeepSeek | CN | deepseek-flash | deepseek-flash | deepseek-v4-pro |
| Alibaba Qwen | CN | qwen-flash | qwen-plus | qwen-max |
| xAI Grok | US | grok-4.3 | grok-4.3 | grok-4.7 |

> **Note on CN providers (DeepSeek, Qwen):** Latency from Buenos Aires is ~390ms.
> Pass `latency_sensitive: true` to route_completion to exclude them automatically
> when response time matters more than cost.

## Setup

```bash
git clone <repo>
cd cheaprouter
pip install -r requirements.txt
cp .env.example .env
# optional: provider keys in .env are used only over stdio (see Running)
```

The server works with any subset of providers. Providers without a key are
excluded from routing, with the reason shown in `excluded`.

## Running

**HTTP — default (MCPize cloud / remote clients):**
```bash
python server.py
# streamable HTTP on $PORT (default 8081), endpoint /mcp
```
Over HTTP only the keys a caller passes in `api_keys` count. Provider key env
vars are ignored, so a hosted server never spends the operator's keys. Set
`ARBITRAGE_ALLOW_ENV_KEYS=1` only for a private deployment you protect yourself.

**stdio — local only (Claude Desktop / local MCP clients):**
```bash
python server.py --stdio
```
Over stdio, provider env vars (`ANTHROPIC_API_KEY`, ...) fill in for providers the
caller leaves out of `api_keys`. A key passed in `api_keys` always wins.

## Deploy to MCPize

```bash
npm install -g mcpize
mcpize login

# From your GitHub repo (MCPize reads mcpize.yaml automatically):
mcpize deploy
```

No provider secrets to configure — cheaprouter is BYOK. Users supply their own
provider keys per request in the `api_keys` parameter. Do not set provider keys as
MCPize secrets: the HTTP server ignores them. MCPize handles hosting, SSL, and
scaling, giving you a public HTTPS endpoint you can connect to from Claude.ai or
any MCP-compatible client. See [CONNECT.md](CONNECT.md).

> **Note on history:** with `UPSTASH_REDIS_REST_URL` + `UPSTASH_REDIS_REST_TOKEN`
> set, routing history is durable across redeploys ([setup](docs/upstash-setup.md)).
> Without them it falls back to a JSONL file (`ARBITRAGE_HISTORY_FILE`, default
> `/tmp/routing_history.jsonl`), which resets on each MCPize redeploy.

## Claude Desktop config (local stdio)

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "cheaprouter": {
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

The router (`server.py`) registers 10 tools. Defaults in brackets; **bold** means
required.

| Tool | Parameters |
|------|------------|
| `arbitrage_get_pricing` | **`tier`**, `input_tokens` [1000], `output_tokens` [500], `cached_input_tokens` [0], `api_keys` [{}] |
| `arbitrage_estimate_cost` | **`tier`**, **`input_tokens`**, **`output_tokens`**, `cached_input_tokens` [0], `api_keys` [{}], `latency_sensitive` [false], `excluded_providers` [[]], `allowed_regions` [null] |
| `arbitrage_route_completion` | **`messages`**, **`api_keys`**, `tier` [tier_fast], `system_prompt` [null], `max_tokens` [2048], `latency_sensitive` [false], `excluded_providers` [[]], `allowed_regions` [null], `estimated_output_tokens` [500], `cached_input_tokens` [0], `stream` [false], `session` [null], `health_aware` [true], `max_failover` [2] |
| `arbitrage_provider_status` | `provider_id` [null = all], `api_keys` [{}] |
| `arbitrage_get_history` | `limit` [20, max 100], `summary_only` [false], `session` [null] |
| `arbitrage_spend_report` | `session` [null], `limit` [500, max 2000], `since` [null] |
| `arbitrage_set_budget` | **`session`**, **`monthly_usd`** |
| `arbitrage_provider_health` | `window` [50] |
| `arbitrage_count_tokens` | **`messages`**, `system_prompt` [null] |
| `arbitrage_check_price_drift` | `propose` [false] |

Full reference with return shapes: [MCPIZE_DOCS.md](MCPIZE_DOCS.md).

### `arbitrage_get_pricing`
```json
{ "tier": "tier_balanced", "input_tokens": 5000, "output_tokens": 1000 }
```

### `arbitrage_estimate_cost`
```json
{
  "tier": "tier_fast",
  "input_tokens": 2000,
  "output_tokens": 500,
  "api_keys": { "groq": "gsk_...", "deepseek": "sk-..." },
  "excluded_providers": ["openai"]
}
```

### `arbitrage_route_completion`
```json
{
  "messages": [{"role": "user", "content": "Summarise this in one sentence."}],
  "tier": "tier_fast",
  "api_keys": { "groq": "gsk_...", "openai": "sk-..." },
  "latency_sensitive": false,
  "estimated_output_tokens": 100
}
```
The response's `cost.actual_cost_usd` prices the tokens the provider reported,
with the cache-read rate applied to `cached_input_tokens`. `cost.savings` compares
the provider that answered (after any failover) with the most expensive eligible
provider (`savings.vs_provider_id`), both priced at the actual token counts.
`cost.estimated_savings` is the pre-flight figure for the routed winner.

### The pricing server

A second MCP server, `pricing_server.py` (CheapRouter Pricing, deployed via
`mcpize.pricing.yaml`, default port 8082), serves the verified price table with
provenance. It does no routing and needs no keys. Its 5 tools:

| Tool | Parameters |
|------|------------|
| `pricing_get` | **`provider`**, **`tier`**, `model_id` [null = the tier's model, or all of them] |
| `pricing_list` | `tier` [null = all] |
| `pricing_drift` | `provider` [null = all] |
| `pricing_history` | `provider` [null], `model_id` [null], `field` [input_price_per_1m], `limit` [200], `trajectory_only` [false] |
| `pricing_observed` | `limit` [500] |

## Routing logic

Priority order:
1. **Price** — always the primary signal; every provider's model in the tier is
   sorted cheapest-first (a provider may list several models in a tier). Exact
   cost ties break on latency, then `provider_id`, then `model_id`, independent of
   `prices.json` order
2. **Availability** — providers without an API key are excluded
3. **Latency** — when `latency_sensitive=True`, providers above `LATENCY_THRESHOLD_MS`
   (default 300ms) are excluded; mainly affects DeepSeek and Qwen from LATAM
4. **Region** — optional `allowed_regions` or global `BLOCKED_REGIONS` env var
5. **Health** — with `health_aware` (default), a provider failing in recent history
   moves behind *all* healthy providers, whatever their price. It is never excluded.
Health is scored on the server's global history, shared across every user,
   but only provider-side failures count (429, 5xx, timeouts, connection errors);
   a caller's bad key or bad request never does, each session's say is capped, and
   one session alone cannot mark a provider unhealthy over HTTP. A local stdio
   server has one user, so there one session is enough (`HEALTH_MIN_SESSIONS`
   defaults to 1 over stdio, 2 over HTTP; an explicit value wins).
6. **Failover** — on a transient error (429/5xx/timeout), or when a provider says
   the model is retired or unavailable (404, or a 400 such as Groq's
   `model_decommissioned`), the next (provider, model) entry in that order is
   tried, up to `max_failover`; that can be the same provider's next model. A retired model shows as `"outcome": "model_unavailable"` in
   `routing.attempts` with a `routing.warnings` entry asking the operator to update
   `prices.json`; it does not count against health. Bad keys and other bad requests
   still stop.

A tier is a price bucket of comparable models, not a guarantee they are equal.
With every key supplied, no constraints and no cache, routing on the shipped table
picks Groq openai/gpt-oss-20b (fast), Groq openai/gpt-oss-120b (balanced) and Groq
openai/gpt-oss-120b (powerful) at 1,000 input / 500 output tokens.
`tier_balanced` is an exact cost tie with DeepSeek deepseek-flash (both 0.15/0.60). Exact cost ties break on latency (lower first), then `provider_id`, then `model_id`, never on the order of `prices.json`, so Groq (us, ~180ms) wins over DeepSeek (cn, ~390ms).

Budgets (`arbitrage_set_budget`) are alerts at 80% and 100%; they never block a
request.

## Pricing notes

Prices live in `prices.json` with a `verified_at` date and a `max_age_days` limit
(45). Once the table is older than that, `arbitrage_route_completion` and
`arbitrage_estimate_cost` refuse with `price_table_stale` until a human re-verifies
the prices (`ARBITRAGE_ALLOW_STALE_PRICES=1` downgrades the refusal to a warning).
`python scripts/check_prices.py` reports the age; `arbitrage_check_price_drift`
compares fetchable prices against the table but never edits it.

Prompt caching: `prices.json` models a cache-read rate for Anthropic, OpenAI, Gemini,
Groq, Mistral, DeepSeek, Grok and qwen-max. Only the cache-read rate is modelled, not
cache-write surcharges. qwen-flash and qwen-plus bill `cached_input_tokens` at the full
input rate. OpenAI gpt-6-luna and Anthropic claude-haiku-5-5 have identical prices
(0.10/0.50, cache 0.01), so they tie exactly on every `tier_fast` request. That
matters on cache-heavy requests, where the pair can come first; the tie-break
(latency, then `provider_id`) picks Anthropic.

All eight providers were re-read on 2026-11-14, the table's `verified_at`: six in
SC1e and Groq and Qwen in SC1d (see `prices.json` notes). The table expires on
2026-12-29. The pricing pages each provider is checked against are listed in
`prices.json` under `pricing_urls`.

### Daily drift check (SC7)

`.github/workflows/price-drift.yml` runs once a day (05:37 UTC) and on demand,
out-of-band; nothing on the request path changes. It runs
`scripts/drift_check.py` against the OpenRouter feed, then
`scripts/drift_pr.py` does the GitHub side with `gh`:

- **Significant drift** (exit 10): it opens one PR from `automation/price-drift`
  with labels `price-drift` and `needs-human-verification`, or updates the open
  one. If any drift PR, open, closed or merged, already carries the same
  fingerprint, it does nothing, so closing the PR rejects that drift for good; only
  a different drift (a new feed number, or a table value changed on the default
  branch) opens a new PR.
- **Your commits on the drift branch are safe.** While the branch holds only the
  automation's commit, a newer drift resets and force-pushes it. Once anyone else
  has pushed a commit to it (your `verified_at`/`verified_by` update, or corrected
  numbers), the automation never resets or pushes it again: it posts one comment
  listing the newer drift and asks you to reconcile it by hand (or close the PR so
  the newer drift is proposed afresh).
- **Default branch moved during the run:** it skips that run (the job still
  succeeds) rather than proposing against an old base; the next run uses the new
  one.
- **Drift cleared** (exit 0): it comments once on the open drift PR and leaves it
  open.
- **Table invalid or feed down everywhere** (exit 2): the job fails and no PR is
  opened. The expiry issue below is still handled when the table is valid, so a
  long feed outage does not hide the expiry.
- **Near expiry:** it opens one `price-expiry` issue, "Price table expires on
  <date>", and closes older ones. The body gives the expiry date, not a daily
  countdown, so it changes only when the dates do.

The proposed numbers come from the OpenRouter feed, so they are proxy
provenance, not verified prices. **Before merging, a human must check every number
on the provider's own pricing page (the PR has a checklist with the links) and
update `verified_at` and `verified_by` in the PR.** The automation never changes
those fields, and it never merges, auto-merges or approves.

PRs opened with `GITHUB_TOKEN` don't trigger other workflows, so CI does not run
on the drift PR by itself. To run CI, push a human commit to the branch (the
`verified_at`/`verified_by` update is the natural one), or close and reopen the PR
as a human.

One-time setup: under Settings → Actions → General → Workflow permissions, enable
**"Allow GitHub Actions to create and approve pull requests"**. Without it,
`gh pr create` fails. The workflow requests only `contents`, `pull-requests` and
`issues` write. The checkout does not persist credentials; git reaches GitHub
through `gh auth setup-git`, which reads the job token from `GH_TOKEN` and never
prints it.

To run it manually, go to Actions → "Daily price-drift check" → Run workflow, or
run `gh workflow run price-drift.yml`.

#### Running the check locally

This runs the same check without touching GitHub:

```bash
python scripts/drift_check.py --json-out drift.json --md-out drift.md \
    --write-proposal prices.proposed.json
```

- **Significant** means `|feed - table| / table × 100 ≥ --threshold-pct` (default 5).
  A table price of 0 with a non-zero feed price is always significant. Only
  significant changes go into the proposal.
- Only finite prices above zero count. A feed value such as `"-1"` (OpenRouter's
  variable-price marker), `"0"`, `"inf"`, `"nan"` or `true` is a failed lookup for
  that field, never drift: the provider's other fields are still compared, and a
  provider with nothing usable is listed as fetch-failed.
- **Minor** drifts are below the threshold. They are listed in the report and never
  proposed.
- Manual providers (Groq, Qwen) and providers whose feed lookup failed are listed
  for a manual check and never proposed.
- The proposal is a full `prices.json`, formatted exactly like the original, so a
  diff shows only the changed numbers. It never changes `verified_at` or
  `verified_by`. The feed is a proxy, so check each number on the provider's page
  (`pricing_urls`) and update both fields yourself before merging.
- The report also gives `expires_on` (`verified_at` + `max_age_days`) and sets
  `expiry_warning` when `--expiry-warn-days` (default 10) or fewer days remain.
- Exit codes: 0 no significant drift, 10 significant drift, 2 invalid table or
  the feed failed for every OpenRouter provider, 1 unexpected error.

## Project structure

```
cheaprouter/
├── server.py        — FastMCP router server, its 10 tool definitions
├── pricing_server.py — second MCP server: verified prices with provenance (5 tools)
├── provenance.py    — trust tiers: verified / proxy / observed
├── price_history.py — append-only price trajectory (price_history.jsonl)
├── scripts/         — check_prices.py (staleness), refresh_prices.py, gen_listing.py,
│                      drift_check.py + drift_pr.py (SC7 daily drift check)
├── providers.py     — Provider registry (built from prices.json), regions, BYOK key resolution
├── pricing_table.py — Price-table loader + staleness guard
├── refresh.py       — Price drift check + refresh proposal (S4b)
├── prices.json      — Versioned price table with provenance (verified_at, max_age_days)
├── pricing.py       — Cost calculation and comparison tables
├── router.py        — Routing decision engine
├── client.py        — Async API client (Anthropic / OpenAI-compat / Gemini)
├── history.py       — spend analytics: sessions, spend report, budgets
├── storage.py       — pluggable history backend (Upstash Redis / JSONL)
├── health.py        — provider health tracking, deprioritizes failing providers
├── tokens.py        — input-token counting (tiktoken o200k_base) for cost routing
├── tests/           — pytest suite (no network; see tests/conftest.py)
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
