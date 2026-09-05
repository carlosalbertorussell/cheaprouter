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

Per-session Redis lists:

- `history:{session}` — a list of JSON records, newest first (`LPUSH`), capped at
  5000 entries per session (`LTRIM`).
- `history:sessions` — a set of all session keys, so an unscoped history read can
  fan out across sessions.

## Privacy

Records hold routing metadata only — provider, tier, region, token counts, cost,
latency, savings. Never API keys, never message content (enforced in `storage.py`
by `ALLOWED_FIELDS` + `build_record()`). The `{session}` key segment is an opaque
SHA-256 prefix, never a user identity.

## Notes

- History writes are best-effort: if Upstash is unreachable, the write is dropped
  silently and routing is never affected.
- Free-tier Upstash is per-request priced and generous; a community deployment
  will typically stay within it.
