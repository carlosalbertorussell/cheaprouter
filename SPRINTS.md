# cheaprouter — Sprints

In-place sprint log. Active sprint at top; closed sprints and backlog below.
One task per unit, PARTIAL-honest over DONE-false, closures noted inline.

---

## Active

### S1 — Spend Analytics (Pro tier foundation)

**Goal:** Turn the ephemeral JSONL history into durable, queryable spend analytics —
the foundation of the Pro tier. Persist routing records beyond redeploy and expose
aggregate spend/savings analysis per user.

**Why now:** History currently lives at `/tmp/routing_history.jsonl` and resets on
every MCPize redeploy. Without persistence there is no spend tracking to sell, and
no data to build budget alerts on. This is the load-bearing task for monetization.

**Scope:**
- [ ] S1.1 — Pluggable history backend: abstract `history.py` behind a storage interface (JSONL local / external store cloud) so the `/tmp` reset stops losing data
- [ ] S1.2 — Persistent store integration (Supabase — reuse the pattern from PayBridge/AVM); schema: timestamp, provider, tier, region, token counts, cost, latency, savings — **never keys, never content**
- [ ] S1.3 — Per-session attribution: a lightweight opaque session token so a user can query *their* spend without the server holding identity (BYOK-consistent)
- [ ] S1.4 — New tool `arbitrage_spend_report`: spend by provider, by tier, by day; total saved vs. baseline; date-range filter
- [ ] S1.5 — Budget alerts: `arbitrage_set_budget` + threshold check surfaced in route_completion responses ("you've used 80% of your $X monthly budget")
- [ ] S1.6 — Tests: persistence round-trip, session isolation, budget threshold math, key/content-leak guard on the new store
- [ ] S1.7 — Docs: MCPIZE_DOCS + CHANGELOG; note Pro-tier framing

**Invariants (check before close):**
- **A** — No API keys or message content in the persistent store, verified by test
- **B** — Session tokens are opaque and hold no PII; a user cannot query another's spend
- **C** — Local stdio mode still works with JSONL (no Supabase dependency forced on self-hosters)
- **D** — CHANGELOG updated under [Unreleased]; MCPIZE_DOCS reflects new tools

---

## Backlog

Prioritized. Top of list = next candidate after S1. Each is a sprint-sized unit.

### High value — extends the arbitrage thesis

- **S2 — Automatic failover.** If the cheapest provider returns 429/5xx, retry on the
  next-cheapest eligible provider automatically. Config: max retries, per-provider
  timeout. Turns routing from single-shot into resilient. Natural extension of the
  existing ranked pool — the router already computes the ordering.

- **S3 — Prompt-caching awareness.** Anthropic, OpenAI, and DeepSeek price cached input
  tokens far below fresh tokens. A router blind to caching can pick a nominally cheaper
  provider and actually overpay. Add cache-hit modeling to the cost estimate and a
  `cached_input_tokens` hint on requests. Genuinely differentiating — few routers do this.

- **S4 — Live pricing refresh.** Prices are hardcoded in `providers.py` and go stale.
  Build a refresh path (scheduled job or tool) that pulls current pricing where providers
  expose it programmatically, flags drift for the rest. Removes the biggest maintenance
  burden and keeps routing decisions correct.

- **S5 — Streaming completions.** `arbitrage_route_completion` is blocking. Add a
  streaming variant so real completion workloads get token-by-token output. Biggest
  functional gap for production use.

### Moderate value

- **S6 — Batch API routing.** OpenAI and Anthropic batch endpoints run ~50% cheaper for
  async work. Add a batch tier to routing for latency-tolerant jobs.

- **S7 — Quality/capability scoring.** Tiers are hand-mapped today. Layer a benchmark
  signal per model so routing can weigh capability, not just price — a `min_quality`
  constraint alongside cost.

### Deferred / out of scope

- Multi-modal routing (images, audio) — different cost model, revisit if demand appears
- Fine-tuning / model management — separate product
- Agentic orchestration — that's what clients build *on top of* cheaprouter, not this layer

---

## Closed

*(none yet — S1 is the first tracked sprint; the v1.0.0 build predates this log)*
