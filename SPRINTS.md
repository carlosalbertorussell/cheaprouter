# cheaprouter — Sprints

In-place sprint log. Active sprint at top; closed sprints and backlog below.
One task per unit, PARTIAL-honest over DONE-false, closures noted inline.

---

## Active

### SC1d — Groq migration + Qwen endpoint

**Goal:** stop routing Groq callers to models Groq has shut down, and send Qwen
calls to the region whose prices the table encodes. Triggered by a 2026-11-14
re-read of the two providers SC1c carried.

**Re-read 2026-11-14 (Groq and Qwen only):**
- **Groq — migrated.** console.groq.com/docs/deprecations: `llama-3.1-8b-instant`
  and `llama-3.3-70b-versatile` shut down 2026-08-16 for free and developer
  accounts (enterprise only now, unpublished price). A BYOK developer key routed
  there got a 404, which `client.py` treats as non-transient, so no failover.
  Now `tier_fast` → `openai/gpt-oss-20b` 0.075/0.30, cache 0.037;
  `tier_balanced`/`tier_powerful` → `openai/gpt-oss-120b` 0.15/0.60, cache 0.075;
  ctx 131072. Prices from the console.groq.com/docs/model/openai/* pages, checked by
  the owner on 2026-11-14. Cache is modelled because
  console.groq.com/docs/prompt-caching lists both models; 0.037 (not 0.0375) is
  what the 20b page shows. `pricing_urls.groq` → console.groq.com/docs/models; the
  refresh block carries the per-tier model pages under `sources`. Refresh stays
  `manual`: OpenRouter's gpt-oss price is the open model across hosts, not Groq's,
  so an `openrouter` mapping would not be a sound comparison.
- **Qwen — re-read, no price change.** alibabacloud.com/help/en/model-studio/model-pricing
  (Singapore): qwen-flash 0.05/0.40, qwen-plus 0.40/1.20 non-thinking, qwen-max
  1.60/6.40 (cache 0.32), all unchanged. `pricing_urls.qwen` fixed to that page
  (`/models` has no prices). **Endpoint switched:**
  https://www.alibabacloud.com/help/en/model-studio/base-url lists
  `dashscope.aliyuncs.com` as China (Beijing) and `dashscope-intl.aliyuncs.com` as
  Singapore, so `base_url` is now `https://dashscope-intl.aliyuncs.com/compatible-mode`
  (`client.py` appends `/v1/chat/completions`, matching the documented
  `.../compatible-mode/v1`). The OpenAI-compatibility page
  (.../compatibility-of-openai-with-dashscope) agrees in its migration note.
- **Provenance.** Table-wide `verified_at` stays 2026-11-01 (the other six were
  not re-read). `verified_by` gained an SC1d sentence; the "CARRIED, NOT RE-READ"
  flags for Groq and Qwen are gone from `verified_by` and `notes` (`groq_2026_11`
  replaced by `groq_lifecycle`), so the listing marks both re-read.

**Downstream:** MCPIZE_LISTING.md regenerated. README and MCPIZE_DOCS provider /
tier tables, tier winners, pricing-page list and the carried sentence updated.
Winners (`route()`, every key, 1000/500, health ignored): fast Groq gpt-oss-20b
(0.000225 vs Qwen qwen-flash 0.00025), balanced Groq gpt-oss-120b, powerful Groq
gpt-oss-120b (0.00045 vs DeepSeek v4-pro 0.00165). **Balanced is an exact tie**
with DeepSeek deepseek-flash (both 0.15/0.60 → 0.00045); `estimate_all_providers`
sorts on cost only and the sort is stable, so prices.json order (groq before
deepseek) decides. Tests updated: cache-support and non-cache tests (Groq now has
cache; OpenAI/Qwen used as the no-cache cases), cache-flip test (Qwen now wins
200K-input fast without cache), listing winners/carried tests, new pins for the
Groq rows and the Qwen host. 413 tests pass (random order, repeated); coverage 96.3%.

**Carried:** Qwen context windows (not shown on the pricing page); the six
providers re-read on 2026-11-01.

**Deferred / follow-ups:**
- **A model-not-found 404 should fail over** instead of ending the request (a
  retired model at one provider should not strand a caller who holds other keys),
  while bad keys (401/403) still do not fail over. Design change in `client.py` /
  the failover loop, not done here.
- **Qwen keys are region-bound.** Callers with a China (Beijing) key now hit the
  Singapore host and will fail; CONNECT.md still points at the China console.
  Decide whether to document a Singapore key source or model region per key (SC4).
- **Tie-break is implicit.** The balanced tie is decided by dict order in
  prices.json. If a deliberate rule is wanted (cache rate, latency), make it
  explicit in `estimate_all_providers`.
- `notes.caching` and the cache-provider lists in README / MCPIZE_DOCS still say
  Groq bills cache at the full input rate; left for the ST5 merge, which owns that
  text.
- Newer Qwen models (qwen3.7-flash ≤32K tiering, qwen3.7-plus promo ambiguity,
  qwen3.8-max) seen and not adopted; owner decision.
- `region` for Qwen stays `cn` (vendor jurisdiction / `BLOCKED_REGIONS` semantics);
  the Singapore host does not change that without a policy decision.

### ST5 — Abuse-resistant health and scoped reads

**Goal:** no single caller can steer provider health for everyone else, while
health still catches real outages; history and spend reads show a caller only
their own records unless the operator opts in to a global view. Plus small
correctness fixes found in the ST4 review.

**Delivered on this branch:**
- **Abuse-resistant health** (`health.py`). History records gain a privacy-clean
  `transient` boolean (in `storage.ALLOWED_FIELDS`, set by `build_record` from
  `log_decision(transient=...)`; the server classifies with
  `client.is_transient_error` at write time). Scoring rules:
  - only successes and transient, provider-side failures (429, 5xx, timeouts,
    connection errors) are scored; 401/403/400/404 are ignored entirely;
  - **legacy records** (no `transient` field): successes count, failures do not.
    Pre-ST5 `error_class` can't reliably separate a timeout from a bad key, so the
    conservative call is to forget pre-ST5 failures at deploy (one window to
    rebuild). An unclassified new failure (`transient: null`) is treated the same;
  - each session contributes at most `HEALTH_MAX_PER_SESSION` (default 5) records
    per provider, newest first (successes too, so one caller can't mask an outage);
  - a provider is unhealthy only when its scored failures come from at least
    `HEALTH_MIN_SESSIONS` (default 2) distinct sessions. Without this a caller
    flooding the shared recent list with its own 429s could push every other
    record out of the scan and leave only its capped failures. Single-user servers
    set it to 1;
  - `HEALTH_WINDOW` now counts *scored* records; up to `HEALTH_SCAN_FACTOR`
    (default 4) times as many raw records are read in the same one request, so
    ignored caller errors can't crowd real evidence out.
  `HEALTH_WINDOW`, `HEALTH_MIN_SAMPLES`, `HEALTH_THRESHOLD` work as before.
  `arbitrage_provider_health` keeps `{score, samples, healthy}` and adds
  `sessions`, `failing_sessions` and the new thresholds. Health still reads
  unscoped history internally (`history.read_history(session=None)`), never via
  the tool. No new writes; the Upstash write is still the one background pipeline.
- **Scoped reads by default.** `arbitrage_get_history` / `arbitrage_spend_report`
  without `session` read the shared `anon` session only, never everyone's; with a
  token, that token's records (unchanged). All-sessions view only with the
  operator opt-in `ARBITRAGE_ALLOW_GLOBAL_READS=1`. Responses gain `scope`
  (`session` / `anon` / `all_sessions`). `history.caller_scope()` resolves the
  key; the module docstring now says exactly this. Tool docstrings, parameter
  descriptions, README, MCPIZE_DOCS, SECURITY, SKILL.md, ARCHITECTURE and
  mcpize.yaml updated.
- **Observed model ID.** Confirmed real: the router recorded `completion.model_id`
  (what the provider returned, e.g. a dated snapshot), while
  `refresh.observed_validation` keys on the prices.json model ID, so such entries
  never matched. Observed entries now carry the table's `model_id`, with the
  provider's ID in a separate `served_model_id` (only when it differs).
- **prices.json `notes.caching`** text corrected (Mistral is modelled, OpenAI is
  not). No price changed; `gen_listing.py` reads only the provider-prefixed
  `CARRIED` notes, so the listing is unaffected (`--check` passes).
- **`.coverage` untracked** and coverage outputs added to `.gitignore`.

**Tests:** `tests/test_health.py` (50 bad-key failures from one session never
count; 400/403/404 ignored; bad-key spam can't flood out real evidence;
transient failures across sessions deprioritize; one session's 429 storm is
capped; a window-flooding session can't condemn; success spam is capped too;
legacy and unclassified records; knobs), `tests/test_st5_abuse_and_scope.py`
(end to end through `arbitrage_route_completion`: 401/400/403/404 never
deprioritize, 503/429/timeout/connect outages across sessions do, a one-session
429 storm doesn't; scoped reads: no session sees only anon, never other hashes;
opt-in returns all; scoped reads unchanged; health still reads all sessions),
`tests/test_observed.py` (dated served ID still validates). 452 tests pass
(random order, several runs); coverage 96.6%.

**PARTIAL / deferred:**
- **Not Sybil-proof.** Session tokens are caller-chosen, so a caller rotating
  tokens still looks like several sessions; the cap and the two-session rule stop
  a single caller, not a determined one. Per-key-hash attribution would need a key
  fingerprint in records, which Invariant A rules out today.
- **`anon` is shared.** Every caller without a token reads and writes the same
  `anon` records (and shares one health cap). Documented; a private view needs a
  token.
- **429 is counted as provider-side** as specified, though a 429 can reflect the
  caller's own key quota; the per-session cap and two-session rule bound it.
- ~~**Groq and Qwen re-read** is a separate, human-verified step; not done in ST5.~~
  Done in SC1d (owner-verified Groq prices, Qwen re-read).

### ST4 — Hosted key safety, savings maths and docs accuracy

**Goal:** a hosted server must never spend its operator's provider keys; the cost
and savings numbers must describe what actually happened; and the docs must say
what the code does. A docs review, checked against the code, found all three.

**Delivered on this branch:**
- **Hosted key safety.** `providers.resolve_api_key` fell back to provider env vars
  (`ANTHROPIC_API_KEY`, ...) whenever a caller omitted a key, so any anonymous
  caller of a hosted server could route completions billed to the operator, and
  `.env.example` told operators to store those keys as MCPize secrets. Rule now:
  caller keys always win; the env fallback applies only over stdio
  (`server.main` marks the process local before `mcp.run(transport="stdio")`) or
  with the explicit opt-in `ARBITRAGE_ALLOW_ENV_KEYS=1`. Never by default over
  HTTP. Every key path (route, stream, estimate, provider status, pricing table)
  goes through this one function. `.env.example` (also renamed from the old
  project name), `mcpize.yaml` and `SECURITY.md` document it.
- **Savings and actual-cost maths.** Response `savings` was the pre-flight estimate
  for the routed winner even after failover, actual cost ignored the cache-read
  discount routing had used, and failed attempts were logged with the estimated
  cost and the decision's savings (inflating spend and saved totals). Now actual
  cost uses `pricing.CostEstimate.compute` (the routing formula, cache split
  capped at the actual input); `savings` compares the provider that answered with
  the most expensive eligible provider, all priced at the actual token counts
  (`savings.vs_provider_id` names the baseline; `estimated_savings` keeps the
  pre-flight figure); failed attempts log cost 0 and saved 0. Same response keys,
  corrected values, two added fields. `history.py` untouched.
- **MCPize listing regenerated.** `scripts/gen_listing.py` writes the pricing block
  of `MCPIZE_LISTING.md` from `prices.json` (2026-11-01), marks Groq and Qwen as
  carried, not re-read, and computes the per-tier winners with `route()`.
  `tests/test_listing.py` fails if the listing drifts. "Semantically equivalent" /
  "clears the tier" claims removed.
- **Docs sweep** (README, ARCHITECTURE, MCPIZE_DOCS, CONNECT, CONTRIBUTING, SECURITY,
  SKILL.md, mcpize.yaml, PIPELINE.md): all 10 router tools with parameters and
  defaults plus the 5 `pricing_*` tools; port 8081; endpoint `/mcp`; Claude.ai
  "Connectors"; prices.json + staleness guard; history durable with Upstash, JSONL
  fallback; cache providers per prices.json; computed tier winners (fast: Groq
  llama-3.1-8b-instant, balanced: DeepSeek deepseek-flash, powerful: Groq
  llama-3.3-70b-versatile, at 1,000/500 tokens with every key); unhealthy behind
  *all* healthy providers; health is global; unscoped history/spend reads return
  every session's records; budgets alert, never enforce; one o200k encoder;
  `pricing_observed` described as the table-price signal it is. PIPELINE.md
  marked superseded.

**Tests:** `tests/test_key_safety.py` (HTTP mode with env keys and no caller keys:
no eligible providers and no request, streaming included; estimate, status and
pricing ignore env keys; opt-in and stdio `main()` enable the fallback; caller
keys win), new hand-computed cases in `tests/test_route_completion.py` (savings at
actual tokens, failover to the baseline and to a middle provider, cache-aware
actual cost and baseline, cache clamp, zero-cost failure logging in summary and
spend report), `tests/test_listing.py`. 371 tests pass (random order, 10 runs);
coverage 96.6%.

**PARTIAL / deferred:**
- ~~**Unscoped reads are not scoped by default.**~~ **Done in ST5:** without
  `session` the history and spend tools read only the `anon` session; global
  reads need `ARBITRAGE_ALLOW_GLOBAL_READS=1`.
- ~~**Health is global, not per user.**~~ **Done in ST5 (abuse-resistant, still
  global):** caller errors no longer count, each session's say is capped, and one
  session alone can't condemn a provider. Not per key hash (see ST5 deferred).
- ~~**Groq and Qwen prices are still carried, not re-read.**~~ **Done in SC1d**
  (2026-11-14): Groq migrated with owner-verified prices, Qwen re-read.
- **Business decisions CR-06 (Pro price / free-Pro boundary) and CR-03
  (liability for "certified")** are the owner's; PRICING_STRATEGY.md,
  TICKER_VISION.md and PRICING_SERVER_BLUEPRINT.md were not touched.
- **Client one-pager** not written.

### ST3 — Budget storage and monthly spend

**Goal:** a budget must never be lost and the month's spend must be right, however
many calls a session makes. Fixes the bug found in ST1.

**The bugs:** (1) a budget was a `__budget__` record inside the capped spend history,
so after ~500 calls it fell out of the read window (and on Upstash LTRIM 0 4999
eventually deleted it) — `route_completion` silently stopped sending budget alerts.
(2) `budget_status` summed only the newest 500 records, so a session with more than
500 calls this month had its spend undercounted. (3) Marker pollution: the marker
reuses `cost_usd` for the budget amount, and `history_summary`, `spend_report` and
`arbitrage_get_history` counted it — a $50 budget showed up as $50 of spend from
provider `__budget__` (provider health already skipped it).

**Delivered on this branch:**
- **Budgets stored apart from history**, behind `get_budget` / `set_budget` /
  `budget_snapshot` on the backend interface. Upstash: `budget:{session}` (one `SET`).
  JSONL: a small JSON file next to the history file (`<history>.budgets.json`,
  default under `/tmp`; override with `ARBITRAGE_BUDGET_FILE`), written atomically.
- **Exact monthly spend.** Upstash: `INCRBYFLOAT spend:{session}:{YYYY-MM}` plus
  `EXPIRE` (70 days) added to the existing single write pipeline; the month comes
  from the record's timestamp. JSONL: a full scan filtered by session and month.
  Semantics unchanged: every record counts by its `cost_usd`, including failed
  attempts (`route_completion` logs those with their estimated cost).
- **One read per `budget_status` on Upstash:** a single pipeline `GET budget`,
  `GET spend counter`, `LRANGE` of the session's newest 500 records (no extra
  request vs ST2). Writes stay in the ST2 background pipeline; everything stays
  best-effort.
- **Backward compatible.** With no dedicated budget, the newest legacy marker is
  used (JSONL: anywhere in the file; Upstash: within the newest 500 records, then
  migrated to `budget:{session}` with `SET NX`). For spend written before deploy
  (no counter yet), Upstash takes the larger of the counter and the history-window
  sum for the month.
- **No more markers:** `set_budget` no longer writes into history, and
  `read_history` filters legacy markers, so summaries, spend reports, get_history
  and health never count them.
- Tests first: `tests/test_budget.py` (40 tests, both backends) — the ST1 500-call
  reproduction failed before the fix (21 of the new tests failed) and passes after;
  >500 calls, list cap, month rollover, legacy fallback/override/migration,
  pre-ST3 spend, request shape and counts, failures swallowed, marker pollution,
  80% / over-budget alerts after 850–1200 calls. 380 tests pass (random order,
  repeated); coverage 96.1%.

**PARTIAL / not done:**
- On Upstash, a legacy budget marker already beyond a session's newest 500 records
  at deploy time is not recovered (it was already lost before ST3); the user has
  to set the budget again.
- In the deploy month, Upstash spend older than the newest 500 records and written
  before the counter existed is not counted (the counter starts at deploy).
- ~~Failed attempts still count their estimated cost toward the budget.~~ Fixed by
  ST4: failed attempts are now logged at cost 0, so they no longer count.

### ST2 — Upstash history off the routing path

**Goal:** a slow or unreachable Upstash must never slow routing down. Before
this, every routed call made its Upstash requests one after another, inline on
the async event loop, each with a 10s timeout: an unscoped health read (one
`SMEMBERS` plus one `LRANGE` per session ever seen, so it grew without bound),
three requests per history write (per failover attempt), and two budget reads.
A healthy Upstash added a few hundred ms per call; an unreachable one could stall
a call, and every other request on the server, for minutes.

**Delivered on this branch (after ST1, so its tests guard the change):**
- **One request per write.** `UpstashBackend.write` sends a single `/pipeline`
  request (per-session LPUSH + LTRIM, global LPUSH + LTRIM, SADD).
- **Short timeout.** `UPSTASH_TIMEOUT_S`, default 2s (was a hard-coded 10s).
- **Background writes.** Upstash writes go through one daemon writer thread
  (bounded queue, in order, drops on overflow rather than blocking). The backend
  is resolved on the caller's thread, so a write always goes where it was
  configured at call time. Reads first wait briefly for this process's pending
  writes, so read-after-write still holds. JSONL stays inline (a fast local
  append). `storage.flush_writes()` drains the queue; it runs at exit and after
  every test.
- **Bounded unscoped reads.** New capped `history:recent` list (5000): health
  scoring and unscoped history are one `LRANGE`, however many sessions exist. Data
  written before the list existed is still read through a pipelined fan-out
  (2 requests) until `history:recent` has records.
- **`budget_status` reads once** instead of twice.
- **Event loop never blocked.** `route()` (health read), budget and history calls
  in `server.py` run via `asyncio.to_thread`.

**Tests:** `tests/test_upstash_nonblocking.py` (request count with 20 sessions,
timeouts and the env override, a write returning before a slow Upstash,
read-after-write, write ordering, queue overflow dropping instead of blocking,
JSONL staying inline, single-read budgets, and an end-to-end check that the event
loop is never stalled for more than 200ms while every Upstash request takes
300ms). ST1's Upstash tests updated to the pipelined contract; its fake REST server
now speaks `/pipeline`. An autouse fixture drains background writes before the
network guard is removed. 340 tests pass (5 random-order seeds); coverage 96.4%.

**PARTIAL / not done:**
- The ST1 budget bug (the budget marker falls out of the 500-record read window)
  was unchanged here. **Fixed in ST3.**
- During the transition after deploy, an unscoped read (health scoring, global
  spend report) sees only `history:recent` once it has any records. Older
  per-session data stays readable per session, but drops out of global views
  until the recent list fills.
- Health snapshots are not cached. With one request per check, caching wasn't
  needed.

### ST1 — Regression test coverage

**Goal:** make the test suite catch regressions in the paths users actually depend
on — real provider adapters, streaming, money maths, key-leak guards, every MCP
tool, the Upstash layer — before refactoring the history/storage layer. Tests and
CI only; no product-code change, no test seams added.

**Why now:** an audit (2026-11-14) found 184 passing tests, but only 29% coverage of
`client.py`, six of nine `server.py` tools never called, the streaming tests mocking
the very code they claimed to test, one test that would turn CI red on 2026-12-16
with no code change (`test_shipped_prices_json_is_fresh`), one test calling the live
OpenRouter feed, and no coverage enforcement in CI.

**Delivered on this branch:**
- **P0 — determinism.** The date-dependent shipped-table test is replaced by
  deterministic ones (the shipped table loads and validates; staleness at fixed
  dates and against a monkeypatched UTC clock). The drift-report test is stubbed
  and asserts real content. New autouse network guard in `tests/conftest.py` fails
  any test that opens a real httpx connection.
- **P1 — adapters, streaming, key leaks.** `tests/test_client.py`: Anthropic,
  OpenAI-compatible (all six) and Gemini over `httpx.MockTransport` — URL, auth
  headers, body (system prompt placement, max_tokens, role mapping), text/token
  parsing, HTTP and transport errors propagating with the right transient class.
  `tests/test_streaming.py`: the real Anthropic/OpenAI SSE parsers and the real
  `server._run_streaming` (text, usage, TTFT, started flag on a mid-body disconnect),
  plus `route_completion` end to end with only HTTP mocked. `tests/test_key_leak.py`:
  Gemini 429 and ConnectError/failover via `route_completion` — the key is in
  neither the response nor the history store.
- **P2 — money, history, budgets, tools.** `tests/test_route_completion.py`:
  hand-computed costs and savings incl. the cached-input split, actual cost billed
  at the succeeding provider's rates, per-attempt history attribution during
  failover, budget alerts at 80% / over budget, previous-month exclusion, latest
  budget wins. `tests/test_server_tools.py`: every tool (incl. `get_pricing` stale
  banner, `check_price_drift` with a proposal), `estimate_cost` with no winner,
  `provider_status` by id / unknown id, input-model validation (bad tier, bounds,
  required fields, extra fields), and the registered tool set.
- **P3 — Upstash and fallbacks.** The real `UpstashBackend._cmd` over a mock
  transport: the exact LPUSH / LTRIM 0 4999 / SADD sequence with the Bearer header,
  list cap, scoped/unscoped reads (newest-first merge, limit, bad JSON skipped),
  500 / timeout / ConnectError swallowed, routing still answering during an Upstash
  outage; `get_backend` with one env var; JSONL ordering/corruption; token-counting
  fallbacks. Weak tests tightened (savings `>= 0`, the tautological observed-emit
  test, the loose stale-override assertion, error-key-only asserts).
- **P4 — CI.** `py_compile` covers every module and script; pytest runs with
  pytest-cov over all product modules and `--cov-fail-under=92`.

**Result:** 184 → 329 tests, all passing (repeated runs, random order). Source
coverage 79% → 96.6% (`client.py` 29% → 98%, `server.py` 66% → 98%, `storage.py`
86% → 100%, `tokens.py` 89% → 100%).

**Bug found — not fixed in ST1 (product change, out of scope); fixed in ST3:** budgets silently vanish.
`get_budget`/`budget_status` only scan the newest 500 history records, so once a
session routes ~500 calls after `set_budget`, the budget marker falls out of the
window: `get_budget` returns None and `route_completion` stops sending
over-budget alerts. On Upstash the 5000-record list cap (LTRIM) eventually deletes
the marker outright. Repro: `set_budget('u', 1.0)`, then 500 ×
`log_decision(..., session='u', actual_cost_usd=0.01)` → `budget_status('u')` is
`None` although $5 was spent against a $1 budget. No test committed for it (it
would fail); fix with the budget-storage change below.

**PARTIAL / deferred:**
- **Upstash non-blocking refactor:** done in ST2 (above), on the same branch.
- Budget storage should move out of the capped spend list (fixes the bug above).
  **Fixed in ST3.**
- The key-leak guard holds because `build_record` and the attempt log keep only
  the text before the first `:` of an error, and httpx messages put `https:`
  before the key. It works, but by accident; a dedicated redaction step would be
  sturdier.
- Not covered: `pricing_server`/`server` `__main__` entrypoints, a few
  `price_history` backend branches, the `GLOBALLY_BLOCKED_REGIONS` env path.
- CI still installs tiktoken, which may download its encoding on first use; tests
  pass with either the encoder or the heuristic.

### SD1 — Skill packaging + MCP-registry distribution (free discovery)

**Goal:** make the server *discoverable*, not just hosted. MCPize hosts the server;
the official MCP registry (`registry.modelcontextprotocol.io`) is the discovery layer
that Smithery and other aggregators ingest. Hosted ≠ discoverable (review CR-07) — the
server is at 0 stars and in no registry. This lists it (pointing at the MCPize
endpoint) and ships a skill so agents know when to use it. Free; the router stays the
funnel. Monetization stays with the pricing/certification layer, out of scope here.

**Delivered on this branch:**
- `skills/cheaprouter/SKILL.md` — free wrapper skill: when/how to use the router tools
  (route/estimate/pricing/health/spend) and the pricing head, BYOK stated. The adoption
  aid; never gated or priced.
- `server.json` — official MCP registry manifest, **remote** server (`remotes`,
  `type: streamable-http`), namespace `io.github.carlosalbertorussell/cheaprouter`
  (GitHub namespace, NOT domain — the endpoint is on mcpize.run, so a domain namespace
  would fail verification; github-oidc only authorizes `io.github.*`). Registers a
  signpost to the MCPize endpoint; hosts nothing.
- `.github/workflows/publish-registry.yml` — publishes via the official `mcp-publisher`
  CLI with GitHub OIDC (no stored secret). Triggers on a `v*` tag or manual dispatch.

**Carlos must do before publishing:** set the real MCPize endpoint in `server.json`
(`remotes[0].url`, ships as `https://YOUR-ENDPOINT.mcpize.run/mcp`). Likely
`cheaprouter.mcpize.run` per the repo's docs pattern, but confirm against the actual
deployment — do not publish a signpost to the wrong address.

**CR-08 (first-call friction) — eval, deferred:** before reading registry conversion,
evaluate removing the MCPize account/key barrier to the first call. The server is
already BYOK, so the MCPize key is the only friction between discovery and a working
first call; if it needs a signup, installs won't convert and the channel gets misread
as "registry doesn't work" rather than "the barrier does." Decision + config, not code.

**Unconfirmed (recorded, not a blocker):** whether Smithery surfaces an ingested
*remote* entry, and with what prominence vs infra-hosted ones. Registry-side ingestion
is documented; Smithery's display behaviour is not.

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

- **SC1c — Catalog-wide "cheapest current" pass (deferred from SC1b, 2026-10-12).**
  SC1b only fixed the broken rows. Cheaper current models exist that the table
  does not use: Anthropic Sonnet 5.5 ($2/$10/$0.10 vs Sonnet 4.6 $3/$15/$0.30),
  Haiku 5.5, Opus 5.5 ($4/$20/$0.20); Gemini 3.6 / 3.8 Flash; xAI grok-4.7; Mistral
  Large 4 (public preview, `mistral-large-4`). Gemini and Grok tier_fast now
  duplicate tier_balanced by decision — a distinct fast model for each is part of
  this pass. Each model swap is Carlos's call.
- **OpenAI migration (urgent-ish, deferred from SC1b).** gpt-4o-mini, gpt-4o and o3
  were kept and priced from their model pages, but OpenAI dropped them from its
  pricing table, marks o3 Deprecated (succeeded by GPT-5), and shuts down snapshots
  gpt-4o-2024-05-13 on 2026-10-23 and o3-2025-04-16 on 2026-12-11. Candidates:
  GPT-5.6 Sol / Terra / Luna (prices not yet read). Decide before December.
- **Catalog hygiene found in SC1b (small, no decision of substance):** pin Mistral
  to dated ids (`mistral-small-2603`, `mistral-large-2512`) — no Mistral page says
  what the `-latest` aliases resolve to; model cache pricing for Mistral (Small 4
  $0.015, Large 3 $0.05) and qwen-flash/qwen-plus (implicit cache 20% of input),
  today billed at full input; refresh `context_window` values that drifted
  (grok-4.3 page says 1M vs 131072; Mistral Small 4 and Large 3 say 256k vs
  32000/128000).

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

- **SC1c — Catalog-wide sweep + OpenAI migration** (2026-11-01) — full re-read of all 8 providers' current pricing pages, triggered by verifying model-update status. Prompted by the OpenAI EOL the staleness guard was blind to (gpt-4o snapshot shut down 2026-10-23). **Re-read & changed:** OpenAI — gpt-4o-mini/gpt-4o/o3 all deprecated and off the pricing page → migrated to `gpt-6-luna` (0.10/0.50), `gpt-6.1-terra` (1/5), `gpt-6.1-sol` (2/10); cache not published, omitted; context_window carried. Did NOT adopt gpt-5.6-sol (promo price to ~2026-11-21). Mistral — Large 3→Large 4 (0.5/1.5 → 0.7/2.1), Small 4 / Large 4 cache now modelled (0.015 / 0.07). DeepSeek — `deepseek-v4-flash` renamed `deepseek-flash` (off-peak price identical). Grok — powerful `grok-4.6`→`grok-4.7` (same 2/6/0.5, newer flagship); 4.3 still listed, unchanged. **Re-read, no change:** Gemini (3.1-flash-lite 0.25/1.5 and 3.1-pro-preview 2/12 still current; gemini-4-argon on promo, not adopted). **Carried, NOT re-read (flagged):** Groq (marketing pages have no prices; needs console.groq.com); Qwen (names moved to qwen3.8-flash/qwen3.7-plus/qwen3.8-max but no prices published; carried). verified_at=2026-11-01, guard good to 2026-12-16. Tests updated (deepseek id, OpenAI cache now unmodelled); 183 pass. **STRONG follow-up:** Anthropic 4.x is now LEGACY — 5.5 is much cheaper (Haiku 5.5 0.10/0.50 vs 1/5 — ~10x; Sonnet 5.5 2/10; Opus 5.5 4/20) but claude.com/pricing doesn't expose the 5.5 API model IDs; migration needs verified IDs. 4.x kept (still listed/working). Remaining deferred: Groq/Qwen priced re-read; Anthropic 5.5 migration; the SC9 lifecycle tracker (this EOL was caught only by a manual check).
- **SC1b — Price refresh before expiry** (2026-10-12) — table was due to expire 2026-10-20 with five rows on models no longer on their provider's pricing page. Applied Carlos's decisions: gemini tier_fast `gemini-3.5-flash` (never existed; its prices were 3.5 Flash-Lite's) → `gemini-3.1-flash-lite`; grok tier_fast `grok-4.1-fast` → `grok-4.3`; OpenAI gpt-4o-mini / gpt-4o / o3 kept and priced from their model pages. Ambiguities resolved: gemini powerful id → `gemini-3.1-pro-preview`; qwen-plus output at non-thinking rate (2.4 → 1.2); Mistral Small direction read from the labelled pricing table. Re-read all 21 rows against provider pages; real finds: o3 cache-read 0.20 → 0.50, qwen-max cache 0.25 → 0.32 (provider's 20%-of-input rule). verified_at=2026-10-12, guard good to 2026-11-26. Docs (README, MCPIZE_DOCS tiers, MCPIZE_LISTING table — the latter also stale since PR #47) brought in line. No test asserted these ids/prices; 183 pass. Deferred to backlog: catalog-wide pass (SC1c), OpenAI migration, catalog hygiene.
- **SC2 — Programmatic price source (OpenRouter feed)** (2026-09-05) — refresh.py drift-checks 6 providers (Anthropic/OpenAI/Gemini/Mistral/DeepSeek/Grok) against OpenRouter's public /api/v1/models feed; one shared fetch, per-token->per-1M conversion, cache-read compared. Groq+Qwen stay manual (documented). Missing IDs / feed outage -> fetch_failed, never bad data. Currency now automated for most of the catalog instead of a 45-day manual chore. 8 tests (mocked feed; sandbox blocks openrouter.ai, runs live on MCPize).
- **SC1 — Catalog refresh** (2026-09-05) — verified all 8 providers against current pricing; the table is current again so the deployed server routes instead of refusing. Replaced 4 providers' retired model IDs (Gemini, DeepSeek, Grok, Qwen) that would fail at call time; corrected all stale prices (Anthropic Opus $15/$75→$5/$25, all Mistral, o3 $10/$40→$2/$8). Cheapest-current-per-tier rule; Mistral Medium dropped (Large now cheaper). DeepSeek off-peak, Qwen Singapore, standard context — variance noted for SC4. verified_at=2026-09-05, guard now fresh. 6 stale-premise tests updated (shipped table is now fresh, not stale); Gemini gained verified cache pricing. 130 tests pass.
- **S5 — Streaming completions** (2026-09-05) — route_completion 'stream' option consumes the provider SSE stream (anthropic + openai formats; gemini falls back). Streaming client layer (stream_provider/stream_supported). Reports time_to_first_token_ms. Failover is pre-first-token only — a mid-stream error is terminal (tagged _cheaprouter_stream_started), never retried, since partial output may be committed. MCP returns one final result; this is server-side streaming from the provider, not an MCP-level token stream. 7 tests.
- **S3 — Prompt-caching awareness** (2026-09-05) — cost model prices cache-hit input tokens at each model's cache-read rate; cached_input_tokens hint on get_pricing/estimate_cost/route_completion. cached_input_price_per_1m in the table for anthropic/openai/deepseek; others bill cache tokens at full input rate (never advantaged). Can flip the winner on cache-heavy loads (proven: DeepSeek beats Groq at tier_fast under heavy caching). Estimates report cache_supported. Inherits S4a staleness. 10 tests.
- **S4b — Price refresh path** (2026-09-05) — refresh.py + arbitrage_check_price_drift compare fetchable prices to the table and report drift (match/drift/fetch_failed/manual); propose_updated_table() builds a review candidate but never writes prices.json or advances verified_at. Per-provider 'refresh' strategy in prices.json (json_api|manual); all manual today (no verified programmatic source — no invented prices). scripts/refresh_prices.py for CI/cron. 9 tests. Clears the S4a guard via reviewed diff, not blind editing.
- **S4a — Price-table staleness guard** (2026-09-05) — prices moved from hardcoded literals to a versioned prices.json with provenance; pricing_table.py loads/validates (fails loudly, no silent default) and exposes verified_at / age_days / is_stale. Routing + estimation REFUSE when stale (older than max_age_days=45); get_pricing + provider_status warn but still return. ARBITRAGE_ALLOW_STALE_PRICES=1 override. scripts/check_prices.py wired into CI (non-blocking price-freshness job + weekly cron). Every response carries a price_table block. Protects S1's savings figure. 20 tests. NOTE: shipped table is ~432 days old, so the deployed server refuses routing until prices are re-verified — intended, flagged in the PR.
- **S2 — Automatic failover** (2026-09-05) — on a transient error (429/5xx/timeout) route_completion retries the next provider in ranked order, up to max_failover (default 2). Non-transient errors (bad key/request) never fail over. Each failed attempt feeds S8 health. Response reports failed_over + attempts. router exposes ranked_pool; client has is_transient_error. 18 tests. Completes the health/failover pair with S8.
- **S8 — Provider health tracking** (2026-09-05) — recent failures deprioritize a provider in routing (behind healthy providers of similar price, never excluded; an all-unhealthy pool still returns the cheapest). New `arbitrage_provider_health` tool; `route_completion` gains a `health_aware` toggle and reports `deprioritized_providers`. health.py reads history metadata only. 10 tests. Precursor to S2 failover.
- **S1 — Spend Analytics** (2026-09-04) — durable session-scoped spend tracking, spend report + budget tools, pluggable Upstash/JSONL backend. Foundation of the Pro tier. Full detail in the collapsed block under Active.
