-- cheaprouter S1 — spend analytics persistent store
-- Run this in your Supabase SQL editor to create the routing_history table.
-- Then set SUPABASE_URL and SUPABASE_KEY in your MCPize deployment env.
--
-- PRIVACY: this table holds routing metadata only. No API keys, no message
-- content are ever written (enforced in storage.py by ALLOWED_FIELDS +
-- build_record()). The `session` column is an opaque SHA-256 prefix, not a
-- user identity.

create table if not exists routing_history (
    id            bigint generated always as identity primary key,
    timestamp     timestamptz not null default now(),
    session       text        not null default 'anon',
    provider_id   text,
    provider_name text,
    region        text,
    tier          text,
    input_tokens  integer,
    output_tokens integer,
    cost_usd      double precision,
    saved_usd     double precision,
    latency_ms    integer,
    success       boolean     not null default true,
    error_class   text
);

-- Fast session-scoped reads (the common query pattern).
create index if not exists routing_history_session_ts_idx
    on routing_history (session, timestamp desc);

-- Optional: enable Row Level Security and restrict by session if you expose
-- the anon key publicly. For a service-key deployment this is not required.
-- alter table routing_history enable row level security;
