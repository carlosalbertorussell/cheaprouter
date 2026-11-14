# MCPize listing — paste-ready copy & redeploy checklist

Everything needed to bring the **live MCPize server** in line with the repo. The
repo is the source of truth; this file is the paste-ready extract for the parts
that are edited in the MCPize dashboard (which can't be updated from the repo or
by an agent — they need your MCPize login).

_Section 3 is generated from `prices.json` by `scripts/gen_listing.py`; a test fails if it drifts. Version per `mcpize.yaml` (v1.1.0)._

---

## 1. Redeploy (picks up repo-sourced docs, config & prices)

The deployed server serves what's in the repo — `mcpize.yaml`, `prices.json`,
tool docstrings. PR #43 already corrected all of these on `main`. To push them
live, **redeploy** (your action; needs MCPize auth):

```bash
mcpize deploy          # from the repo, or trigger from the MCPize dashboard
```

After redeploy, verify: `arbitrage_provider_status` should list **8** providers,
and `arbitrage_get_pricing` should show the current models from §3 (e.g.
claude-haiku-5-5, gpt-6-luna, deepseek-flash — not retired IDs).

## 2. Listing description (paste into the MCPize dashboard)

**Short (one-liner):**
> BYOK router that sends each LLM call to the cheapest provider in the capability tier you pick, across 8 providers — cache-aware, health-aware, with failover, streaming, and spend analytics.

**Long:**
> **cheaprouter** routes every LLM completion to the cheapest provider you hold a
> key for within the capability tier you pick (fast, balanced or powerful),
> across **8 providers** — Anthropic, OpenAI, Google Gemini, Groq, Mistral,
> DeepSeek, Alibaba Qwen, and xAI Grok. A tier is a price bucket of comparable
> models, not a quality guarantee: pick the tier your task needs, and exclude
> providers you don't want.
>
> **BYOK — bring your own keys.** You pass your provider API keys per request;
> the server stores no keys and no message content, and a hosted server never
> falls back to the operator's own keys.
>
> **What it does beyond "cheapest":**
> - **Prompt-caching-aware costing** — accounts for cache-read rates, so a
>   cache-heavy request routes to the truly cheapest provider (not the cheapest
>   headline price).
> - **Token counting** (tiktoken `o200k_base`, one encoder for every provider) —
>   the cost ranking rests on a real token count, not a char/4 guess.
> - **Provider health tracking + automatic failover** — a provider failing in
>   recent routing history (shared across all users of a server) moves behind
>   every healthy one, and transient errors retry the next provider in order.
> - **Streaming** — optional token streaming for lower time-to-first-token.
> - **Spend analytics + budget alerts** — per-session spend tracking (durable
>   with Upstash) and alerts at 80% and 100% of a monthly budget. Budgets alert;
>   they do not block requests.
> - **Staleness-guarded prices** — prices are versioned and dated; the router
>   *refuses to route on stale prices* rather than misprice.
>
> Free. Open source (MIT). No vendor lock-in.

**Tags:** `llm` `routing` `cost-optimization` `byok` `arbitrage` `openai`
`anthropic` `gemini` `deepseek` `mcp`

## 3. Current verified pricing (for the listing / a pricing tab)

<!-- BEGIN GENERATED: prices (scripts/gen_listing.py) -->
Prices are USD per 1M tokens from `prices.json` (verified_at 2026-11-14, max_age_days 45). Cache = the cache-read rate cheaprouter models; — means cached tokens are billed at the full input rate.

| Provider | Tier | Model | Input | Output | Cache | Status |
|----------|------|-------|------:|-------:|------:|--------|
| Anthropic | fast | claude-haiku-5-5 | 0.10 | 0.50 | 0.01 | re-read |
| Anthropic | balanced | claude-sonnet-5-5 | 2.00 | 10.00 | 0.10 | re-read |
| Anthropic | powerful | claude-opus-5-5 | 4.00 | 20.00 | 0.20 | re-read |
| OpenAI | fast | gpt-6-luna | 0.10 | 0.50 | 0.01 | re-read |
| OpenAI | balanced | gpt-6.1-terra | 1.00 | 5.00 | 0.05 | re-read |
| OpenAI | powerful | gpt-6.1-sol | 2.00 | 10.00 | 0.10 | re-read |
| Google Gemini | fast/balanced | gemini-3.1-flash-lite | 0.25 | 1.50 | 0.025 | re-read |
| Google Gemini | powerful | gemini-3.1-pro-preview | 2.00 | 12.00 | 0.20 | re-read |
| Groq | fast | openai/gpt-oss-20b | 0.075 | 0.30 | 0.037 | re-read |
| Groq | balanced/powerful | openai/gpt-oss-120b | 0.15 | 0.60 | 0.075 | re-read |
| Mistral AI | fast | mistral-small-latest | 0.15 | 0.60 | 0.015 | re-read |
| Mistral AI | balanced/powerful | mistral-large-latest | 0.70 | 2.10 | 0.07 | re-read |
| DeepSeek | fast/balanced | deepseek-flash | 0.15 | 0.60 | 0.003 | re-read |
| DeepSeek | powerful | deepseek-v4-pro | 0.66 | 1.98 | 0.022 | re-read |
| Alibaba Qwen | fast | qwen-flash | 0.05 | 0.40 | — | re-read |
| Alibaba Qwen | balanced | qwen-plus | 0.40 | 1.20 | — | re-read |
| Alibaba Qwen | powerful | qwen-max | 1.60 | 6.40 | 0.32 | re-read |
| xAI Grok | fast/balanced | grok-4.3 | 1.25 | 2.50 | 0.20 | re-read |
| xAI Grok | powerful | grok-4.7 | 2.00 | 6.00 | 0.50 | re-read |

These are list prices at standard context. DeepSeek is encoded at off-peak rates (peak is about 2x), Qwen at the Singapore endpoint with qwen-plus at the non-thinking rate. See the `notes` in `prices.json` for every caveat. Your negotiated rates may differ.

With every key supplied, no constraints and no cache, routing picks **Groq openai/gpt-oss-20b** for fast, **Groq openai/gpt-oss-120b** for balanced and **Groq openai/gpt-oss-120b** for powerful (1,000 input / 500 output tokens). Other volumes, cache hits, `latency_sensitive` or excluded providers can change the winner; `arbitrage_estimate_cost` shows it for your request.
<!-- END GENERATED: prices -->

## 4. What an agent can't do here (why this is a checklist, not an automated update)

Updating the *live* MCPize server needs your MCPize login: an agent can't reach
MCPize (egress-blocked) and holds no MCPize credentials. What's automatable is
already done — the repo (docs, config, prices) is correct on `main`. The manual
steps are: **redeploy** (§1) and **paste the listing copy** (§2–3) into the
dashboard.
