# cheaprouter — Sprints

In-place sprint log. Active sprint at top; closed sprints and backlog below.
One task per unit, PARTIAL-honest over DONE-false, closures noted inline.

---

## Active

*(none — S1 shipped; pick the next from Backlog)*

<details>
<summary>S1 — Spend Analytics (Pro tier foundation) — SHIPPED</summary>

### S1 — Spend Analytics (Pro tier foundation)

**Goal:** Turn the ephemeral JSONL history into durable, queryable spend analytics —
the foundation of the Pro tier. Persist routing records beyond redeploy and expose
aggregate spend/savings analysis per user.

**Why now:** History currently lives at `/tmp/routing_history.jsonl` and resets on
every MCPize redeploy. Without persistence there is no spend tracking to sell, and
no data to build budget alerts on. This is the load-bearing task for monetization.

**Scope:**
- [x] S1.1 — Pluggable history backend: abstract `history.py` behind a storage interface (JSONL local / external store cloud) so the `/tmp` reset stops losing data
- [x] S1.2 — Persistent store integration (Upstash Redis over REST); schema: timestamp, provider, tier, region, token counts, cost, latency, savings — **never keys, never content**
- [x] S1.3 — Per-session attribution: a lightweight opaque session token so a user can query *their* spend without the server holding identity (BYOK-consistent)
- [x] S1.4 — New tool `arbitrage_spend_report`: spend by provider, by tier, by day; total saved vs. baseline; date-range filter
- [x] S1.5 — Budget alerts: `arbitrage_set_budget` + threshold check surfaced in route_completion responses ("you've used 80% of your $X monthly budget")
- [x] S1.6 — Tests: persistence round-trip, session isolation, budget threshold math, key/content-leak guard on the new store
- [x] S1.7 — Docs: MCPIZE_DOCS + CHANGELOG; note Pro-tier framing

**Invariants (check before close):**
- **A** — No API keys or message content in the persistent store, verified by test
- **B** — Session tokens are opaque and hold no PII; a user cannot query another's spend
- **C** — Local stdio mode still works with JSONL (no cloud dependency forced on self-hosters)
- **D** — CHANGELOG updated under [Unreleased]; MCPIZE_DOCS reflects new tools

**Closed 2026-09-04** — storage backend abstraction (Upstash/JSONL), opaque session attribution, `arbitrage_spend_report` + `arbitrage_set_budget` tools, 17 new tests. All four invariants verified; response-content preview removed from the store (invariant A fix).

</details>

---

## Backlog

Prioritized. Top of list = next candidate after S1. Each is a sprint-sized unit.

### Catalog & currency — do in this order (each enables the next)

These three are a dependency chain, not independent picks. Building them out of
order is a trap: a bigger catalog (SC3) is a millstone unless currency is
automated (SC2) first, and automation is pointless until the catalog is not
broken (SC1). The one-model-per-tier limit and the manual staleness burden are
the two forces that fight each other; this sequence resolves them.

- **SC1 — Catalog refresh (fix the eight).** The verification pass (2026-09-05,
  see `CATALOG_REFRESH.md`) found the model catalog ~14 months stale: Gemini
  (2.0-flash / 1.5-pro) and DeepSeek (chat / reasoner) model IDs are **retired
  and fail at call time**; Anthropic's Opus tier carries the retired $15/$75 rate
  (Opus 4.6 is $5/$25); Mistral prices are all wrong; OpenAI o3 has a source
  conflict; Groq is fine. **Blocked on Carlos's tier-mapping decisions** (which
  current model → fast/balanced/powerful per provider — see CATALOG_REFRESH.md
  §"Decisions needed"). Once decided: verify each chosen model's current price
  from its provider page (cite it), update prices.json, set verified_at to today,
  PR for review. Clears the S4a staleness guard and gets the deployed server
  routing again. No invented prices, no silently-chosen models. Qwen + Grok still
  need verifying as part of this. **This is the immediate priority — three
  providers are broken right now.**

- **SC2 — Programmatic price source (OpenRouter feed).** Currency is fully manual
  today, which is why a large catalog is dangerous. OpenRouter fronts hundreds of
  models through one OpenAI-compatible endpoint **and publishes machine-readable
  pricing** — exactly the `json_api` source S4b's `refresh.py` was built for but
  had none of. Wire OpenRouter's price feed as a real drift source so
  `arbitrage_check_price_drift` actually detects drift for the models it covers,
  turning S4b from "all manual" into "automated where a feed exists." This is the
  enabler: it makes keeping a catalog current a background check instead of a
  six-weekly hand-verification. Depends on SC1 (fix before automate).

- **SC3 — Multi-model catalog expansion.** Only safe *after* SC2 makes currency
  cheap. Today each tier is one model per provider — you can't express "this
  provider has three good options at different prices" (e.g. Mistral Large now
  undercuts Mistral Medium; DeepSeek V4 Flash beats mid-tier models). Two parts:
  (a) let a provider expose **multiple models per tier** (or fold into S7 capability
  scoring and drop rigid tiers), and (b) add newer models across the current eight
  providers. Comprehensiveness is the payoff, but it only stays honest because SC2
  keeps it current automatically. Depends on SC2. Deliberately NOT about adding
  new providers — that's separate and mostly stays deferred (see below).

### High value — extends the arbitrage thesis




- **S10 — Semantic response cache.** A cache *in front of* routing: if the same or a
  near-identical prompt was answered recently, return the cached response and skip the API
  call entirely — the cheapest token is the one never spent. Largest potential saving of
  anything in the backlog, and the most novel; also the most complex, with correctness and
  staleness trade-offs to design carefully. A sprint on its own. Distinct from S3, which is
  provider-side prompt caching.

### Moderate value

- **S6 — Batch API routing.** OpenAI and Anthropic batch endpoints run ~50% cheaper for
  async work. Add a batch tier to routing for latency-tolerant jobs.

- **S7 — Quality/capability scoring.** Tiers are hand-mapped today. Layer a benchmark
  signal per model so routing can weigh capability, not just price — a `min_quality`
  constraint alongside cost.

- **S11 — Per-request cost ceiling.** A `max_cost_usd` parameter that refuses to route if
  even the cheapest eligible provider would exceed it — a hard guardrail, not just the
  soft budget alert from S1. Pairs naturally with the S1 budget work.

- **S12 — Structured-output / tool-calling passthrough.** Many callers need JSON mode or
  function calling. Routing must account for which providers support it at which tier, or
  it will route to a provider that can't honor the request. Adds a capability constraint
  to the eligibility filter.

- **S13 — Weighted cost-quality routing.** Instead of pure cheapest, a tunable knob:
  "cheapest within X% quality of the best." Depends on S7 quality scoring landing first.

- **S14 — Multi-key rotation per provider.** For heavy users hitting rate limits:
  round-robin across several keys for the same provider. Niche but real; must preserve the
  BYOK no-storage invariant (keys still arrive per request).

### Deferred / out of scope

**Considered providers (evaluated for the token-priced core, declined — reasons on record so they don't resurface):**

- **Manus** — autonomous agent platform, **credit-based** pricing (bundles LLM tokens + VM + browser + third-party calls into one opaque unit). No per-token cost to compare; breaks the cost model, the price table, and the S4a staleness guard. If ever pursued, it's a separate "agent tier" with its own complexity-based cost basis, **not** a token-priced provider. (Declined at project start and again 2026-09-05.)
- **MS Copilot / Azure** — two different things. The **Copilot product** (M365 Copilot, Copilot Studio) is seat-licensed SaaS with no per-token price — can't fit the engine, same class as Manus. **Azure OpenAI / AI Foundry** *is* a real per-token API, but it re-serves models cheaprouter already reaches directly (GPT, Claude) at a compliance premium, with non-BYOK auth (resource endpoints + deployment names + Azure AD). Its value is data residency / BAA / EU tenancy, not price — an **Enterprise-tier gateway feature**, not a cheapest-token catalog entry. Lives with the enterprise gateways below.
- **Hermes Agent** (Nous Research agent framework) — MIT agent framework, BYOM: "you pay whatever the underlying LLM provider charges." It's a **consumer** of routers like cheaprouter, not a provider to add. Manus-class no.
- **Nous Hermes *models*** (Hermes 4 70B/405B etc.) — these DO fit the cost model (per-MTok, OpenAI-compatible, e.g. Hermes 4 70B $0.13/$0.40). But they're cheap open-weight models in the same lane Groq occupies, and their best prices come *through* aggregators (Nebius, OpenRouter). So they're a good **SC3 candidate — the first solid one — reached via the SC2 OpenRouter source**, not a standalone ninth provider to hand-verify. Yes, but later and through the catalog-expansion path, not now.

The through-line: a provider joins the core only if it's **token-priced and adds model or price coverage we don't already have**. Agent/credit platforms fail the first test; gateways and same-lane hosts fail the second.

- **New providers** (distinct from SC3, which expands models *within* the current
  eight). These stay deferred unless a specific need pulls one in:
  - Chinese labs beyond DeepSeek/Qwen — Moonshot (Kimi K2), Zhipu (GLM),
    ByteDance (Doubao). Same deferral as the project's first scoping decision.
  - Enterprise gateways — AWS Bedrock, Azure OpenAI, Google Vertex. Only if the
    Enterprise tier becomes real; then they're a data-residency *feature*, not a
    catalog entry.
  - Other open-model hosts — Together AI, Fireworks. Groq already covers this lane.
  - OpenRouter is the one exception, but as a *pricing source* (SC2), not for
    model coverage — see the Catalog & currency block.
  The value is a curated apples-to-apples set, not maximum coverage: every added
  provider is another price to keep current every 45 days.
- Multi-modal routing (images, audio) — different cost model, revisit if demand appears
- Fine-tuning / model management — separate product
- Agentic orchestration — that's what clients build *on top of* cheaprouter, not this layer

---

## Closed

- **S5 — Streaming completions** (2026-09-05) — route_completion 'stream' option consumes the provider SSE stream (anthropic + openai formats; gemini falls back). Streaming client layer (stream_provider/stream_supported). Reports time_to_first_token_ms. Failover is pre-first-token only — a mid-stream error is terminal (tagged _cheaprouter_stream_started), never retried, since partial output may be committed. MCP returns one final result; this is server-side streaming from the provider, not an MCP-level token stream. 7 tests.
- **S3 — Prompt-caching awareness** (2026-09-05) — cost model prices cache-hit input tokens at each model's cache-read rate; cached_input_tokens hint on get_pricing/estimate_cost/route_completion. cached_input_price_per_1m in the table for anthropic/openai/deepseek; others bill cache tokens at full input rate (never advantaged). Can flip the winner on cache-heavy loads (proven: DeepSeek beats Groq at tier_fast under heavy caching). Estimates report cache_supported. Inherits S4a staleness. 10 tests.
- **S4b — Price refresh path** (2026-09-05) — refresh.py + arbitrage_check_price_drift compare fetchable prices to the table and report drift (match/drift/fetch_failed/manual); propose_updated_table() builds a review candidate but never writes prices.json or advances verified_at. Per-provider 'refresh' strategy in prices.json (json_api|manual); all manual today (no verified programmatic source — no invented prices). scripts/refresh_prices.py for CI/cron. 9 tests. Clears the S4a guard via reviewed diff, not blind editing.
- **S4a — Price-table staleness guard** (2026-09-05) — prices moved from hardcoded literals to a versioned prices.json with provenance; pricing_table.py loads/validates (fails loudly, no silent default) and exposes verified_at / age_days / is_stale. Routing + estimation REFUSE when stale (older than max_age_days=45); get_pricing + provider_status warn but still return. ARBITRAGE_ALLOW_STALE_PRICES=1 override. scripts/check_prices.py wired into CI (non-blocking price-freshness job + weekly cron). Every response carries a price_table block. Protects S1's savings figure. 20 tests. NOTE: shipped table is ~432 days old, so the deployed server refuses routing until prices are re-verified — intended, flagged in the PR.
- **S2 — Automatic failover** (2026-09-05) — on a transient error (429/5xx/timeout) route_completion retries the next provider in ranked order, up to max_failover (default 2). Non-transient errors (bad key/request) never fail over. Each failed attempt feeds S8 health. Response reports failed_over + attempts. router exposes ranked_pool; client has is_transient_error. 18 tests. Completes the health/failover pair with S8.
- **S8 — Provider health tracking** (2026-09-05) — recent failures deprioritize a provider in routing (behind healthy providers of similar price, never excluded; an all-unhealthy pool still returns the cheapest). New `arbitrage_provider_health` tool; `route_completion` gains a `health_aware` toggle and reports `deprioritized_providers`. health.py reads history metadata only. 10 tests. Precursor to S2 failover.
- **S1 — Spend Analytics** (2026-09-04) — durable session-scoped spend tracking, spend report + budget tools, pluggable Upstash/JSONL backend. Foundation of the Pro tier. Full detail in the collapsed block under Active.
