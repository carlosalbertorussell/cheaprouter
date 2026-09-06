# Vision — CheapRouter Pricing as a token-cost market view ("the ticker")

A destination, not a sprint. This note describes what the pricing product could
*become* — a market view of token costs — and shows how the already-scoped pieces
ladder up to it. It commits to no build. Companions: `PRICING_SERVER_BLUEPRINT.md`
(architecture), `PRICING_STRATEGY.md` (publishing/monetization).

Status: VISION. Reframes the SP-series toward a destination; individual steps stay
normal reviewed sprints. Deliberately does not schedule anything.

---

## The reframing

A price *table* answers "what does X cost?" — a reference lookup. A *ticker*
answers "what's moving, and where's the edge right now?" — a market view. If token
pricing is becoming a competitive financial KPI (see PRICING_STRATEGY.md), the
natural product surface isn't a spreadsheet — it's a **tape**: price movements and
the *spreads between providers*.

**Why this is real and not a metaphor:** cheaprouter already computes the spreads.
Every routing decision is "provider A is $X cheaper than B for this task." The
router has been calculating the edge all along and throwing it away after routing.
The ticker is that same computation *surfaced as the product*. The signal already
exists; the ticker publishes it.

## Two signals, labelled honestly — NEVER blurred

This is the load-bearing discipline of the whole idea. "Arbitrage" has a precise
meaning that mostly does NOT apply to token pricing, and blurring it would be the
no-invented-prices rigor abandoned as no-invented-claims. Two distinct signals:

### 1. Cheapest-capable spread (cross-model) — NOT arbitrage

The gap, within a capability tier, between the cheapest provider and the rest.
"tier_balanced: cheapest is DeepSeek at $0.0035, 42% below the tier median."
This is real and useful, but it is **substitution, not arbitrage**: GPT-4o and
DeepSeek-V4 are *different assets* (different models, possibly different output
quality). You are choosing a cheaper *comparable* capability, not buying-and-
selling an identical thing. **Always labelled "cheapest-capable spread"** — never
"arbitrage." Includes tier inversions (a real anomaly worth surfacing): "Provider
X's fast tier now costs more than Provider Y's balanced tier" (cf. Mistral Large
priced below Mistral Medium).

### 2. True arbitrage (same-model, cross-venue) — genuinely arbitrage

The *same* model priced differently across hosts: Llama-3.3-70B on Groq vs
Together vs Bedrock; a model direct vs via OpenRouter vs via a cloud gateway.
Identical asset, different venue, different price — **this is real arbitrage** in
the precise sense, and it's the more defensible headline feature precisely because
it withstands scrutiny. It is exactly what SC3's cross-venue coverage expansion
(cloud gateways, same-model multi-host) would surface. **Labelled "true
arbitrage"** — because it is.

The credibility of the whole product rests on these two never being merged into
one "arbitrage" number. A certified source distinguishes them; a hype tracker
doesn't.

## What the ticker surfaces

- **The tape (event-driven, not real-time).** Price *movements* as detected:
  "DeepSeek balanced ↓18%", "Anthropic Opus held 4th quarter running", "Mistral
  Large crossed below Medium". Honest cadence: LLM prices move *weekly, not
  per-second* (the SC7 insight), so this is an **event feed of movements**, not a
  continuously-streaming stock tape. Building for stock-ticker velocity would be
  theater — the underlying doesn't move that fast.
- **Live spreads (genuinely current).** Per-tier cheapest-capable spread, and how
  it's moving. This view *is* true right now (unlike movements, which are
  event-driven), so it's the "live" part that honestly earns the ticker framing.
- **True-arbitrage feed.** Same-model cross-venue price gaps — the defensible
  arbitrage headline.
- **Charts (price_history).** The trajectory behind each "symbol" — the KPI series.

## How the already-scoped pieces ladder up to it

The ticker is not a new direction; it's the surface that *unifies* what's scoped:

| Piece | Role in the ticker |
|-------|--------------------|
| KPI framing (PRICING_STRATEGY) | the "why" — pricing is a tracked financial metric |
| `price_history` (SP1b) | the charts / trajectory behind each symbol |
| SC2 OpenRouter feed | movement detection (the tape's input) |
| SC7 freshness loop | keeps the tape current without hot-path polling |
| SC3 cross-venue coverage | the source of **true** (same-model) arbitrage |
| the router itself | proof-of-concept that the spreads are actionable |
| provenance tiers (blueprint) | every quote carries its trust level |

So the ticker is the *destination* the SP-series was already walking toward — it
reframes the goal, it doesn't add an orthogonal one.

## Honest caveats (the reasons this stays a vision, not a plan)

1. **Coverage becomes existential.** A "market view" covering 8 providers looks
   partial the way a stock ticker showing 8 stocks would. The ticker ambition
   collides with the currency-discipline (SC2/SC7 must automate currency *before*
   SC3 expands coverage) harder than anywhere else in the roadmap. The ticker is
   only credible once currency is automated AND coverage is broad — i.e. it sits
   *after* the SC-chain, not alongside it.
2. **It leans product/business, not open-source tool.** A ticker with users has
   uptime and freshness expectations a reference doc doesn't. This loops back to
   the still-open question of whether this becomes a thing Carlos *runs*. The
   vision doesn't answer that — it just makes the stakes of answering it clearer.
3. **The velocity trap.** Resist building for real-time. The movement feed is
   event-driven; only the spread view is live. Overbuilding for a tape that
   updates weekly would be effort spent on theater.

## What this is NOT

- Not a real-time streaming tape — the underlying moves weekly; it's an event feed
  plus a live-spread view.
- Not "arbitrage" as a blanket label — two distinct signals, honestly separated.
- Not a scheduled sprint — a destination that reframes the SP-series.
- Not a commitment to run a product — it sharpens that question without answering it.

## If it were ever built (illustrative, unscheduled)

A ticker surface would come *after* the SC-chain (currency + coverage) and the
SP-series (the pricing server + history), as e.g.:
- a `pricing_ticker` tool / view: current per-tier spreads + recent movements,
  provenance-labelled, both signals distinct;
- a `pricing_arbitrage` tool: same-model cross-venue gaps (true arbitrage), only
  meaningful once SC3 cross-venue coverage exists;
- a public web view (the "terminal") as the flagship free-tier funnel, with the
  certified/attestation depth as the paid layer (per PRICING_STRATEGY).

None scheduled. Recorded so the destination is explicit and the scoped work is
understood as laddering toward it.
