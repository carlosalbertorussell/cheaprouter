"""ST5 end to end: caller errors never poison health, and history reads are
scoped to the caller unless the operator opts in to a global view."""

import asyncio
import json

import httpx
import pytest

import history
from client import CompletionResult


def _run(c):
    return asyncio.run(c)


def _status_error(code):
    req = httpx.Request("POST", "https://provider.invalid/v1/chat")
    return httpx.HTTPStatusError(f"Client error '{code}'", request=req,
                                 response=httpx.Response(code, request=req))


def _route(s, session, **kw):
    return json.loads(_run(s.arbitrage_route_completion(s.RouteCompletionInput(
        messages=[{"role": "user", "content": "hi"}], tier="tier_fast",
        api_keys={"groq": "sk-test-FAKE", "gemini": "sk-test-FAKE"},
        session=session, max_failover=0, **kw))))


def _install(s, monkeypatch, fail_with=None, fail_on="groq"):
    seen = []

    async def fake(provider, model, api_key, messages, system_prompt, max_tokens):
        seen.append(provider.name)
        if fail_with is not None and provider.name.lower().startswith(fail_on):
            raise fail_with()
        return CompletionResult(text="ok", input_tokens_used=3, output_tokens_used=2,
                                latency_ms=1, model_id=model.model_id, raw_response={})
    monkeypatch.setattr(s, "call_provider", fake)
    return seen


def _winner(s):
    return _route(s, "probe", health_aware=True)["routing"]["provider_id"]


# ─── health via the real routing path ─────────────────────────────────────────

def test_fifty_bad_key_calls_from_one_session_do_not_deprioritize(route_env, monkeypatch):
    s = route_env.server
    _install(s, monkeypatch, fail_with=lambda: _status_error(401))
    for _ in range(50):
        out = _route(s, "mallory")
        assert out["attempts"][0]["outcome"] == "error"
    recs = history.read_history(limit=50)
    assert all(r["transient"] is False and r["success"] is False for r in recs)
    health = json.loads(_run(s.arbitrage_provider_health(s.ProviderHealthInput())))
    assert "groq" not in health["providers"] and health["unhealthy"] == []
    _install(s, monkeypatch)
    assert _winner(s) == "groq"                      # still the cheapest choice


@pytest.mark.parametrize("code", [400, 403, 404])
def test_bad_requests_do_not_deprioritize(route_env, monkeypatch, code):
    s = route_env.server
    _install(s, monkeypatch, fail_with=lambda: _status_error(code))
    for i in range(10):
        _route(s, f"caller-{i}")                     # even across many sessions
    _install(s, monkeypatch)
    assert _winner(s) == "groq"


@pytest.mark.parametrize("make", [lambda: _status_error(503), lambda: _status_error(429),
                                  lambda: httpx.ReadTimeout("slow"),
                                  lambda: httpx.ConnectError("down")])
def test_real_outage_across_sessions_deprioritizes(route_env, monkeypatch, make):
    s = route_env.server
    _install(s, monkeypatch, fail_with=make)
    for i in range(6):
        _route(s, f"caller-{i}")
    health = json.loads(_run(s.arbitrage_provider_health(s.ProviderHealthInput())))
    assert health["unhealthy"] == ["groq"]
    # once groq turned unhealthy, later callers were routed elsewhere first
    assert health["providers"]["groq"]["failing_sessions"] >= 2
    _install(s, monkeypatch)
    out = _route(s, "probe", health_aware=True)
    assert out["routing"]["provider_id"] != "groq"
    assert "groq" in out["deprioritized_providers"]


def test_one_session_429_storm_cannot_deprioritize(route_env, monkeypatch):
    s = route_env.server
    _install(s, monkeypatch, fail_with=lambda: _status_error(429))
    for _ in range(30):
        _route(s, "spammer")
    health = json.loads(_run(s.arbitrage_provider_health(s.ProviderHealthInput())))
    import health as h
    assert health["providers"]["groq"]["samples"] == h.HEALTH_MAX_PER_SESSION
    assert health["unhealthy"] == []
    _install(s, monkeypatch)
    assert _winner(s) == "groq"


# ─── scoped reads ─────────────────────────────────────────────────────────────

def _log(session, pid="groq", cost=0.1):
    history.log_decision({"winner": {"provider_id": pid, "provider_name": pid, "region": "us"},
                          "tier": "tier_fast", "savings": {"saved_usd": 0.01}},
                         session=session, actual_cost_usd=cost)


@pytest.fixture
def three_sessions(route_env, monkeypatch):
    monkeypatch.delenv("ARBITRAGE_ALLOW_GLOBAL_READS", raising=False)
    _log("alice", cost=1.0)
    _log("bob", cost=2.0)
    _log(None, cost=4.0)            # anon
    return route_env.server


def test_history_without_session_sees_only_anon(three_sessions):
    s = three_sessions
    out = json.loads(_run(s.arbitrage_get_history(s.GetHistoryInput())))
    assert out["scope"] == "anon"
    assert [r["session"] for r in out["records"]] == ["anon"]
    assert out["summary"]["total_records"] == 1
    summ = json.loads(_run(s.arbitrage_get_history(s.GetHistoryInput(summary_only=True))))
    assert summ["total_records"] == 1 and summ["total_cost_usd"] == 4.0


def test_spend_report_without_session_sees_only_anon(three_sessions):
    s = three_sessions
    out = json.loads(_run(s.arbitrage_spend_report(s.SpendReportInput())))
    assert out["scope"] == "anon" and out["total_cost_usd"] == 4.0


def test_no_session_never_shows_other_session_hashes(three_sessions):
    s = three_sessions
    raw = _run(s.arbitrage_get_history(s.GetHistoryInput(limit=100)))
    for other in ("alice", "bob"):
        assert history.normalize_session(other) not in raw


def test_scoped_reads_unchanged(three_sessions):
    s = three_sessions
    out = json.loads(_run(s.arbitrage_get_history(s.GetHistoryInput(session="alice"))))
    assert out["scope"] == "session"
    assert [r["session"] for r in out["records"]] == [history.normalize_session("alice")]
    rep = json.loads(_run(s.arbitrage_spend_report(s.SpendReportInput(session="bob"))))
    assert rep["total_cost_usd"] == 2.0 and rep["scope"] == "session"


@pytest.mark.parametrize("flag", ["1", "true", "yes"])
def test_operator_opt_in_returns_all_sessions(three_sessions, monkeypatch, flag):
    s = three_sessions
    monkeypatch.setenv("ARBITRAGE_ALLOW_GLOBAL_READS", flag)
    out = json.loads(_run(s.arbitrage_get_history(s.GetHistoryInput())))
    assert out["scope"] == "all_sessions" and len(out["records"]) == 3
    rep = json.loads(_run(s.arbitrage_spend_report(s.SpendReportInput())))
    assert rep["scope"] == "all_sessions" and rep["total_cost_usd"] == 7.0
    # a token still scopes to that token even with the opt-in on
    mine = json.loads(_run(s.arbitrage_get_history(s.GetHistoryInput(session="alice"))))
    assert mine["scope"] == "session" and len(mine["records"]) == 1


@pytest.mark.parametrize("flag", ["0", "", "no", "false"])
def test_opt_in_needs_an_explicit_yes(three_sessions, monkeypatch, flag):
    s = three_sessions
    monkeypatch.setenv("ARBITRAGE_ALLOW_GLOBAL_READS", flag)
    out = json.loads(_run(s.arbitrage_get_history(s.GetHistoryInput())))
    assert out["scope"] == "anon" and len(out["records"]) == 1


def test_health_still_reads_every_session_internally(three_sessions):
    # Health is an internal aggregate: it scores all sessions' records even
    # though the caller-facing history tool would show only one.
    import health
    snap = health.provider_health()
    assert snap["groq"]["samples"] == 3 and snap["groq"]["sessions"] == 3


def test_empty_summary_and_report_still_carry_scope(route_env, monkeypatch):
    monkeypatch.delenv("ARBITRAGE_ALLOW_GLOBAL_READS", raising=False)
    s = route_env.server
    summ = json.loads(_run(s.arbitrage_get_history(s.GetHistoryInput(summary_only=True))))
    assert summ == {"total_records": 0, "message": "No history found.", "scope": "anon"}
    rep = json.loads(_run(s.arbitrage_spend_report(s.SpendReportInput())))
    assert rep == {"total_records": 0, "message": "No spend in range.", "scope": "anon"}
