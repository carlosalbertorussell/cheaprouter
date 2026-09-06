# cheaprouter — Forward Pipeline

One-page map of what's shipped, what's next, and in what order. `SPRINTS.md` is
the live per-sprint log (scope, closures, test counts) and remains the source of
truth for detail; this document is the narrative overview and sequencing rationale.

_Last updated: 2026-09-05._

## The one thing gating everything

**The deployed server refuses to route until prices are re-verified.** The S4a
staleness guard is live and the price table is ~432 days old, so on deploy
`arbitrage_route_completion` and `arbitrage_estimate_cost` refuse. Every feature
below is built or buildable, but the live server can't exercise any of it until
**SC1** lands. SC1 is blocked only on Carlos's tier-mapping decisions
(`CATALOG_REFRESH.md`). This is the highest-leverage action in the whole pipeline.

## Shipped (foundation — all live on main)

Eight sprints, 2026-09-04/05. The router is resilient, observable, and its cost
model is honest:

| Sprint | What it gave the product |
|--------|--------------------------|
| S1 | Durable session-scoped spend analytics (Upstash) — Pro-tier foundation |
| S8 | Provider health tracking — failing providers deprioritized, never stranded |
| S2 | Automatic failover — transient errors retry the next ranked provider |
| S9 | Accurate token counting (tiktoken) — cost ranking rests on real volume |
| S4a | Price-table staleness guard — refuses to route on prices it can't defend |
| S4b | Price refresh path — drift check that proposes, never invents |
| S3 | Prompt-caching awareness — cache-heavy loads route to the true cheapest |
| S5 | Streaming completions — lower TTFT, failover-safe (pre-first-token only) |

Cross-cutting properties held throughout: BYOK no-storage, MIT open source,
CI-protected (tests + non-blocking price-freshness), all deps ceiling-pinned.

## Next: Catalog & currency (SC1 → SC2 → SC3, strict order)

A dependency chain, not independent picks. Building out of order is the trap:
comprehensiveness is a millstone unless currency is automated, and automation is
pointless until the catalog isn't broken.

1. **SC1 — Catalog refresh (fix the eight).** *Immediate; blocked on decisions.*
   The 2026-09-05 verification pass found the catalog ~14 months stale: Gemini
   (`2.0-flash`/`1.5-pro`) and DeepSeek (`chat`/`reasoner`) IDs are **retired and
   fail at call time**; Anthropic's Opus tier carries the retired $15/$75 rate;
   Mistral prices all wrong; OpenAI o3 has a source conflict; Groq is fine. Needs
   Carlos's five tier-mapping decisions (`CATALOG_REFRESH.md` §"Decisions needed"),
   plus Qwen + Grok still to verify. Deliverable: verified current prices, cited,
   `verified_at` = today → **clears the guard, server routes again.**

2. **SC2 — Programmatic price source (OpenRouter feed).** *Enabler.* OpenRouter
   publishes machine-readable pricing — the `json_api` source S4b's `refresh.py`
   was built for but has none of. Wiring it turns currency from a six-weekly
   hand-verification into an automated drift check. This is what makes a larger
   catalog survivable. Depends on SC1.

3. **SC3 — Multi-model catalog expansion.** *Payoff, only safe after SC2.* Today
   each tier is one model per provider; can't express "this provider has three
   good options at different prices." Two parts: (a) multiple models per tier (or
   fold into S7 capability scoring, dropping rigid tiers), (b) add newer models
   across the current eight. Concrete candidates surfaced during research:
   **Nous Hermes** models (Hermes 4 70B/405B — first solid pick), **Stepfun
   Step-3.5-Flash** ($0.10/$0.30, cheapest model clearing the frontier bar), and
   the newer Chinese open-weight labs (**Moonshot/Kimi, Zhipu/GLM, MiniMax**) that
   the market names as the current source of price pressure. All reached ideally
   via the SC2 OpenRouter source, not as standalone providers. Depends on SC2.

## Feature backlog (after the catalog chain, roughly by value)

High value — extends the arbitrage thesis:
- **S10 — Semantic response cache.** Cache *in front of* routing: skip the API
  call entirely on a repeated/near-identical prompt. Largest potential saving,
  most novel, most complex (correctness + staleness). A sprint on its own.

Moderate:
- **S6 — Batch API routing.** OpenAI/Anthropic batch endpoints ~50% cheaper for
  async work; add a batch tier.
- **S7 — Quality/capability scoring.** Benchmark signal per model so routing can
  weigh capability, not just price. (Also the structural home for SC3's
  multiple-models-per-tier, if rigid tiers are dropped.)
- **S11 — Per-request cost ceiling.** `max_cost_usd` hard guardrail, complements
  the S1 soft budget alert.
- **S12 — Structured-output / tool-calling passthrough.** Route only to providers
  that support JSON mode / function calling when the request needs it.
- **S13 — Weighted cost-quality routing.** "Cheapest within X% quality of best."
  Depends on S7.
- **S14 — Multi-key rotation per provider.** Round-robin keys for rate-limited
  heavy users; must preserve BYOK no-storage.

## Provider policy (the rule for "should we add X?")

Recorded in `SPRINTS.md` Deferred. **A provider joins the token-priced core only
if it is (a) token-priced AND (b) adds model or price coverage we don't already
have.** Agent/credit platforms fail (a); gateways and same-lane hosts fail (b).

Evaluated and declined (reasons on record so they don't resurface):
- **Manus** — credit-based agent platform; no per-token cost. Would be a separate
  "agent tier," never a token-priced provider.
- **MS Copilot / Azure** — Copilot is seat-licensed SaaS (no per-token price);
  Azure OpenAI re-serves models we already reach at a compliance premium with
  non-BYOK auth — an Enterprise-tier data-residency *feature*, not a catalog entry.
- **Hermes Agent** — BYOM framework; a *consumer* of routers, not a provider.

Deferred provider classes (pulled in only on specific need): Chinese labs beyond
DeepSeek/Qwen (except as SC3 model candidates), enterprise gateways
(Bedrock/Azure/Vertex — Enterprise-tier features), other open-model hosts
(Together/Fireworks — Groq covers the lane). OpenRouter is the sole exception,
as a pricing *source* (SC2), not for coverage.

## Beyond the current roadmap (not yet sprints)

- **Pro tier packaging.** S1 built the analytics foundation, but the price,
  packaging, and free/Pro boundary are still undefined (open question from the
  window handover). BYOK means no take-rate on savings — the Pro tier must be
  worth paying for on its analytics alone.
- **Enterprise tier.** Where the deferred gateways (Azure/Bedrock/Vertex), data
  residency controls, audit logging, and compliance reporting would live. A
  sales-led motion more than a feature flag; the deferred-provider notes point here.
- **Schema extensions for exotic pricing.** DeepSeek's peak/off-peak and
  Gemini/Anthropic's >200K-token surcharges don't fit the one-price-per-model
  schema. SC1 encodes the standard/off-peak case and notes the rest; a
  time-of-day or context-tier pricing schema would be its own later sprint if it
  proves worth it.
