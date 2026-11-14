# Upstash setup — durable spend analytics (S1)

cheaprouter persists routing history to [Upstash Redis](https://upstash.com) when
configured, so spend data survives MCPize redeploys. Without it, the server falls
back to local JSONL (fine for self-hosting, but ephemeral on cloud).

## Steps

1. Create a free Upstash Redis database at https://console.upstash.com
2. On the database page, open the **REST API** section and copy:
   - `UPSTASH_REDIS_REST_URL`
   - `UPSTASH_REDIS_REST_TOKEN`
3. Set both in your MCPize deployment environment.

That's it — no schema to create. cheaprouter creates the keys it needs on first write.

## Data model

- `history:{session}` — a list of JSON records per session, newest first (`LPUSH`),
  capped at 5000 entries (`LTRIM`).
- `history:recent` — the most recent records across all sessions, capped at 5000.
  Unscoped reads (provider health scoring, global history) use this list, so they
  are one request however many sessions exist.
- `history:sessions` — a set of all session keys. Only used as a fallback for
  unscoped reads when `history:recent` is empty (data written before it existed).

Each write is a single `/pipeline` request covering both lists and the set.

## Privacy

Records hold routing metadata only — provider, tier, region, token counts, cost,
latency, savings. Never API keys, never message content (enforced in `storage.py`
by `ALLOWED_FIELDS` + `build_record()`). The `{session}` key segment is an opaque
SHA-256 prefix (`s_` + 16 hex chars), never a user identity. Records written
without a session token go under `history:anon`.

## Notes

- History writes are best-effort and never on the routing path: they run on a
  background thread, so a slow or unreachable Upstash does not delay a response.
  A write that fails is dropped silently. If writes back up (1000 pending), new
  ones are dropped instead of queueing without limit.
- Reads (health scoring before routing, budget status after it) wait for this
  process's pending writes first, so they see what was just written. Every
  request uses a short timeout, `UPSTASH_TIMEOUT_S` (default 2s).
- Free-tier Upstash is per-request priced and generous; a community deployment
  will typically stay within it. A routed call costs about 3 requests: one health
  read, one write per provider attempt, one budget read when a session is passed.
