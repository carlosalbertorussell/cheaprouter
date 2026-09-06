# Changelog

All notable changes to cheaprouter are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **SC1 catalog refresh — the price table is current again (verified 2026-09-05), so the deployed server routes instead of refusing.** Fixed four providers whose model IDs had been retired and would fail at call time: Gemini (2.0-flash/1.5-pro → 3.5-flash/3.1-flash-lite/3.1-pro), DeepSeek (chat/reasoner → v4-flash/v4-pro), Grok (4/4.1-fast → 4.6/4.1-fast), Qwen (turbo → flash). Corrected all stale prices, incl. Anthropic Opus (was the retired $15/$75 → $5/$25) and every Mistral row. Applied the cheapest-current-model-per-tier rule; dropped Mistral Medium as strictly dominated by the now-cheaper Large. DeepSeek encoded at off-peak, Qwen at Singapore endpoint, all at standard context — the peak/region/long-context variance is noted for **SC4**.

### Added
- **S5 streaming completions.** `arbitrage_route_completion` gains a `stream` option that consumes the provider's SSE stream (Anthropic + OpenAI-compatible formats, 6 of 8 providers; others fall back to a normal call). Lowers time-to-first-token and reports it as `time_to_first_token_ms`; the full text is still returned as one result. New streaming client layer (`stream_provider`, `stream_supported`). Failover interacts safely: it applies only *before* the first streamed token — a mid-stream error is terminal and never silently retried on another provider (partial output may already be committed).

### Added
- **S3 prompt-caching awareness.** The cost model now accounts for provider prompt caching: a new `cached_input_tokens` hint on `arbitrage_get_pricing`, `arbitrage_estimate_cost`, and `arbitrage_route_completion` prices cache-hit input tokens at each model's cache-read rate. Anthropic, OpenAI, and DeepSeek carry `cached_input_price_per_1m` in the table; providers without a cache offering bill cached tokens at the full input rate (never wrongly advantaged). On cache-heavy workloads this can flip which provider is cheapest — e.g. DeepSeek's cache-read pricing can beat a nominally cheaper no-cache provider. Estimates report `cached_input_tokens` and `cache_supported`. Cache prices are the same unverified mid-2025 vintage as the rest of the table and inherit the S4a staleness guard.

### Added
- **S4b price refresh path.** New `refresh.py` + `arbitrage_check_price_drift` tool compare fetchable provider prices against the table and report drift, so the S4a staleness guard can be cleared with a reviewed diff instead of blind hand-editing. Each provider declares a `refresh` strategy in prices.json (`json_api` = fetchable, `manual` = human-verified); all ship `manual` today since no verified programmatic source exists. The path **never writes prices.json and never advances verified_at** — it proposes a candidate for human review; no price is invented or silently accepted. New `scripts/refresh_prices.py` (exit 1 on drift) for CI/cron.

### Added
- **S4a price-table staleness guard.** Provider prices moved from hardcoded literals in `providers.py` into a versioned `prices.json` with provenance (`verified_at`, `max_age_days`, source URLs). New `pricing_table.py` loads and validates it and **fails loudly** on a missing/malformed file — no silent default. Every tool response carries a `price_table` block. When prices are stale (older than `max_age_days`), `arbitrage_route_completion` and `arbitrage_estimate_cost` **refuse** with a structured error; `arbitrage_get_pricing` and `arbitrage_provider_status` still return data behind a warning. `ARBITRAGE_ALLOW_STALE_PRICES=1` downgrades refusal to a warning. New `scripts/check_prices.py`, wired into CI as a non-blocking `price-freshness` check plus a weekly scheduled reminder.
<<<<<<< HEAD
- **S9 accurate token counting.** Pre-flight input-token counts now use a real tokenizer (tiktoken `o200k_base`) instead of the crude `chars/4` estimate, so the cost ranking that drives routing rests on accurate volume. Falls back to an improved word/char heuristic if tiktoken is unavailable (still far better than `chars/4`, and handles code and CJK). New `arbitrage_count_tokens` tool to preview a request's size.
=======
>>>>>>> f24d3f9 (ci: wire price-freshness check into CI (non-blocking + weekly cron))

### Changed
- **BREAKING (deploy):** the committed price table is dated 2025-06-30 and is well past its 45-day limit, so on deploy the server **refuses routing and cost estimation until prices are re-verified** in `prices.json`. This is intended: a savings figure computed from unverified ~430-day-old prices cannot be defended. Re-verify prices and update `verified_at`, or set `ARBITRAGE_ALLOW_STALE_PRICES=1` to proceed with a warning.
- `arbitrage_route_completion` counts input tokens with the real tokenizer rather than `chars/4`, correcting cost comparisons for code, CJK, and other content that the flat divisor mis-estimated.

### Added
- **S2 automatic failover.** On a transient error (429, 5xx, timeout, connection error) `route_completion` now retries the next provider in ranked order, up to `max_failover` times (default 2). Non-transient errors (bad key, bad request) never fail over — they'd fail identically everywhere. Each failed attempt is logged as a failure, which feeds S8 health scoring, so failover and health reinforce each other. The response reports `failed_over` and a per-provider `attempts` list.

### Added
- **S8 provider health tracking.** Recent routing failures now feed back into routing: a provider failing more than half its recent calls (min 3 samples) is *deprioritized* — moved behind healthy providers of similar price — but never excluded, so a blip can't strand a caller and an all-unhealthy pool still returns the cheapest option. New `arbitrage_provider_health` tool reports success rate, sample count, and healthy flag per provider. `route_completion` gains a `health_aware` toggle (default on) and returns `deprioritized_providers`. Health is read from history metadata only — no keys, no content.

### Added
- **S1 spend analytics (Pro-tier foundation).** Durable, session-scoped spend tracking. New tools `arbitrage_spend_report` (spend by provider/tier/day) and `arbitrage_set_budget` (monthly budget with 80%/over alerts surfaced in route_completion). Pluggable storage backend: Upstash Redis when `UPSTASH_REDIS_REST_URL`/`UPSTASH_REDIS_REST_TOKEN` are set (setup in `docs/upstash-setup.md`), local JSONL otherwise — self-hosters need no cloud dependency. Opaque per-session attribution; the store holds routing metadata only, never keys or content.

### Changed
- `arbitrage_route_completion` and `arbitrage_get_history` accept an optional `session` token to attribute and scope spend.

### Fixed
- History no longer stores a response-content preview — the store is now metadata-only, verified by test (privacy invariant A).

### Fixed
- Pin `mcp[cli]` to `<2`: the 2.x SDK renamed `FastMCP` to `MCPServer`, breaking server startup on deploy. Dependabot now ignores all `mcp` updates (manual bump only, after verifying the FastMCP API).

### Added
- Sprint roadmap (`SPRINTS.md`): S1 spend analytics active; backlog S2–S14 covering failover, prompt-caching awareness, live pricing, streaming, health tracking, accurate token counting, semantic response cache, batch routing, quality scoring, cost ceilings, structured-output passthrough, weighted routing, and multi-key rotation.

## [1.0.0] — 2026-09-02

### Added
- Initial release of cheaprouter — a BYOK MCP server for LLM token cost arbitrage.
- Eight providers across three regions: Anthropic, OpenAI, Google Gemini, Groq,
  Mistral (EU), DeepSeek (CN), Alibaba Qwen (CN), and xAI Grok.
- Three capability tiers (`tier_fast`, `tier_balanced`, `tier_powerful`) mapping
  semantically equivalent models across all providers.
- Five MCP tools: `arbitrage_get_pricing`, `arbitrage_estimate_cost`,
  `arbitrage_route_completion`, `arbitrage_provider_status`, `arbitrage_get_history`.
- BYOK (Bring Your Own Keys) model — API keys are supplied per request, never
  stored or logged.
- Latency-aware routing with configurable threshold (`latency_sensitive` flag).
- Region filtering via `allowed_regions` and global `BLOCKED_REGIONS`.
- Provider exclusion via `excluded_providers`.
- JSONL routing history with cumulative spend and savings tracking.
- Three wire protocols: Anthropic native, OpenAI-compatible, Google Gemini native.
- MCPize cloud deployment via streamable-http transport.

[Unreleased]: https://github.com/carlosalbertorussell/cheaprouter/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/carlosalbertorussell/cheaprouter/releases/tag/v1.0.0
