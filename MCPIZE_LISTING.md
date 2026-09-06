# MCPize listing — paste-ready copy & redeploy checklist

Everything needed to bring the **live MCPize server** in line with the repo. The
repo is the source of truth; this file is the paste-ready extract for the parts
that are edited in the MCPize dashboard (which can't be updated from the repo or
by an agent — they need your MCPize login).

_Generated from `prices.json` (verified 2026-09-05) and `mcpize.yaml` (v1.1.0)._

---

## 1. Redeploy (picks up repo-sourced docs, config & prices)

The deployed server serves what's in the repo — `mcpize.yaml`, `prices.json`,
tool docstrings. PR #43 already corrected all of these on `main`. To push them
live, **redeploy** (your action; needs MCPize auth):

```bash
mcpize deploy          # from the repo, or trigger from the MCPize dashboard
```

After redeploy, verify: `arbitrage_provider_status` should list **8** providers,
and `arbitrage_get_pricing` should show the current models (e.g. gemini-3.5-flash,
deepseek-v4-flash — not the old retired IDs).

## 2. Listing description (paste into the MCPize dashboard)

**Short (one-liner):**
> BYOK router that sends each LLM call to the cheapest capable provider across 8 providers — cache-aware, health-aware, with failover, streaming, and spend analytics.

**Long:**
> **cheaprouter** routes every LLM completion to the cheapest model that clears
> the task's capability tier, across **8 providers** — Anthropic, OpenAI, Google
> Gemini, Groq, Mistral, DeepSeek, Alibaba Qwen, and xAI Grok.
>
> **BYOK — bring your own keys.** You pass your provider API keys per request;
> the server stores no keys and no message content. The routing intelligence is
> the value.
>
> **What it does beyond "cheapest":**
> - **Prompt-caching-aware costing** — accounts for cache-read rates, so a
>   cache-heavy request routes to the truly cheapest provider (not the cheapest
>   headline price).
> - **Accurate token counting** (tiktoken) — the cost ranking rests on real token
>   volume, not a char/4 guess.
> - **Provider health tracking + automatic failover** — a failing provider is
>   deprioritised, and transient errors retry the next-cheapest automatically.
> - **Streaming** — optional token streaming for lower time-to-first-token.
> - **Spend analytics + budgets** — durable per-session spend tracking and
>   budget alerts.
> - **Staleness-guarded prices** — prices are versioned and dated; the router
>   *refuses to route on stale prices* rather than misprice.
>
> Free. Open source (MIT). No vendor lock-in.

**Tags:** `llm` `routing` `cost-optimization` `byok` `arbitrage` `openai`
`anthropic` `gemini` `deepseek` `mcp`

## 3. Current verified pricing (for the listing / a pricing tab)

Prices are USD per 1M tokens, verified 2026-09-05. Cache = cache-read rate where
the provider offers it.

| Provider | Tier | Model | Input | Output | Cache |
|----------|------|-------|------:|-------:|------:|
| Anthropic | fast | claude-haiku-4-5 | 1.00 | 5.00 | 0.10 |
| Anthropic | balanced | claude-sonnet-4-6 | 3.00 | 15.00 | 0.30 |
| Anthropic | powerful | claude-opus-4-6 | 5.00 | 25.00 | 0.50 |
| OpenAI | fast | gpt-4o-mini | 0.15 | 0.60 | 0.075 |
| OpenAI | balanced | gpt-4o | 2.50 | 10.00 | 1.25 |
| OpenAI | powerful | o3 | 2.00 | 8.00 | 0.20 |
| Gemini | fast | gemini-3.5-flash | 0.30 | 2.50 | 0.03 |
| Gemini | balanced | gemini-3.1-flash-lite | 0.25 | 1.50 | 0.025 |
| Gemini | powerful | gemini-3.1-pro | 2.00 | 12.00 | 0.20 |
| Groq | fast | llama-3.1-8b-instant | 0.05 | 0.08 | — |
| Groq | balanced/powerful | llama-3.3-70b-versatile | 0.59 | 0.79 | — |
| Mistral | fast | mistral-small-latest | 0.15 | 0.60 | — |
| Mistral | balanced/powerful | mistral-large-latest | 0.50 | 1.50 | — |
| DeepSeek | fast/balanced | deepseek-v4-flash (off-peak) | 0.22 | 0.66 | 0.007 |
| DeepSeek | powerful | deepseek-v4-pro (off-peak) | 0.66 | 1.98 | 0.022 |
| Qwen | fast | qwen-flash | 0.10 | 0.40 | — |
| Qwen | balanced | qwen-plus | 0.40 | 2.40 | — |
| Qwen | powerful | qwen-max | 2.00 | 6.00 | 0.25 |
| Grok | fast | grok-4.1-fast | 0.20 | 0.50 | — |
| Grok | balanced | grok-4.3 | 1.25 | 2.50 | 0.20 |
| Grok | powerful | grok-4.6 | 2.00 | 6.00 | 0.50 |

Notes: DeepSeek encoded at off-peak (peak ~2x); Qwen at the Singapore endpoint;
all at standard context. See `prices.json` notes for the variance. These are the
provider *list* prices cheaprouter routes on — your negotiated rates may differ.

## 4. What an agent can't do here (why this is a checklist, not an automated update)

Updating the *live* MCPize server needs your MCPize login: an agent can't reach
MCPize (egress-blocked) and holds no MCPize credentials. What's automatable is
already done — the repo (docs, config, prices) is correct on `main`. The manual
steps are: **redeploy** (§1) and **paste the listing copy** (§2–3) into the
dashboard.
