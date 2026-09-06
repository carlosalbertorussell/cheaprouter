# Strategy — CheapRouter Pricing: publishing & monetization

Companion to `PRICING_SERVER_BLUEPRINT.md`. The blueprint is the *architecture*
(how the pricing server works). This is the *business* (how the data is published
and monetized). Kept separate on purpose: the pricing model will change many times
before the architecture does — different lifespans, different docs.

Status: STRATEGY NOTES, not commitments. Options and direction, recorded so the
build can be shaped by where it's headed. Nothing here is decided-final except
the top-line direction.

---

## Direction (decided): tiered — free real-time feed → paid certified/attestation

Two tiers of the **same data at different provenance depths** — NOT two separate
data operations. This is the load-bearing idea: if the free and paid tiers were
different datasets, we'd be running two businesses. They are one dataset, exposed
at two levels of the provenance model the blueprint already defines.

- **Free tier — real-time feed (top of funnel).** The current price table
  (`verified` + `proxy` tiers) as an open feed: HTTP `GET /prices`, dated JSON
  snapshot, the pricing MCP's read tools. Broad reach, drives adoption, builds
  the reputation the paid tier sells on. This is the *commodity* lane — it
  competes with OpenRouter/trackers on freshness and coverage, and that's fine
  because it's the funnel, not the product.
- **Paid tier — certified / attestation (the product).** The *same* prices, plus
  the attestation layer: signed & dated snapshots citable in a contract or audit,
  full source trail, confidence scoring, the `price_history` series, and change
  alerts. This is the *trust* lane — defensible, no incumbent, and aligned with
  the governance/risk/board credibility that is Carlos's actual moat.

**Why this shape works:** free real-time is a race-to-zero commodity on its own,
and certified-only has no funnel. Tiered, the commodity feed *feeds* the trust
product — adoption of the free feed is what makes the certified snapshot worth
citing ("the source everyone uses, now signed and dated for your audit").

## The moat is certification, not data

Everyone in this space *publishes* prices (OpenRouter, the tracker sites). Almost
no one *certifies* them — stands behind a number with a methodology and provenance
trail the way an auditor stands behind a financial statement. That attestation
slot is essentially unoccupied, and it fits Carlos's risk/governance/board-advisory
credibility better than a real-time feed ever could.

The blueprint's provenance tiers (`verified` / `proxy` / `observed`) are already
the skeleton of an attestation framework — built before it was named that. The
paid product deepens exactly that skeleton: who verified, against what source,
when, with what confidence, and how it has moved over time.

## Publishing / distribution options (menu, not commitments)

Free-tier reach:
- **Public HTTP price API + dated JSON snapshot** — reaches every developer and
  dashboard, not just MCP clients. The OpenRouter-shaped distribution.
- **Pricing MCP in a registry/marketplace** — distribution via the (early,
  uncrowded) MCP ecosystem.
- **"Verified by" badge / embeddable widget** — provenance layer *under* other
  people's pages; distribution through the existing ecosystem, high credibility.

Paid-tier products:
- **Certified snapshot licensing** — dated, signed, provenanced price sheet,
  citable in contract/audit. Per-snapshot or subscription. The core certified play.
- **Historical dataset** (`price_history`) — the provenanced time series of LLM
  price movement, licensed. Genuinely scarce; sells to analysts, procurement,
  researchers, AI-cost due-diligence (Carlos's world).
- **Change-alert / webhook subscriptions** — "notify me the moment a provider
  reprices." Real procurement pain, clear willingness to pay (today they find out
  on the invoice).
- **Attestation-as-a-service** — verify & certify *someone else's* stated pricing
  claims (a vendor's rates, a FinOps tool's numbers). The most differentiated,
  most Carlos-shaped option: a ratings/attestation business, not a data feed.
- **Embedded / OEM** — other tools (routers, dashboards, FinOps platforms) license
  the feed as their pricing backend. B2B2C reach; become infrastructure.
- **API SLA tiers** — free feed; paid adds freshness SLA, rate limits, uptime
  guarantee. Standard data-API model; the commodity monetization, thin.

## Monetization fit (honest ranking for Carlos specifically)

1. **Attestation-as-a-service** — most differentiated, no incumbent, maps directly
   onto governance/board credibility. Closest to an auditor's business.
2. **Certified snapshot licensing** — the productized version of #1; defensibility
   is what's sold, not data.
3. **Historical dataset** — scarce, citable, one-time or subscription.
4. **Change-alerts** — underrated, real pain, clear buyer (procurement/finance).
5. **Embedded/OEM** — good reach, less differentiated.
6. **API SLA tiers** — commodity; fine as free-funnel + light paid, not the business.

## Emerging framing — token pricing as a hyperscaler financial KPI (recorded, not yet lead positioning)

Observation (2026-09): token price-per-capability-tier is behaving less like a
product spec and more like a **competitive financial KPI** — a unit-economic
measure tracked and moved for strategic reasons, the way "cost per available seat
mile" works for airlines or "cost per kWh" for utilities. Evidence from the SC1
verification pass alone:
- OpenAI's o3 $10/$40 -> $2/$8; GPT-5.6 tier cuts described as "defend market share."
- Anthropic holding Opus at $5/$25 across generations while repricing the old
  $15/$75 tier *down* — a deliberate "premium, more affordable" signal.
- Mistral Large priced *below* Mistral Medium — positioning, not cost.
- DeepSeek introducing peak/off-peak billing — yield management imported from
  airlines/electricity.
- "The squeeze is coming from Chinese labs" as a pricing-pressure narrative.

**If that framing holds, it reframes the paid tier** from "developer price feed"
(commodity) to **"provenanced intelligence on an emerging financial KPI"** — whose
buyers are equity analysts, FinOps-at-scale, M&A due-diligence, corporate strategy,
and boards (Carlos's actual world), at a far higher willingness-to-pay than
developer tooling. Under this framing `price_history` stops being a feature and
becomes **the core asset** — a KPI's value is in its *trajectory*, not its spot
value. "DeepSeek's balanced-tier cost fell 60% over three quarters while Anthropic
held flat" is an intelligence product; "$0.27/1M today" is a lookup.

**Status: recorded, NOT yet the lead positioning.** Decision deferred — the framing
is real but positioning the whole paid tier around it is premature. Two caveats to
weigh when deciding: (1) it raises the liability bar to a *financial-decision*
standard (an analyst citing the trajectory in a memo relies at a different level
than a dev looking up a price) — making the provenance discipline a legal
requirement, not just a moat; (2) credible KPI intelligence about "the
hyperscalers" wants comprehensive coverage of the financially-material players —
which drives the coverage consequence below.

### Consequence for coverage (this part IS decided)

The KPI framing **does** change the coverage discipline — coverage expansion
becomes a *driver*, not a default-defer. But it changes SC3's **rationale and
target list, not its timing**: SC3 stays sequenced behind SC2 (expanding before
currency is automated rebuilds the maintenance-vs-sprawl trap — unchanged). What
changes:
- SC3's expansion target now explicitly includes the **financially-material
  players**, not just cheaper models within the eight: the major labs' full
  lineups, and — newly relevant — the **cloud gateways (Azure OpenAI, AWS Bedrock,
  Google Vertex)**, because that is where a large share of enterprise token spend
  actually flows, so a KPI series that omits them is incomplete as *financial*
  intelligence. (Gateways were deferred as *routing* targets; as *KPI-coverage*
  targets they are in scope for SC3.)
- Rationale shifts from "capture more arbitrage" to "be comprehensive enough that
  the price-KPI series is citable as market intelligence."
- Discipline preserved: still behind SC2, still threshold-gated, still no invented
  prices. Comprehensiveness serves *citability*; a rigorous series beats a
  sloppy-but-broad one — coverage grows only as fast as currency can keep it honest.

## Autonomous tending — resolving the persistence objection (SC8)

The strongest objection to the first-mover-via-trust thesis: trust compounds over
*years*, and only if the reference is tended *quarter after quarter* — a
maintenance-forever burden that collides with a high-opportunity-cost operator's
time. Unsustained first-mover advantage is just an expensive way to educate the
market for whoever comes next.

**SC8 (the autonomous verification agent) is what dissolves this.** Tending splits
into two halves along the *same line* as free-vs-certified:
- **Automatable (~95%):** drift detection (SC2), scheduled verification against
  public provider pages, history capture, PR-proposed updates, published snapshots.
  Runs autonomously in GitHub Actions. This is the grind — and it's gone.
- **Irreducibly human (~5%):** *standing behind* the number (attestation is a
  human liability act, not a technical one), resolving genuine ambiguity (source
  conflicts, model retirements, structural repricing), and owning the methodology.
  This is the *value*, and it's exactly the high-leverage senior work — low-volume,
  high-worth, the part your credibility makes valuable.

So the persistence question isn't "will I grind on data for years?" (SC8 answers
no) but "will I stay the light-touch overseer who vouches and judges?" — a
sustainable role, not a second job. **The automation boundary IS the monetization
boundary IS the attestation boundary.** That the same line keeps appearing is how
we know the design is coherent: the free tier is what the agent can tend alone;
the certified tier is what only a human can vouch for.

Honest limit: autonomous ≠ unattended. The agent misreads pages sometimes;
escalations need a human; methodology is an ongoing (light) responsibility. SC8
converts a daily data-ops job into oversight — not into zero work.

## The discipline this requires (non-negotiable)

Every monetization option above adds a **promise**, and promises about data have
teeth:
- A certified snapshot someone cites in a contract → an error is a **liability**,
  not a bug.
- A freshness SLA → downtime is a **breach**.
- An attestation → you are **standing behind** a number.

This is why the blueprint's discipline is the *only* safe foundation for any of
this: provenance, refuse-rather-than-lie, no-invented-prices. Note the direction
each tier pulls:
- **Real-time/commodity monetization tempts you to LOOSEN** discipline for
  coverage and speed.
- **Certified/attestation monetization REQUIRES you to tighten it.**

The tiered model must resolve this in favour of tightening: the free feed may be
best-effort, but **anything sold — anything with the word "certified" on it — is
held to the full provenance standard.** The free tier's looseness must never
contaminate the paid tier's guarantee. (Architecturally: the `observed`/`proxy`
tiers can be looser; the `verified` tier that backs certification cannot.)

## What this means for the build (SP-series)

- The **free feed** is mostly the existing table exposed via new surfaces (HTTP +
  MCP read tools) — light lift, mostly SP1c.
- The **paid product** needs the attestation layer built *deeper* than a router
  ever required: signed/dated snapshots, confidence scoring, the source trail as
  a first-class field, and `price_history` (SP1b) as the dataset spine.
- So the tiered decision *sharpens* SP1: build the provenance/attestation model to
  certification depth from the start (SP1a), because the paid product is that
  model — not a feature added later.

## Open strategic questions (for Carlos, later — not build-blocking)

1. **Signing:** does a certified snapshot need cryptographic signing (a real
   signature/hash chain) to be citable, or is a dated provenance trail enough?
2. **Liability posture:** what does "certified" actually warrant? (This is a legal
   question as much as a product one — Carlos's governance lens applies.)
3. **Attestation scope:** verify only the 8 curated providers, or offer to certify
   arbitrary third-party pricing claims (the bigger, harder attestation business)?
4. **Pricing of the paid tier:** per-snapshot, subscription, per-seat, per-query?
   (Deliberately unanswered — this is the part that changes ten times.)
