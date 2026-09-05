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
