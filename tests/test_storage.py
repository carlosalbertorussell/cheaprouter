"""Tests for the pluggable storage backend and privacy invariants (S1)."""

import json
import pytest
from storage import (
    build_record, sanitize, ALLOWED_FIELDS, JSONLBackend, get_backend, UpstashBackend
)


def test_build_record_has_only_allowed_fields():
    rec = build_record(
        session="s_abc", provider_id="groq", provider_name="Groq", region="us",
        tier="tier_fast", input_tokens=100, output_tokens=50, cost_usd=0.001,
        saved_usd=0.002, latency_ms=120, success=True,
    )
    assert set(rec.keys()) == ALLOWED_FIELDS


def test_build_record_never_carries_content_or_keys():
    # Even though callers pass an error string, only an error CLASS is kept.
    rec = build_record(
        session="s", provider_id="openai", provider_name="OpenAI", region="us",
        tier="tier_balanced", input_tokens=10, output_tokens=5, cost_usd=0.01,
        saved_usd=0.0, latency_ms=90, success=False,
        error="AuthenticationError: invalid api key sk-ant-SECRET in request body 'hello world'",
    )
    blob = json.dumps(rec)
    assert "SECRET" not in blob
    assert "hello world" not in blob
    assert rec["error_class"] == "AuthenticationError"
    # no field named response/content/api_key/messages
    assert not any(k in rec for k in ("response", "content", "api_key", "messages", "response_preview"))


def test_sanitize_drops_injected_fields():
    dirty = {"session": "s", "provider_id": "groq", "api_key": "sk-LEAK", "messages": "secret prompt"}
    clean = sanitize(dirty)
    assert "api_key" not in clean
    assert "messages" not in clean
    assert clean["provider_id"] == "groq"


def test_jsonl_write_read_roundtrip(tmp_path):
    f = tmp_path / "h.jsonl"
    be = JSONLBackend(str(f))
    be.write(build_record(
        session="s_1", provider_id="groq", provider_name="Groq", region="us",
        tier="tier_fast", input_tokens=1, output_tokens=1, cost_usd=0.001,
        saved_usd=0.0, latency_ms=10, success=True))
    got = be.read(session=None, limit=10)
    assert len(got) == 1
    assert got[0]["provider_id"] == "groq"


def test_jsonl_session_isolation(tmp_path):
    f = tmp_path / "h.jsonl"
    be = JSONLBackend(str(f))
    for sess in ("s_alice", "s_alice", "s_bob"):
        be.write(build_record(
            session=sess, provider_id="groq", provider_name="Groq", region="us",
            tier="tier_fast", input_tokens=1, output_tokens=1, cost_usd=0.001,
            saved_usd=0.0, latency_ms=10, success=True))
    alice = be.read(session="s_alice", limit=10)
    bob = be.read(session="s_bob", limit=10)
    assert len(alice) == 2
    assert len(bob) == 1
    # Alice's read never returns Bob's rows
    assert all(r["session"] == "s_alice" for r in alice)


def test_jsonl_sanitizes_on_write(tmp_path):
    f = tmp_path / "h.jsonl"
    be = JSONLBackend(str(f))
    # Force a dirty record straight to write() — sanitize must strip it
    be.write({"session": "s", "provider_id": "groq", "api_key": "sk-LEAK", "cost_usd": 0.001})
    raw = f.read_text()
    assert "sk-LEAK" not in raw
    assert "api_key" not in raw


def test_backend_selection_defaults_to_jsonl(monkeypatch):
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    assert get_backend().name == "jsonl"


def test_backend_selection_upstash_when_configured(monkeypatch):
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://x.upstash.io")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "test-token")
    assert get_backend().name == "upstash"


# ─── Upstash backend (mocked REST) ────────────────────────────────────────────

class _FakeRedis:
    """In-memory stand-in for the Upstash REST endpoint, command-compatible."""
    def __init__(self):
        self.lists = {}
        self.sets = {}
        self.strings = {}
        self.ttls = {}

    def run(self, args):
        cmd = args[0].upper()
        if cmd == "GET":
            return self.strings.get(args[1])
        if cmd == "SET":
            if "NX" in [str(a).upper() for a in args[3:]] and args[1] in self.strings:
                return None
            self.strings[args[1]] = str(args[2]); return "OK"
        if cmd == "INCRBYFLOAT":
            v = float(self.strings.get(args[1], 0)) + float(args[2])
            self.strings[args[1]] = repr(v); return repr(v)
        if cmd == "EXPIRE":
            self.ttls[args[1]] = int(args[2]); return 1
        if cmd == "LPUSH":
            self.lists.setdefault(args[1], []).insert(0, args[2]); return len(self.lists[args[1]])
        if cmd == "LTRIM":
            k, s, e = args[1], int(args[2]), int(args[3]); self.lists[k] = self.lists.get(k, [])[s:e+1]; return "OK"
        if cmd == "SADD":
            self.sets.setdefault(args[1], set()).add(args[2]); return 1
        if cmd == "SMEMBERS":
            return list(self.sets.get(args[1], set()))
        if cmd == "LRANGE":
            k, s, e = args[1], int(args[2]), int(args[3]); return self.lists.get(k, [])[s:e+1]
        return None


@pytest.fixture
def upstash(monkeypatch):
    from storage import UpstashBackend
    be = UpstashBackend("https://fake.upstash.io", "tok")
    fake = _FakeRedis()
    monkeypatch.setattr(be, "_cmd", lambda *args: fake.run(args))
    monkeypatch.setattr(be, "_pipeline", lambda *cmds: [fake.run(c) for c in cmds])
    return be


def _rec(session, cost=0.001):
    return build_record(
        session=session, provider_id="groq", provider_name="Groq", region="us",
        tier="tier_fast", input_tokens=1, output_tokens=1, cost_usd=cost,
        saved_usd=0.0, latency_ms=10, success=True)


def test_upstash_write_read_roundtrip(upstash):
    upstash.write(_rec("s_1"))
    got = upstash.read(session="s_1", limit=10)
    assert len(got) == 1 and got[0]["provider_id"] == "groq"


def test_upstash_session_isolation(upstash):
    upstash.write(_rec("s_alice")); upstash.write(_rec("s_alice")); upstash.write(_rec("s_bob"))
    assert len(upstash.read(session="s_alice", limit=10)) == 2
    assert len(upstash.read(session="s_bob", limit=10)) == 1


def test_upstash_unscoped_read_merges_sessions(upstash):
    upstash.write(_rec("s_alice")); upstash.write(_rec("s_bob"))
    allrecs = upstash.read(session=None, limit=10)
    assert len(allrecs) == 2


def test_upstash_sanitizes_on_write(upstash):
    upstash.write({"session": "s_x", "provider_id": "groq", "api_key": "sk-LEAK"})
    got = upstash.read(session="s_x", limit=10)
    assert "api_key" not in got[0]


def test_upstash_cmd_failure_is_silent(monkeypatch):
    from storage import UpstashBackend
    be = UpstashBackend("https://fake.upstash.io", "tok")
    monkeypatch.setattr(be, "_cmd", lambda *args: None)  # simulate outage
    monkeypatch.setattr(be, "_pipeline", lambda *cmds: [None] * len(cmds))
    be.write(_rec("s_1"))                # must not raise
    assert be.read(session="s_1", limit=10) == []


# ─── Upstash REST layer: the real _cmd over a mocked transport ────────────────

import httpx

UPSTASH_URL = "https://fake-db.upstash.io"
UPSTASH_TOKEN = "upstash-test-FAKE-token"


class _RestRedis:
    """Upstash REST semantics over _FakeRedis.

    - POST /          a JSON command array → {"result": ...}
    - POST /pipeline  a JSON array of command arrays → [{"result"|"error"}, ...]

    `fail` maps a command name to a failure mode. "500" fails that command (an
    HTTP 500 for a single command, a per-item error inside a pipeline, which is
    how Upstash reports it). "timeout"/"connect" fail the whole request.
    """
    def __init__(self, fail=None):
        self.redis = _FakeRedis()
        self.fail = fail or {}

    def __call__(self, request):
        body = json.loads(request.content)
        pipeline = request.url.path.endswith("/pipeline")
        cmds = body if pipeline else [body]
        for cmd in cmds:
            mode = self.fail.get(cmd[0])
            if mode == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            if mode == "connect":
                raise httpx.ConnectError("unreachable", request=request)
        if not pipeline:
            if self.fail.get(body[0]) == "500":
                return httpx.Response(500, json={"error": "ERR internal"})
            return httpx.Response(200, json={"result": self.redis.run(body)})
        out = []
        for cmd in cmds:
            if self.fail.get(cmd[0]) == "500":
                out.append({"error": "ERR internal"})
            else:
                out.append({"result": self.redis.run(cmd)})
        return httpx.Response(200, json=out)


def _bodies(http):
    return [json.loads(r.content) for r in http.requests]


def _commands(http):
    """Every Redis command sent, flattening pipelined requests."""
    out = []
    for r in http.requests:
        body = json.loads(r.content)
        out.extend(body if r.url.path.endswith("/pipeline") else [body])
    return out


def test_upstash_write_issues_lpush_ltrim_sadd(mock_http):
    rest = _RestRedis()
    http = mock_http(rest)
    be = UpstashBackend(UPSTASH_URL + "/", UPSTASH_TOKEN)        # trailing slash trimmed
    rec = _rec("s_abc")
    rec["api_key"] = "sk-test-FAKE-LEAK"                          # must be sanitized away
    be.write(rec)
    # one write == one pipelined request
    assert len(http.requests) == 1
    req = http.requests[0]
    assert req.method == "POST"
    assert str(req.url) == UPSTASH_URL + "/pipeline"
    assert req.headers["authorization"] == f"Bearer {UPSTASH_TOKEN}"
    cmds = _commands(http)
    key = "history:s_abc"
    assert [c[0] for c in cmds] == ["LPUSH", "LTRIM", "LPUSH", "LTRIM", "SADD",
                                    "INCRBYFLOAT", "EXPIRE"]          # + monthly spend (ST3)
    assert cmds[0][1] == key
    stored = json.loads(cmds[0][2])
    assert "api_key" not in stored and set(stored) <= ALLOWED_FIELDS
    assert cmds[1] == ["LTRIM", key, 0, UpstashBackend.MAX_PER_SESSION - 1]
    assert UpstashBackend.MAX_PER_SESSION == 5000
    assert cmds[2] == ["LPUSH", "history:recent", cmds[0][2]]
    assert cmds[3] == ["LTRIM", "history:recent", 0, UpstashBackend.MAX_RECENT - 1]
    assert cmds[4] == ["SADD", "history:sessions", key]
    spend_key = f"spend:s_abc:{stored['timestamp'][:7]}"
    assert cmds[5] == ["INCRBYFLOAT", spend_key, stored["cost_usd"]]
    assert cmds[6] == ["EXPIRE", spend_key, UpstashBackend.SPEND_TTL_S]
    # short default timeout (UPSTASH_TIMEOUT_S, 2s)
    assert http.timeouts == [2.0]


def test_upstash_ltrim_caps_list(mock_http, monkeypatch):
    rest = _RestRedis()
    mock_http(rest)
    monkeypatch.setattr(UpstashBackend, "MAX_PER_SESSION", 3)
    be = UpstashBackend(UPSTASH_URL, UPSTASH_TOKEN)
    for i in range(5):
        be.write(_rec("s_cap", cost=float(i)))
    assert len(rest.redis.lists["history:s_cap"]) == 3
    assert [r["cost_usd"] for r in be.read("s_cap", 10)] == [4.0, 3.0, 2.0]   # newest kept


def _rec_at(session, ts, cost=0.0):
    r = _rec(session, cost)
    r["timestamp"] = ts
    return r


def test_upstash_scoped_read_uses_lrange_with_limit(mock_http):
    rest = _RestRedis()
    http = mock_http(rest)
    be = UpstashBackend(UPSTASH_URL, UPSTASH_TOKEN)
    for i in range(4):
        be.write(_rec_at("s_a", f"2026-01-0{i+1}T00:00:00+00:00", cost=float(i)))
    http.requests.clear()
    got = be.read("s_a", 2)
    assert _bodies(http) == [["LRANGE", "history:s_a", 0, 1]]
    assert [r["cost_usd"] for r in got] == [3.0, 2.0]                 # newest first


def test_upstash_unscoped_read_merges_newest_first_with_limit(mock_http):
    rest = _RestRedis()
    http = mock_http(rest)
    be = UpstashBackend(UPSTASH_URL, UPSTASH_TOKEN)
    be.write(_rec_at("s_a", "2026-01-01T00:00:00+00:00", 1))
    be.write(_rec_at("s_b", "2026-01-02T00:00:00+00:00", 2))
    be.write(_rec_at("s_a", "2026-01-03T00:00:00+00:00", 3))
    be.write(_rec_at("s_c", "2026-01-04T00:00:00+00:00", 4))
    http.requests.clear()
    got = be.read(None, 3)
    assert [r["cost_usd"] for r in got] == [4, 3, 2]
    # one request on the global recent list, however many sessions exist
    assert _bodies(http) == [["LRANGE", "history:recent", 0, 2]]


def test_upstash_unscoped_read_fallback_is_pipelined(mock_http):
    # Data written before history:recent existed: per-session lists + index only.
    rest = _RestRedis()
    http = mock_http(rest)
    for s_, ts, cost in [("s_a", "2026-01-01", 1), ("s_b", "2026-01-03", 3), ("s_c", "2026-01-02", 2)]:
        key = f"history:{s_}"
        rest.redis.lists[key] = [json.dumps(_rec_at(s_, ts + "T00:00:00+00:00", cost))]
        rest.redis.sets.setdefault("history:sessions", set()).add(key)
    be = UpstashBackend(UPSTASH_URL, UPSTASH_TOKEN)
    got = be.read(None, 2)
    assert [r["cost_usd"] for r in got] == [3, 2]
    # LRANGE recent (empty), SMEMBERS, then ONE pipeline for all sessions
    assert [r.url.path for r in http.requests] == ["/", "/", "/pipeline"]
    assert sorted(c[1] for c in _commands(http)[2:]) == ["history:s_a", "history:s_b", "history:s_c"]


def test_upstash_read_skips_bad_json(mock_http):
    rest = _RestRedis()
    mock_http(rest)
    rest.redis.lists["history:s_x"] = ["{not json", json.dumps(_rec("s_x", 5.0)), None]
    be = UpstashBackend(UPSTASH_URL, UPSTASH_TOKEN)
    got = be.read("s_x", 10)
    assert [r["cost_usd"] for r in got] == [5.0]


@pytest.mark.parametrize("mode", ["500", "timeout", "connect"])
def test_upstash_failures_swallowed(mock_http, mode):
    rest = _RestRedis(fail={c: mode for c in ("LPUSH", "LTRIM", "SADD", "SMEMBERS", "LRANGE")})
    http = mock_http(rest)
    be = UpstashBackend(UPSTASH_URL, UPSTASH_TOKEN)
    be.write(_rec("s_1"))                                   # must not raise
    assert len(http.requests) == 1                          # one pipelined request
    assert be.read("s_1", 10) == []
    assert be.read(None, 10) == []
    assert be._cmd("PING") is None


def test_upstash_partial_failure_keeps_other_commands(mock_http):
    rest = _RestRedis(fail={"SADD": "500"})
    mock_http(rest)
    be = UpstashBackend(UPSTASH_URL, UPSTASH_TOKEN)
    be.write(_rec("s_1"))
    assert len(be.read("s_1", 10)) == 1                     # LPUSH landed
    assert len(be.read(None, 10)) == 1                      # unscoped reads use history:recent, not the index


def test_upstash_cmd_missing_result_is_none(mock_http):
    mock_http(lambda req: httpx.Response(200, json={"error": "WRONGTYPE"}))
    assert UpstashBackend(UPSTASH_URL, UPSTASH_TOKEN)._cmd("GET", "k") is None


# ─── backend selection edge cases ─────────────────────────────────────────────

@pytest.mark.parametrize("present", ["UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"])
def test_backend_selection_needs_both_env_vars(monkeypatch, present):
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.setenv(present, "x")
    assert get_backend().name == "jsonl"


def test_backend_selection_empty_values_fall_back(monkeypatch):
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "tok")
    assert get_backend().name == "jsonl"


def test_backend_selection_upstash_carries_config(monkeypatch):
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", UPSTASH_URL + "/")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", UPSTASH_TOKEN)
    be = get_backend()
    assert isinstance(be, UpstashBackend)
    assert be.base == UPSTASH_URL and be.token == UPSTASH_TOKEN


# ─── JSONL ordering / limit / corruption ──────────────────────────────────────

def test_jsonl_read_newest_first_limit_and_bad_lines(tmp_path):
    f = tmp_path / "h.jsonl"
    be = JSONLBackend(str(f))
    for i in range(3):
        be.write(_rec("s_1", cost=float(i)))
    with open(f, "a") as fh:
        fh.write("{truncated\n")
    be.write(_rec("s_1", cost=3.0))
    assert [r["cost_usd"] for r in be.read("s_1", 10)] == [3.0, 2.0, 1.0, 0.0]
    assert [r["cost_usd"] for r in be.read(None, 2)] == [3.0, 2.0]


def test_jsonl_missing_file_reads_empty(tmp_path):
    assert JSONLBackend(str(tmp_path / "nope.jsonl")).read(None, 10) == []


# ─── privacy across the history layer ─────────────────────────────────────────

def test_session_key_hashed_in_upstash_key(mock_http, monkeypatch):
    import history
    rest = _RestRedis()
    http = mock_http(rest)
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", UPSTASH_URL)
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", UPSTASH_TOKEN)
    import storage
    history.log_decision({"winner": {"provider_id": "groq"}}, session="alice@example.com")
    assert storage.flush_writes(timeout=5.0)                # Upstash writes are backgrounded
    blob = json.dumps(_bodies(http))
    assert "alice" not in blob
    assert f"history:{history.normalize_session('alice@example.com')}" in blob
    assert history.read_history(session="alice@example.com")[0]["provider_id"] == "groq"


# ─── routing survives an Upstash outage ───────────────────────────────────────

@pytest.mark.parametrize("mode", ["timeout", "connect", "500"])
def test_route_completion_survives_upstash_outage(route_env, mock_http, monkeypatch, mode):
    import asyncio
    from client import CompletionResult
    rest = _RestRedis(fail={c: mode for c in ("LPUSH", "LTRIM", "SADD", "SMEMBERS", "LRANGE")})
    http = mock_http(rest)
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", UPSTASH_URL)
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", UPSTASH_TOKEN)

    async def fake_call(provider, model, api_key, messages, system_prompt, max_tokens):
        return CompletionResult(text="still routed", input_tokens_used=3, output_tokens_used=2,
                                latency_ms=5, model_id="m", raw_response={})
    s = route_env.server
    monkeypatch.setattr(s, "call_provider", fake_call)
    out = json.loads(asyncio.run(s.arbitrage_route_completion(s.RouteCompletionInput(
        messages=[{"role": "user", "content": "hi"}], api_keys={"groq": "sk-test-FAKE"},
        session="outage", health_aware=True))))
    assert out["response"] == "still routed"
    assert out["routing"]["provider_id"] == "groq"
    import storage
    assert storage.flush_writes(timeout=5.0)                # the success write is backgrounded
    cmds = _commands(http)
    # health read (global recent list), the success write, and the budget read all hit Upstash
    assert ["LRANGE", "history:recent", 0, 49] in cmds
    assert any(c[0] == "LPUSH" for c in cmds)
    assert any(c[0] == "LRANGE" and c[1].startswith("history:s_") for c in cmds)
    assert "budget" not in out
