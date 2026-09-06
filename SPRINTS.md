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

### High value — extends the arbitrage thesis



- **S5 — Streaming completions.** `arbitrage_route_completion` is blocking. Add a
  streaming variant so real completion workloads get token-by-token output. Biggest
  functional gap for production use.

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

- Multi-modal routing (images, audio) — different cost model, revisit if demand appears
- Fine-tuning / model management — separate product
- Agentic orchestration — that's what clients build *on top of* cheaprouter, not this layer

---

## Closed

- **S3 — Prompt-caching awareness** (2026-09-05) — cost model prices cache-hit input tokens at each model's cache-read rate; cached_input_tokens hint on get_pricing/estimate_cost/route_completion. cached_input_price_per_1m in the table for anthropic/openai/deepseek; others bill cache tokens at full input rate (never advantaged). Can flip the winner on cache-heavy loads (proven: DeepSeek beats Groq at tier_fast under heavy caching). Estimates report cache_supported. Inherits S4a staleness. 10 tests.
- **S4b — Price refresh path** (2026-09-05) — refresh.py + arbitrage_check_price_drift compare fetchable prices to the table and report drift (match/drift/fetch_failed/manual); propose_updated_table() builds a review candidate but never writes prices.json or advances verified_at. Per-provider 'refresh' strategy in prices.json (json_api|manual); all manual today (no verified programmatic source — no invented prices). scripts/refresh_prices.py for CI/cron. 9 tests. Clears the S4a guard via reviewed diff, not blind editing.
- **S4a — Price-table staleness guard** (2026-09-05) — prices moved from hardcoded literals to a versioned prices.json with provenance; pricing_table.py loads/validates (fails loudly, no silent default) and exposes verified_at / age_days / is_stale. Routing + estimation REFUSE when stale (older than max_age_days=45); get_pricing + provider_status warn but still return. ARBITRAGE_ALLOW_STALE_PRICES=1 override. scripts/check_prices.py wired into CI (non-blocking price-freshness job + weekly cron). Every response carries a price_table block. Protects S1's savings figure. 20 tests. NOTE: shipped table is ~432 days old, so the deployed server refuses routing until prices are re-verified — intended, flagged in the PR.
- **S2 — Automatic failover** (2026-09-05) — on a transient error (429/5xx/timeout) route_completion retries the next provider in ranked order, up to max_failover (default 2). Non-transient errors (bad key/request) never fail over. Each failed attempt feeds S8 health. Response reports failed_over + attempts. router exposes ranked_pool; client has is_transient_error. 18 tests. Completes the health/failover pair with S8.
- **S8 — Provider health tracking** (2026-09-05) — recent failures deprioritize a provider in routing (behind healthy providers of similar price, never excluded; an all-unhealthy pool still returns the cheapest). New `arbitrage_provider_health` tool; `route_completion` gains a `health_aware` toggle and reports `deprioritized_providers`. health.py reads history metadata only. 10 tests. Precursor to S2 failover.
- **S1 — Spend Analytics** (2026-09-04) — durable session-scoped spend tracking, spend report + budget tools, pluggable Upstash/JSONL backend. Foundation of the Pro tier. Full detail in the collapsed block under Active.
