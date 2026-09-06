# Blueprint — CheapRouter Pricing (SP1)

A design document, not an implementation. It specifies a **second MCP server**
in this same repo — *CheapRouter Pricing* — whose product is **verified LLM
token-price truth with provenance**, and how it and the existing router relate.

Status: DESIGN (docs-first, per the discipline this project holds for
center-of-gravity changes). Nothing here is built until this blueprint is
reviewed and a build sprint is opened.

---

## 1. The idea, and why

There is **no single source of pricing truth** for LLM tokens — the market is
federated (8+ provider pages, 8 formats, 8 schedules, no consolidated tape).
That gap is an opportunity: the entity that maintains *verified, provenanced,
current* prices with real rigor can become the citable reference. cheaprouter
already built the methodology a truth-source needs — versioned prices, a
`verified_at`/`verified_by` provenance model, a staleness guard that **refuses
rather than lies**, drift detection, and a hard **no-invented-prices** rule.

CheapRouter Pricing turns that methodology from an *input to routing* into a
*product in its own right*. The router becomes the flagship **consumer** of the
truth source (and a live demo of why the data matters).

## 2. Governing metaphor — "tied at the waist"

The two servers are **joined at the core, distinct at the surface.** Not one
server with two names; not two strangers passing messages. They share one spine
— the price-truth layer — and each has its own head (tool surface) and hands
(what it does with the data).

```
        Router head                         Pricing head
   (route/estimate/stream)            (get/list/drift/history/verify)
            │                                     │
            ▼                                     ▼
   ┌───────────────────────── shared waist ─────────────────────────┐
   │  prices.json · pricing_table.py (loader + staleness guard)      │
   │  refresh.py (OpenRouter drift feed) · provenance model          │
   │  price history store · the three provenance tiers               │
   └────────────────────────────────────────────────────────────────┘
```

**What "tied at the waist" commits us to:**
- **One spine, imported by both** — never duplicated (duplication is the rot we
  fought all session). A change to the guard, the schema, or the feed changes
  both servers at once, by construction.
- **Distinct heads** — the router never grows pricing-catalog tools; the pricing
  server never grows routing tools. Each surface stays coherent to its purpose.
- **They move together but can still stand** — same repo, same release, same CI,
  same tests. Either can run alone (router with a static table; pricing server
  as a pure data service), but they're *designed to be deployed together* and to
  enrich each other. Reversible into two repos later **only if** the waist proves
  clean — which building it this way is precisely the test of.

## 3. Same repo, two entrypoints

- `server.py` — the router (unchanged).
- `pricing_server.py` — **new**. A second FastMCP entrypoint serving the pricing
  tool surface. Imports the shared waist modules; adds no routing.
- Shared waist (existing, now explicitly the joint): `prices.json`,
  `pricing_table.py`, `refresh.py`, plus new `price_history.py` and a
  `provenance.py` that formalises the tiers.
- MCPize deploys two servers from one repo (two entrypoints, shared modules) —
  a well-trodden pattern.

**Why same repo (settled):** the price-truth layer *is* the shared code; separate
repos would force either duplication (divergence) or a third published package
(more machinery than warranted now). Repo-split is a *later-stage* identity
decision the monorepo does not foreclose — and the clean extraction into a second
entrypoint is itself the test of whether a split is ever warranted.

## 4. Pricing server — tool surface (the head)

Provenance-first: every response **leads with** who-verified-what-when-against-
what-source, not as a footnote.

- **`pricing_get`** — verified price for a provider/model, with full provenance
  (verified_at, source URL, tier, confidence). The atomic "what does X cost?"
- **`pricing_list`** — the whole current table with per-entry staleness status.
- **`pricing_drift`** — surface OpenRouter-feed drift (SC2, exposed as a product
  feature): where the proxy disagrees with the verified table, right now.
- **`pricing_history`** — how a price has changed over time. **The killer feature
  no tracker offers well** — a citable, provenanced record of LLM price movement.
  Requires *keeping* history (new capability, see §6).
- **`pricing_verify`** *(operator-gated)* — record a human verification: set a
  price, cite a source, stamp verified_at/by. The disciplined write path; never
  auto-invoked.

## 5. The three provenance tiers (never blended)

The interactivity unlocks a third tier. All three coexist, each **explicitly
labelled**, never silently merged:

| Tier | Source | Meaning | Citable as |
|------|--------|---------|------------|
| **verified** | human-checked vs provider page | the authoritative list price | reference truth |
| **proxy** | OpenRouter feed (SC2) | unverified, machine-aggregated | "proxy-sourced" |
| **observed** | empirical, from the router's real completions | what a real call actually cost | empirical data point |

**The cardinal rule:** these tiers are a *hierarchy of trust*, never a blend.
`observed` and `proxy` inform and *challenge* `verified` (drift/validation
signals) but **never auto-become** verified prices. Publishing an observed cost
as a list price would violate the no-invented-prices rule through the back door —
an observed cost may be a caller's negotiated rate (SC5) or an off-peak/regional
rate (SC4), i.e. *not* the list price.

## 6. The waist interaction — bidirectional, metadata-only

The two servers **enrich each other**, which is the point of tying them at the
waist. Two clean, defined signal channels — never deep reach-in:

**Router → Pricing (the novel direction):**
- After a real completion, the router already knows **actual tokens + actual
  cost** (metadata it already computes for S1). It publishes that to Pricing as
  an **`observed` signal**: provider, model, tokens, cost, timestamp.
- Pricing uses it two ways: (a) **validation** — observed matches verified ⇒
  confidence up; (b) **drift trigger** — observed materially diverges from
  verified ⇒ flag "reality disagrees, go verify," stronger than any feed
  comparison because it's real spend.
- **Also availability** (from S8 health): "this price is real *and* this provider
  is currently reachable" — more useful than price alone.

**Pricing → Router:**
- Verified, fresh prices feed routing (already the plan).
- **Drift alerts** can inform routing dynamically (closing the SC7 loop through
  the pricing server rather than a static table): "provider just dropped 40%,
  prefer it" — *if and only if* that drop reaches `verified` (or the operator
  accepts proxy-provenance for that provider).

**Invariant protection at the boundary — load-bearing, designed in from line 1:**
1. **Metadata-only across the waist.** The observed signal carries provider,
   model, tokens, cost, timestamp — **never message content, never API keys.**
   The exact S1 storage discipline, now crossing a server boundary (a *new* leak
   surface, so stated explicitly).
2. **Provenance never corrupted.** Observed/proxy data are signals, not prices.
   No path auto-writes them into the verified tier.
3. **Reversibility preserved.** The interaction is a defined interface (shared
   modules + explicit signals), not entanglement. Each server still stands alone.
   If they couldn't run independently, we'd have built one server with two faces
   and lost the option to ever split — so the interface stays clean.

## 7. Price history (`price_history.py`) — the real new capability, and its store

A source-of-truth needs *change over time*, which the current design lacks. A
provenanced, append-only record: each entry =
`(provider, model, tier, field, old, new, provenance_tier, source, verified_at,
observed_at)`. Powers `pricing_history`, drift-over-time, and "how much has
DeepSeek dropped this quarter?" — the citable series no scraper keeps well, and
(under the financial-KPI framing, see PRICING_STRATEGY.md) the **core asset**: a
KPI's value is its trajectory, not its spot value.

### Why NOT Redis / Upstash for this (settled)

Reaching for the store already in the stack (Upstash Redis, from S1) is the
natural instinct — and it is the **wrong tool for trend data.** Redis is a
key-value/in-memory store optimised for fast lookups of *current state* — right
for S1's session/spend logs (write-append, read-recent, key-shaped), wrong for
price history, which is:
- **queried analytically** — range scans and aggregations over time
  ("every change for DeepSeek balanced over 8 quarters", "which providers cut in
  Q3", "the trajectory of the tier_powerful cohort"), not key lookups;
- **append-mostly, read-analytical, loss-intolerant** — a gap in the series is a
  *defect in the product*, because completeness is the value proposition;
- **unbounded and slow-growing, kept forever** — Redis is memory-priced; storing
  years of every-price-change in RAM-backed storage pays premium rates for cold
  archival data.
Redis *can* be bent to this (RedisTimeSeries, sorted-set gymnastics) but you fight
the tool and pay memory prices for archival data. Note this is NOT a reversal of
the S1 Supabase->Upstash swap: S1's data is key-shaped (Redis wins); history is
table-shaped (Redis loses). Right tool per data shape, not a change of mind.

### The store, by product stage (pluggable interface)

`price_history.py` is written against a **storage interface** (exactly like S1's
pluggable backend) so the choice is not load-bearing and can graduate without
touching history logic — the same discipline that let S1 swap Supabase->Upstash
cleanly. Backends, in the order to adopt them:

1. **Versioned append-only file (start here).** Price changes happen *weekly, not
   per-second* (the SC7 insight), so volume is tiny — order hundreds of rows/year
   across all providers. A committed append-only JSONL (or small Parquet) needs no
   database, is queryable with DuckDB/pandas, and — crucially for a *certified*
   product — **git itself is the immutable audit log**, giving provenance for
   free. For a slow-moving, provenance-critical, modest-volume dataset this is not
   a hack; it is arguably *more* certifiable than a database.
2. **Postgres (upgrade path).** When/if the KPI-intelligence product needs live
   analytical queries at scale, move to Postgres (e.g. Supabase): a `price_history`
   table where trend queries are plain SQL (`GROUP BY`, `date_trunc`, window
   functions for trajectory). Trend queries *are* relational queries.
3. **TimescaleDB / columnar (only if it gets serious).** A Postgres extension, so
   same SQL surface, no lock-in from starting at (2). Overkill until high-frequency
   analytics demand it.

**Store choice follows product direction:** if history is only an *internal
drift/validation signal* (blueprint minimal version), the existing S1 backend is
fine — don't add anything. If it is the *core asset of a certified KPI product*
(the recorded framing), it must be **auditable and citable**, which points to the
versioned file first and Postgres later, and away from Redis — "here is the signed,
dated, append-only record" is a stronger provenance claim than "trust our cache."

**History is metadata only** (prices + provenance), never content — same invariant
as S1, and (per §6) it holds across the waist too.

## 8. What this is NOT (guardrails)

- **Not** per-call price polling (see SC7 — rejected: hot-path latency, worse
  provenance). Pricing serves the *verified table + signals*, refreshed
  out-of-band.
- **Not** a blend of tiers into one number. Three tiers, always labelled.
- **Not** a new provider or a router feature — it's a distinct product sharing
  the spine.
- **Not** a repo split (yet) — same repo, reversible, the split is a later
  identity decision this does not force.
- **Not** content- or key-aware — the waist carries metadata only, forever.

## 9. Open questions for Carlos

1. **Deployment:** two MCPize servers from one repo, or one server that exposes
   both tool surfaces behind a flag? (Blueprint assumes two entrypoints; a single
   dual-surface server is simpler to deploy but muddies the "distinct heads" line.)
2. **Operator vs. public verify:** who can call `pricing_verify`? (Assumed:
   operator-only — the disciplined write path.)
3. **Observed-tier exposure:** does `pricing_get` ever *return* the observed tier
   to callers (labelled), or is observed purely an internal validation/drift
   signal that never leaves the waist? (Earlier lean: signal-first; this decides
   whether "observed" is a product surface or an internal mechanism.)
4. **History depth:** how far back, and rolled-up how? (A source-of-truth wants
   deep history; the store wants bounds — same cadence discipline as the S1 logs.)

## 10. Suggested build sequence (once approved)

- **SP1a** — ✅ SHIPPED 2026-09-05. `provenance.py` (three tiers) + tier-aware
  `pricing_table.py`; no router behaviour change, 12 tests.
- **SP1b** — ✅ SHIPPED 2026-09-05. `price_history.py`: append-only, pluggable, versioned-file backend (git = audit log); record_diff/trajectory/summary; metadata-only. 12 tests. Capture wiring into the verify flow is SP1d/SC8.
- **SP1c** — ✅ SHIPPED 2026-09-05. `pricing_server.py` + read tools (get/list/
  drift/history), provenance-first; distinct port + mcpize.pricing.yaml. 12 tests.
- **SP1d** — ✅ SHIPPED 2026-09-05. Router publishes OBSERVED signal to the shared
  price-history spine (opt-in, metadata-only, no-leak tested); observed_validation
  + pricing_observed tool; observed never promotes to verified. 9 tests.
- **SP1e** — `pricing_verify` operator write path + docs positioning the pricing
  server as the reference source.

Each SPx is a normal sprint (scope, tests, PR). SP1a/SP1b touch only shared
modules and are safe to land first; the router keeps working throughout.
