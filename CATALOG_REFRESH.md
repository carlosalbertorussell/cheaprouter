# Catalog Refresh — findings & decisions needed

> **STATUS: EXECUTED 2026-09-05 (SC1).** Decision taken: *cheapest current model per tier*. All 8 providers verified and prices.json updated (verified_at=2026-09-05); the guard is fresh and the server routes again. DeepSeek encoded off-peak, Qwen at Singapore, standard context — peak/region/long-context variance deferred to SC4. This document is retained as the verification record.


Working notes for the model-catalog refresh sprint (S-catalog). This is the
verification pass from 2026-09-05, escalated to a sprint because the catalog —
not just the prices — is ~14 months stale. Several providers' model IDs in
prices.json are **retired and would fail at call time**, so this is a product
decision about what cheaprouter routes to, not a number swap.

**Hard rule (unchanged):** no invented prices, no silently-chosen replacement
models. Every final number cites a provider source; every model swap is Carlos's
call, recorded here.

## Per-provider findings (verified 2026-09-05 via web search)

| Provider | Current table model | Status | Finding |
|----------|--------------------|--------|---------|
| **anthropic** fast | claude-haiku-4-5 | ⚠ price | Haiku 4.5 is **$1/$5** (table has $0.80/$4.00). Cache read = 10% of input → $0.10. |
| anthropic balanced | claude-sonnet-4-6 | ✓ price | $3/$15 correct. Cache read $0.30. Sonnet 5 now exists ($3/$15) as a newer option. |
| anthropic powerful | claude-opus-4-6 | ✗ price+model | Opus 4.6 is **$5/$25**, not the $15/$75 in the table (that's the retired Opus 4/4.1 rate). Current flagship line is **Opus 5 / Fable 5**. Cache read $0.50. |
| **openai** fast | gpt-4o-mini | ✓ | $0.15/$0.60 correct. Cache read ~$0.075 correct. |
| openai balanced | gpt-4o | ✓ | $2.50/$10 correct. Still available (legacy but live). |
| openai powerful | o3 | ⚠ conflict | Sources split: several say o3 cut to **$2/$8**; one July source still $10/$40. Needs confirmation. Newer: GPT-5.x, GPT-6 Astra ($10/$50). |
| **gemini** fast | gemini-2.0-flash | ✗ RETIRED | 2.0 Flash **shut down 1 Jun 2026**. Maps to 3.5 Flash. |
| gemini balanced | gemini-1.5-pro | ✗ RETIRED | 1.5 family returns **404**. |
| gemini powerful | gemini-2.5-pro | ⚠ retiring | 2.5 Pro still works ($1.25/$10) but **retires 16 Oct 2026**. Current Pro is 3.1 Pro ($2/$12). Cache read = 10% of input. |
| **groq** all | llama-3.1-8b / 3.3-70b | ✓ | Both current and correctly priced ($0.05/$0.08, $0.59/$0.79). Newer mid-tier GPT-OSS 120B ($0.15/$0.60) available if we want a distinct balanced tier. |
| **deepseek** all | deepseek-chat / -reasoner | ✗ RETIRED | Aliases **retired 24 Jul 2026**. Current: deepseek-v4-flash, deepseek-v4-pro. Now has **peak/off-peak** pricing (off-peak flash $0.22/$0.66, pro $0.66/$1.98) + cache-hit rates. Model choice + which price window to encode is a decision. |
| **mistral** fast | mistral-small-latest | ⚠ price | Small 4 = **$0.15/$0.60** (table $0.10/$0.30). Alias still resolves. |
| mistral balanced | mistral-medium-latest | ✗ price | Medium 3.5 = **$1.50/$7.50** (table $0.40/$1.20, badly off). |
| mistral powerful | mistral-large-latest | ✗ price | Large 3 = **$0.50/$1.50** (table $2.00/$6.00 — Large got much cheaper). |
| **qwen** all | qwen-turbo/plus/max | ? | Not yet verified — pending. |
| **grok** all | grok-4.1-fast/4.3/4 | ? | Not yet verified — pending. No pricing URL ever recorded. |

## Decisions needed from Carlos

For each provider, the tier mapping (which current model → fast/balanced/powerful)
is a product call. The big ones:

1. **Anthropic powerful** — stay on Opus 4.6 ($5/$25), or move to Opus 5 / Fable 5?
   (Balanced could also move Sonnet 4.6 → Sonnet 5, same price.)
2. **OpenAI powerful** — confirm o3 price ($2/$8 vs $10/$40), or move to a GPT-5.x model?
3. **Gemini** — fast+balanced are retired. Replace with which current models?
   (e.g. 3.5 Flash / 3.1 Flash-Lite / 3.1 Pro.) 2.5 Pro is a stopgap that dies Oct 16.
4. **DeepSeek** — v4-flash + v4-pro confirmed; encode off-peak or peak prices?
   (Off-peak is the safer/typical default. Peak is 2x.)
5. **Groq** — keep 8B/70B, or introduce GPT-OSS 120B as a distinct balanced tier?

## Note on peak/off-peak (DeepSeek) and long-context tiers (Gemini/Anthropic)

The current price model has one input+output+cache price per model. DeepSeek's
peak/off-peak and Gemini/Anthropic's >200K-token surcharges don't fit that shape.
For this sprint the pragmatic call is to encode the **off-peak / standard-context**
price (the common case) and note the surcharge in prices.json `notes`, rather than
expand the schema. A schema change (time-of-day or context-tier pricing) would be
its own later sprint if it proves worth it.
