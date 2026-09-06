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

- **SC3 — Multi-model catalog expansion.** Only safe *after* SC2 makes currency
  cheap. Today each tier is one model per provider — you can't express "this
  provider has three good options at different prices" (e.g. Mistral Large now
  undercuts Mistral Medium; DeepSeek V4 Flash beats mid-tier models). Two parts:
  (a) let a provider expose **multiple models per tier** (or fold into S7 capability
  scoring and drop rigid tiers), and (b) add newer models across the current eight
  providers. Comprehensiveness is the payoff, but it only stays honest because SC2
  keeps it current automatically. Depends on SC2. Deliberately NOT about adding
  new providers — that's separate and mostly stays deferred (see below). **KPI-coverage driver (2026-09, see PRICING_STRATEGY.md):** if the pricing data is positioned as financial-KPI intelligence, SC3's target list explicitly adds the financially-material players — major labs' full lineups and the cloud gateways (Azure OpenAI / AWS Bedrock / Google Vertex, in scope here as KPI-coverage targets even though deferred as routing targets) — because a KPI series that omits them is incomplete as market intelligence. Rationale shifts from 'more arbitrage' to 'citable coverage'; timing unchanged (still behind SC2).

- **SC4 — Variable / jurisdictional pricing schema.** The one-price-per-model
  schema flattens away real, structured price variation that the arbitrage engine
  should be *using*, not ignoring. Three axes found during the SC1 verification
  (2026-09-05), each large enough to flip a routing decision:
    - **Time-of-day** — DeepSeek bills peak (01:00–04:00, 06:00–10:00 UTC) at ~2x
      off-peak. SC1 encodes off-peak (the conservative, typical case) and notes it;
      SC4 would let routing send cost-tolerant work to DeepSeek off-peak.
    - **Region / jurisdiction** — same model, different price by endpoint. Qwen
      prices differ across Singapore / Beijing / Tokyo / Frankfurt / Virginia;
      OpenAI adds a **10% data-residency uplift** on regional endpoints for models
      released on/after 2026-03-05. This is the jurisdictional axis: a request's
      required data residency changes which provider is actually cheapest.
    - **Long-context tier** — Gemini, Anthropic, Grok, and OpenAI's flagships all
      apply a higher meter once a prompt crosses ~200K tokens (often ~2x). SC1
      encodes the standard-context rate; SC4 would apply the cliff by request size.
  **Threshold-gated:** model a variation only when it's large enough to change a
  routing decision (DeepSeek's 2x qualifies; a 5% regional wobble does not) — else
  we rebuild the sprawl-vs-currency problem the whole chain is disciplined against.
  Touches the prices.json schema and CostEstimate.compute, so it composes with S3
  caching and S1 savings. **Best sequenced after SC2** — a programmatic feed that
  already reports these variations tells us which are worth modelling, rather than
  hand-encoding time-of-day/region tables that then go stale. Depends on SC2;
  natural sibling of SC3.

- **SC5 — Caller price overrides (BYO cheaper price).** BYOK already lets a caller
  bring their own *key*; SC5 lets them bring their own *price*. An optional
  per-request `price_overrides` map (`{provider: {input_price_per_1m, output_price_per_1m,
  cached_input_price_per_1m}}`) replaces the table's list price **for that request's
  cost ranking only**, when the caller has a better rate (negotiated/enterprise,
  promo, spot/aggregator discount, credits). Serves the arbitrage thesis directly:
  if you've negotiated DeepSeek to half list, cheaprouter should route to it more
  aggressively because for *you* it's even cheaper. Caller-supplied, per-request,
  never stored (same as keys). Response flags which providers used a caller price
  vs the table.
  **Staleness-guard interaction (the subtle part):** an overridden provider's price
  is self-certified-current, so it doesn't need the table. Rule: override providers
  bypass the S4a guard *for themselves*; if **all** eligible providers are overridden
  the request routes even on a stale table (the table isn't used); but a **mixed**
  request (some overridden, some relying on the table) still refuses when stale —
  comparing fresh caller-prices against stale table-prices is exactly the
  indefensible mix the guard exists to prevent.

- **SC6 — Data-residency constraints (declared, not detected).** A first-class
  `data_residency` constraint (e.g. `"eu"`) that hard-filters the eligible pool to
  providers whose jurisdiction satisfies it, and **refuses** (like the stale-price
  guard) if none qualify — never silently falling back to a non-compliant provider.
  Stronger and clearer than the incidental `allowed_regions`/`region` tag: it reads
  as "these tokens must only go to <jurisdiction> providers." Response shows each
  candidate's jurisdiction so the decision is auditable.
  **Honest boundary — load-bearing, must be documented plainly:** cheaprouter
  **enforces the constraint the caller declares; it does NOT detect personal data
  and does NOT constitute legal compliance.** It cannot inspect content to know a
  request contains PII — by design, the privacy model never touches message
  content. The caller (who knows their data) declares the constraint; cheaprouter
  honours it or refuses. Anything claiming auto-PII-detection would either break the
  privacy invariant or be compliance theater. Bridges toward the Enterprise-tier
  gateways (Azure/Bedrock/Vertex regional endpoints give the legally-adequate
  residency guarantees a raw provider API doesn't). Relates to SC4 (jurisdictional
  pricing).

- **SC7 — Freshness SLA / auto-refresh loop.** The "best execution" question:
  guarantee routing is always against near-current prices *without* per-call
  polling. Per-call polling is the wrong mechanism and is explicitly rejected: it
  adds hundreds of ms–seconds of blocking I/O to the hot path (undoing S5's TTFT
  work) to catch a change that, for LLM pricing, happens on the order of **weeks,
  not seconds** — LLM API prices are near-static between infrequent discrete
  repricing events, so real-time polling fetches identical numbers thousands of
  times for almost no benefit, at high latency/rate-limit/provenance cost. It also
  replaces verified, auditable prices (the whole S4a/SC1 basis of a defensible
  savings figure) with unprovenanced live scrapes — *worse* execution, not better.
  The correct design is a **freshness contract**, most of which already exists:
  route only on known-current verified prices (SC1), refuse rather than route on
  stale ones (S4a guard), check drift out-of-band (SC2), let callers inject a live
  price per request (SC5). SC7 tightens the one loose loop: today drift-checking is
  on-demand/weekly and doesn't close the loop (SC2 proposes, never auto-writes, to
  keep provenance). SC7 adds a **scheduled daily drift check that auto-surfaces
  significant drift** — opens a PR with the proposed update (human still merges, so
  provenance holds), or, if feed-provenance is explicitly accepted for a provider,
  fast-tracks it — so a real price change is caught within a day, not whenever
  someone remembers. Bounded, threshold-gated (only significant drift), zero hot-path
  latency. **Optional cross-check:** a second automated proxy (e.g. a Price-Per-Token
  MCP) to flag where OpenRouter's routed price diverges from provider-direct — two
  disagreeing proxies are more informative than one (cf. the o3 $2/$8-vs-$10/$40
  conflict that correctly triggered manual verification). Depends on SC2.

- **SC8 — Autonomous verification agent.** The piece that makes tending the price
  data *autonomous* — turning "will I keep this current for years?" from a grind
  into a background process with the human as light-touch overseer. It is the
  automatable version of the manual verification pass run on 2026-09-05 (search
  provider pages, cross-check, flag conflicts, propose updates), turned into a
  scheduled agent.

  **Where it sits (settled): GitHub Actions, NOT inside the MCP servers.** The
  router/pricing servers are stateless request-response processes that hold
  nothing and do one thing; an always-on agent embedded in them would break that
  model and need write-back credentials. The agent is a *separate* scheduled
  process — the servers *serve* the data, the agent *tends* it. GitHub Actions is
  the right home because: (a) it's already where SC7's schedule lives; (b) its
  output is "open a PR," landing changes exactly where human approval already sits
  (the merge) — no new trust surface; (c) **git is the audit log** — every
  proposed change is a commit + diff + timestamp + cited source in the PR body,
  the immutable provenance trail the certified thesis needs, for free; (d) it
  reads *public* pricing pages, so it needs **no provider keys** — zero
  secret-handling risk; (e) zero infrastructure/uptime/bill. Scheduled (daily),
  not real-time — correct, because prices move weekly (SC7).

  **What it does (autonomous half):** on a daily cron — run the SC2 drift check
  (OpenRouter feed), fetch the relevant provider pricing pages, verify each
  flagged change against the page, append confirmed movements to `price_history`,
  and **open a PR** proposing the update with the source cited. `scripts/
  verify_agent.py` reuses `refresh.py`'s drift machinery + page reads;
  `.github/workflows/verify-prices.yml` schedules it.

  **The auto-verify-vs-escalate rule (the crux):** the agent classifies each
  finding.
    - **auto-verify** — the OpenRouter feed and the provider page *agree* on a
      changed number → PR labelled `price-update:auto-verified`, cited, a quick
      human merge.
    - **escalate** — sources *conflict* (o3 case), a model ID is *retired* (needs
      a replacement judgment), or a provider does something *structurally new*
      (DeepSeek-style peak/off-peak → methodology work) → PR labelled
      `price-update:needs-judgment`, describing the ambiguity, NOT a number to
      rubber-stamp. Optionally a human invokes an on-demand agent session
      (Cowork/Claude Code) to reason through it — the smart part, human-triggered.

  **Hard safeguards (keep "no invented prices" intact even with an agent in the
  loop):**
    1. The agent **proposes via PR, never writes verified prices directly.** The
       `verified` tier only advances on a **human merge** — the agent can propose
       all day but can never *certify*.
    2. A stale-but-safe number beats an auto-scraped wrong one — on any doubt
       (page format changed, extraction uncertain, sources disagree) it
       **escalates, never guesses**.
    3. No provider keys, no paid calls — reads public pages only.
    4. It runs on the `verified`/`proxy` provenance tiers only; it never touches
       the `observed` tier or any content/keys (nothing to touch — it's a
       separate process from routing).

  **Why this matters strategically:** the automation boundary (autonomous
  mechanical tending vs. irreducible human judgment) is the *same line* as
  free-tier-vs-certified and automatable-vs-attestation. The agent does the ~95%
  grind (gather/verify/propose); the human does the ~5% that *is* the value
  (**95/5 is ESTIMATED, not measured — CR-05; measure it once SC8 runs**)
  (vouch, resolve ambiguity, own the methodology) — which is exactly the
  high-leverage senior work, not the drudgery. This is what makes the
  first-mover-via-trust thesis viable for a high-opportunity-cost operator: the
  reference tends itself and calls a human only for judgment. Depends on SC2
  (drift machinery) and pairs with SC7 (the schedule it runs on); SP1b's
  `price_history` is where confirmed movements accrue. Needs `workflow` scope to
  land the Action.

  **Honest limit:** autonomous ≠ unattended. The agent will sometimes misread a
  page; escalations still need a human; and "who owns the methodology" is a real
  (if light) ongoing responsibility. It converts a daily data-ops job into a
  light-touch oversight role — not into zero work.

**Note — no single source of pricing truth exists, by design of the market.**
Pricing ground-truth is federated: it lives on 8 provider pages, in 8 formats,
changing on 8 schedules. There is no consolidated tape for LLM tokens. OpenRouter
(SC2) is the best unified *proxy* but is not ground truth (it prices its own
routed rate, and can't see DeepSeek peak/off-peak, Qwen regions, or context
cliffs — SC4's remit). Aggregator/tracker sites are secondary and occasionally
conflict. The chosen architecture is therefore correct for a federated market:
a fast automated proxy (SC2) for breadth + per-provider manual verification for
authority and the proxy's blind spots + caller overrides (SC5) for live prices.

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

- **SC2 — Programmatic price source (OpenRouter feed)** (2026-09-05) — refresh.py drift-checks 6 providers (Anthropic/OpenAI/Gemini/Mistral/DeepSeek/Grok) against OpenRouter's public /api/v1/models feed; one shared fetch, per-token->per-1M conversion, cache-read compared. Groq+Qwen stay manual (documented). Missing IDs / feed outage -> fetch_failed, never bad data. Currency now automated for most of the catalog instead of a 45-day manual chore. 8 tests (mocked feed; sandbox blocks openrouter.ai, runs live on MCPize).
- **SC1 — Catalog refresh** (2026-09-05) — verified all 8 providers against current pricing; the table is current again so the deployed server routes instead of refusing. Replaced 4 providers' retired model IDs (Gemini, DeepSeek, Grok, Qwen) that would fail at call time; corrected all stale prices (Anthropic Opus $15/$75→$5/$25, all Mistral, o3 $10/$40→$2/$8). Cheapest-current-per-tier rule; Mistral Medium dropped (Large now cheaper). DeepSeek off-peak, Qwen Singapore, standard context — variance noted for SC4. verified_at=2026-09-05, guard now fresh. 6 stale-premise tests updated (shipped table is now fresh, not stale); Gemini gained verified cache pricing. 130 tests pass.
- **S5 — Streaming completions** (2026-09-05) — route_completion 'stream' option consumes the provider SSE stream (anthropic + openai formats; gemini falls back). Streaming client layer (stream_provider/stream_supported). Reports time_to_first_token_ms. Failover is pre-first-token only — a mid-stream error is terminal (tagged _cheaprouter_stream_started), never retried, since partial output may be committed. MCP returns one final result; this is server-side streaming from the provider, not an MCP-level token stream. 7 tests.
- **S3 — Prompt-caching awareness** (2026-09-05) — cost model prices cache-hit input tokens at each model's cache-read rate; cached_input_tokens hint on get_pricing/estimate_cost/route_completion. cached_input_price_per_1m in the table for anthropic/openai/deepseek; others bill cache tokens at full input rate (never advantaged). Can flip the winner on cache-heavy loads (proven: DeepSeek beats Groq at tier_fast under heavy caching). Estimates report cache_supported. Inherits S4a staleness. 10 tests.
- **S4b — Price refresh path** (2026-09-05) — refresh.py + arbitrage_check_price_drift compare fetchable prices to the table and report drift (match/drift/fetch_failed/manual); propose_updated_table() builds a review candidate but never writes prices.json or advances verified_at. Per-provider 'refresh' strategy in prices.json (json_api|manual); all manual today (no verified programmatic source — no invented prices). scripts/refresh_prices.py for CI/cron. 9 tests. Clears the S4a guard via reviewed diff, not blind editing.
- **S4a — Price-table staleness guard** (2026-09-05) — prices moved from hardcoded literals to a versioned prices.json with provenance; pricing_table.py loads/validates (fails loudly, no silent default) and exposes verified_at / age_days / is_stale. Routing + estimation REFUSE when stale (older than max_age_days=45); get_pricing + provider_status warn but still return. ARBITRAGE_ALLOW_STALE_PRICES=1 override. scripts/check_prices.py wired into CI (non-blocking price-freshness job + weekly cron). Every response carries a price_table block. Protects S1's savings figure. 20 tests. NOTE: shipped table is ~432 days old, so the deployed server refuses routing until prices are re-verified — intended, flagged in the PR.
- **S2 — Automatic failover** (2026-09-05) — on a transient error (429/5xx/timeout) route_completion retries the next provider in ranked order, up to max_failover (default 2). Non-transient errors (bad key/request) never fail over. Each failed attempt feeds S8 health. Response reports failed_over + attempts. router exposes ranked_pool; client has is_transient_error. 18 tests. Completes the health/failover pair with S8.
- **S8 — Provider health tracking** (2026-09-05) — recent failures deprioritize a provider in routing (behind healthy providers of similar price, never excluded; an all-unhealthy pool still returns the cheapest). New `arbitrage_provider_health` tool; `route_completion` gains a `health_aware` toggle and reports `deprioritized_providers`. health.py reads history metadata only. 10 tests. Precursor to S2 failover.
- **S1 — Spend Analytics** (2026-09-04) — durable session-scoped spend tracking, spend report + budget tools, pluggable Upstash/JSONL backend. Foundation of the Pro tier. Full detail in the collapsed block under Active.
