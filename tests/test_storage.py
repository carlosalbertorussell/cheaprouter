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

    def run(self, args):
        cmd = args[0].upper()
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
    be.write(_rec("s_1"))                # must not raise
    assert be.read(session="s_1", limit=10) == []


# ─── Upstash REST layer: the real _cmd over a mocked transport ────────────────

import httpx

UPSTASH_URL = "https://fake-db.upstash.io"
UPSTASH_TOKEN = "upstash-test-FAKE-token"


class _RestRedis:
    """Upstash REST semantics over _FakeRedis: POST a JSON command array, get
    {"result": ...} back. Optionally fails specific commands."""
    def __init__(self, fail=None):
        self.redis = _FakeRedis()
        self.fail = fail or {}

    def __call__(self, request):
        cmd = json.loads(request.content)
        mode = self.fail.get(cmd[0])
        if mode == "500":
            return httpx.Response(500, json={"error": "ERR internal"})
        if mode == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if mode == "connect":
            raise httpx.ConnectError("unreachable", request=request)
        return httpx.Response(200, json={"result": self.redis.run(cmd)})


def _bodies(http):
    return [json.loads(r.content) for r in http.requests]


def test_upstash_write_issues_lpush_ltrim_sadd(mock_http):
    rest = _RestRedis()
    http = mock_http(rest)
    be = UpstashBackend(UPSTASH_URL + "/", UPSTASH_TOKEN)        # trailing slash trimmed
    rec = _rec("s_abc")
    rec["api_key"] = "sk-test-FAKE-LEAK"                          # must be sanitized away
    be.write(rec)
    bodies = _bodies(http)
    key = "history:s_abc"
    assert [b[0] for b in bodies] == ["LPUSH", "LTRIM", "SADD"]
    assert bodies[0][1] == key
    stored = json.loads(bodies[0][2])
    assert "api_key" not in stored and set(stored) <= ALLOWED_FIELDS
    assert bodies[1] == ["LTRIM", key, 0, UpstashBackend.MAX_PER_SESSION - 1]
    assert UpstashBackend.MAX_PER_SESSION == 5000
    assert bodies[2] == ["SADD", "history:sessions", key]
    for r in http.requests:
        assert r.method == "POST"
        assert str(r.url) == UPSTASH_URL
        assert r.headers["authorization"] == f"Bearer {UPSTASH_TOKEN}"
    # every command uses a bounded timeout (current value pinned only as an upper bound)
    assert all(t is not None and 0 < t <= 10.0 for t in http.timeouts)


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
    cmds = _bodies(http)
    assert cmds[0] == ["SMEMBERS", "history:sessions"]
    assert sorted(c[1] for c in cmds[1:]) == ["history:s_a", "history:s_b", "history:s_c"]
    assert all(c[0] == "LRANGE" and c[2:] == [0, 2] for c in cmds[1:])


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
    assert len(http.requests) == 3                          # each command still attempted
    assert be.read("s_1", 10) == []
    assert be.read(None, 10) == []
    assert be._cmd("PING") is None


def test_upstash_partial_failure_keeps_other_commands(mock_http):
    rest = _RestRedis(fail={"SADD": "500"})
    mock_http(rest)
    be = UpstashBackend(UPSTASH_URL, UPSTASH_TOKEN)
    be.write(_rec("s_1"))
    assert len(be.read("s_1", 10)) == 1                     # LPUSH landed
    assert be.read(None, 10) == []                          # not indexed → unscoped misses it


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
    history.log_decision({"winner": {"provider_id": "groq"}}, session="alice@example.com")
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
    cmds = [b[0] for b in _bodies(http)]
    # health read (SMEMBERS), the success write, and the budget read all hit Upstash
    assert "SMEMBERS" in cmds and "LPUSH" in cmds and "LRANGE" in cmds
    assert "budget" not in out
