"""Budget storage and monthly spend (ST3).

Budgets live outside the capped spend history, and the month's spend is correct
however many calls a session makes. Covers both backends, legacy budget markers
written before ST3, the month rollover, marker pollution of the analytics, and
the route_completion alerts after many calls."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import history
import storage
from storage import JSONLBackend, UpstashBackend, build_record
from tests.test_storage import _RestRedis, _commands, UPSTASH_URL, UPSTASH_TOKEN

MARKER = "__budget__"


def _month(dt=None):
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m")


def _last_month_ts():
    first = datetime.now(timezone.utc).replace(day=1, hour=12, minute=0, second=0, microsecond=0)
    return (first - timedelta(days=3)).isoformat()


def _spend(session, usd, n=1, error=None):
    for _ in range(n):
        history.log_decision({"winner": {"provider_id": "groq", "provider_name": "Groq",
                                         "region": "us"}, "tier": "tier_fast"},
                             session=session, actual_cost_usd=usd, error=error)


def _legacy_marker(session, usd, ts=None):
    """A budget record exactly as set_budget wrote it before ST3."""
    rec = build_record(session=history.normalize_session(session), provider_id=MARKER,
                       provider_name=None, region=None, tier=None, input_tokens=None,
                       output_tokens=None, cost_usd=float(usd), saved_usd=None,
                       latency_ms=None, success=True)
    if ts:
        rec["timestamp"] = ts
    return rec


# ─── backends under test ──────────────────────────────────────────────────────

@pytest.fixture
def jsonl(tmp_path, monkeypatch):
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.delenv("ARBITRAGE_BUDGET_FILE", raising=False)
    monkeypatch.setenv("ARBITRAGE_HISTORY_FILE", str(tmp_path / "h.jsonl"))
    return tmp_path


@pytest.fixture
def upstash(mock_http, monkeypatch):
    rest = _RestRedis()
    http = mock_http(rest)
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", UPSTASH_URL)
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", UPSTASH_TOKEN)
    rest.http = http
    return rest


@pytest.fixture(params=["jsonl", "upstash"])
def backend(request):
    return request.getfixturevalue(request.param)


def _write_raw(backend_fixture, rec):
    """Write a record straight to the active backend (bypassing log_decision)."""
    storage.get_backend().write(rec)


# ─── 1. the budget is no longer lost (the ST1 reproduction) ───────────────────

def test_budget_survives_500_calls(backend):
    history.set_budget("u", 1.0)
    _spend("u", 0.01, n=500)
    assert history.get_budget("u") == 1.0
    st = history.budget_status("u")
    assert st is not None
    assert st["spent_this_month_usd"] == pytest.approx(5.0)
    assert st["over_budget"] is True


def test_budget_survives_list_cap(upstash, monkeypatch):
    """On Upstash LTRIM eventually deleted a marker kept in the history list."""
    monkeypatch.setattr(UpstashBackend, "MAX_PER_SESSION", 10)
    monkeypatch.setattr(UpstashBackend, "MAX_RECENT", 10)
    history.set_budget("cap", 2.0)
    _spend("cap", 0.1, n=30)
    st = history.budget_status("cap")
    assert st["monthly_budget_usd"] == 2.0
    assert st["spent_this_month_usd"] == pytest.approx(3.0)    # counter, not the 10-item list
    assert st["over_budget"] is True


def test_latest_budget_wins(backend):
    history.set_budget("w", 10.0)
    history.set_budget("w", 20.0)
    assert history.get_budget("w") == 20.0


def test_set_budget_writes_no_history_marker(backend):
    history.set_budget("nm", 5.0)
    storage.flush_writes(5.0)
    recs = storage.get_backend().read(history.normalize_session("nm"), 100)
    assert recs == []


def test_budget_is_session_scoped(backend):
    history.set_budget("a", 1.0)
    _spend("b", 3.0)
    assert history.get_budget("b") is None
    assert history.budget_status("b") is None
    assert history.budget_status("a")["spent_this_month_usd"] == 0.0


# ─── 2. monthly spend is correct regardless of call volume ────────────────────

def test_spend_correct_beyond_500_calls(backend):
    history.set_budget("big", 10.0)
    _spend("big", 0.01, n=650)
    st = history.budget_status("big")
    assert st["spent_this_month_usd"] == pytest.approx(6.5)
    assert st["used_pct"] == 65.0
    assert st["over_budget"] is False


def test_month_rollover_excludes_previous_month(jsonl):
    history.set_budget("roll", 10.0)
    old = build_record(session=history.normalize_session("roll"), provider_id="groq",
                       provider_name="Groq", region="us", tier="tier_fast", input_tokens=1,
                       output_tokens=1, cost_usd=9.0, saved_usd=0.0, latency_ms=1, success=True)
    old["timestamp"] = _last_month_ts()
    JSONLBackend().write(old)
    _spend("roll", 1.0)
    assert history.budget_status("roll")["spent_this_month_usd"] == 1.0


def test_month_rollover_upstash_uses_this_months_counter(upstash):
    sess = history.normalize_session("roll")
    last = _last_month_ts()[:7]
    upstash.redis.strings[f"spend:{sess}:{last}"] = "9.0"     # last month's spend
    history.set_budget("roll", 10.0)
    _spend("roll", 1.0)
    st = history.budget_status("roll")
    assert st["spent_this_month_usd"] == 1.0
    assert upstash.redis.strings[f"spend:{sess}:{_month()}"] in ("1.0", "1")


def test_failed_attempts_count_their_recorded_cost(backend):
    """Unchanged semantics: a record counts by its cost_usd whatever its success
    flag (route_completion logs a failed attempt with its estimated cost)."""
    history.set_budget("f", 1.0)
    _spend("f", 0.2, error="RateLimitError: 429")
    _spend("f", 0.3)
    history.log_decision({"winner": {"provider_id": "groq"}}, session="f")   # no cost at all
    assert history.budget_status("f")["spent_this_month_usd"] == pytest.approx(0.5)


# ─── 3. backward compatibility with budgets set before ST3 ────────────────────

def test_legacy_marker_fallback(backend):
    _write_raw(backend, _legacy_marker("old", 7.0))
    _spend("old", 0.7)
    assert history.get_budget("old") == 7.0
    st = history.budget_status("old")
    assert st["monthly_budget_usd"] == 7.0
    assert st["spent_this_month_usd"] == pytest.approx(0.7)    # the $7 marker is not spend


def test_dedicated_budget_overrides_legacy_marker(backend):
    _write_raw(backend, _legacy_marker("ov", 7.0))
    history.set_budget("ov", 3.0)
    assert history.get_budget("ov") == 3.0
    assert history.budget_status("ov")["monthly_budget_usd"] == 3.0


def test_legacy_marker_migrated_to_budget_key(upstash):
    _write_raw(upstash, _legacy_marker("mig", 4.0))
    storage.flush_writes(5.0)
    assert history.get_budget("mig") == 4.0
    sess = history.normalize_session("mig")
    assert float(upstash.redis.strings[f"budget:{sess}"]) == 4.0
    # survives the marker leaving the list afterwards
    upstash.redis.lists[f"history:{sess}"] = []
    assert history.get_budget("mig") == 4.0


def test_jsonl_legacy_marker_found_beyond_500_records(jsonl):
    _write_raw(jsonl, _legacy_marker("deep", 2.0))
    _spend("deep", 0.01, n=600)
    assert history.get_budget("deep") == 2.0
    assert history.budget_status("deep")["spent_this_month_usd"] == pytest.approx(6.0)


def test_upstash_pre_st3_spend_without_counter_still_counts(upstash):
    """Spend written before ST3 has no counter; the scoped history window fills in."""
    sess = history.normalize_session("pre")
    rec = build_record(session=sess, provider_id="groq", provider_name="Groq", region="us",
                       tier="tier_fast", input_tokens=1, output_tokens=1, cost_usd=0.4,
                       saved_usd=0.0, latency_ms=1, success=True)
    upstash.redis.lists[f"history:{sess}"] = [json.dumps(rec)]
    history.set_budget("pre", 1.0)
    assert history.budget_status("pre")["spent_this_month_usd"] == pytest.approx(0.4)


# ─── JSONL storage location ───────────────────────────────────────────────────

def test_jsonl_budget_file_next_to_history(jsonl):
    history.set_budget("loc", 5.0)
    f = jsonl / "h.budgets.json"
    data = json.loads(f.read_text())
    sess = history.normalize_session("loc")
    assert data[sess]["monthly_usd"] == 5.0
    assert "loc" not in f.read_text()                       # hashed key only
    assert not (jsonl / "h.jsonl").exists()                 # nothing in the spend history


def test_jsonl_budget_file_env_override(jsonl, monkeypatch):
    target = jsonl / "elsewhere" / "budgets.json"
    monkeypatch.setenv("ARBITRAGE_BUDGET_FILE", str(target))
    history.set_budget("env", 2.5)
    assert json.loads(target.read_text())[history.normalize_session("env")]["monthly_usd"] == 2.5
    assert history.get_budget("env") == 2.5


def test_jsonl_corrupt_budget_file_is_ignored(jsonl):
    (jsonl / "h.budgets.json").write_text("{not json")
    assert history.get_budget("x") is None
    history.set_budget("x", 1.0)                            # rewrites a valid file
    assert history.get_budget("x") == 1.0


# ─── Upstash request shape and counts ─────────────────────────────────────────

def test_upstash_write_pipeline_adds_month_counter(upstash):
    _spend("shape", 0.25)
    storage.flush_writes(5.0)
    reqs = upstash.http.requests
    assert len(reqs) == 1 and reqs[0].url.path == "/pipeline"
    cmds = json.loads(reqs[0].content)
    sess = history.normalize_session("shape")
    key = f"spend:{sess}:{_month()}"
    assert ["INCRBYFLOAT", key, 0.25] in cmds
    assert ["EXPIRE", key, UpstashBackend.SPEND_TTL_S] in cmds
    assert UpstashBackend.SPEND_TTL_S >= 60 * 86400
    assert [c[0] for c in cmds][:5] == ["LPUSH", "LTRIM", "LPUSH", "LTRIM", "SADD"]
    assert upstash.redis.ttls[key] == UpstashBackend.SPEND_TTL_S


def test_upstash_write_without_cost_skips_counter(upstash):
    history.log_decision({"winner": {"provider_id": "groq"}}, session="nc")
    storage.flush_writes(5.0)
    assert not any(c[0] in ("INCRBYFLOAT", "EXPIRE") for c in _commands(upstash.http))


def test_upstash_set_budget_is_one_set(upstash):
    out = history.set_budget("sb", 12.5)
    assert out == {"session_scoped": True, "monthly_budget_usd": 12.5}
    sess = history.normalize_session("sb")
    assert _commands(upstash.http) == [["SET", f"budget:{sess}", 12.5]]
    assert "sb" not in json.dumps(_commands(upstash.http)).replace(sess, "")


def test_upstash_budget_status_is_one_request(upstash):
    history.set_budget("one", 1.0)
    _spend("one", 0.1, n=3)
    storage.flush_writes(5.0)
    upstash.http.requests.clear()
    st = history.budget_status("one")
    assert st["spent_this_month_usd"] == pytest.approx(0.3)
    assert len(upstash.http.requests) == 1
    sess = history.normalize_session("one")
    cmds = json.loads(upstash.http.requests[0].content)
    assert cmds == [["GET", f"budget:{sess}"], ["GET", f"spend:{sess}:{_month()}"],
                    ["LRANGE", f"history:{sess}", 0, 499]]


def test_upstash_no_budget_is_one_request(upstash):
    _spend("none", 0.1)
    storage.flush_writes(5.0)
    upstash.http.requests.clear()
    assert history.budget_status("none") is None
    assert len(upstash.http.requests) == 1


@pytest.mark.parametrize("mode", ["500", "timeout", "connect"])
def test_upstash_budget_failures_swallowed(mock_http, monkeypatch, mode):
    rest = _RestRedis(fail={c: mode for c in ("GET", "SET", "LRANGE", "INCRBYFLOAT",
                                               "EXPIRE", "LPUSH", "LTRIM", "SADD")})
    mock_http(rest)
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", UPSTASH_URL)
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", UPSTASH_TOKEN)
    history.set_budget("down", 1.0)                         # must not raise
    _spend("down", 0.5)
    assert history.get_budget("down") is None
    assert history.budget_status("down") is None


def test_upstash_counter_failure_falls_back_to_window(mock_http, monkeypatch):
    rest = _RestRedis(fail={"INCRBYFLOAT": "500"})
    mock_http(rest)
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", UPSTASH_URL)
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", UPSTASH_TOKEN)
    history.set_budget("cf", 1.0)
    _spend("cf", 0.2, n=2)
    assert history.budget_status("cf")["spent_this_month_usd"] == pytest.approx(0.4)


# ─── 4. legacy markers never pollute analytics ────────────────────────────────

def test_legacy_markers_do_not_pollute_analytics(backend):
    _write_raw(backend, _legacy_marker("pol", 50.0))
    _spend("pol", 0.5)
    storage.flush_writes(5.0)
    summ = history.history_summary(session="pol")
    assert summ["total_records"] == 1
    assert summ["total_cost_usd"] == 0.5
    assert MARKER not in summ["provider_usage"]
    rep = history.spend_report(session="pol")
    assert rep["total_cost_usd"] == 0.5
    assert "unknown" not in rep["spend_by_provider"] and "unknown" not in rep["spend_by_tier"]
    assert all(r["provider_id"] != MARKER for r in history.read_history(50, "pol"))
    assert all(r["provider_id"] != MARKER for r in history.read_history(50))   # global too
    glob = history.spend_report()
    assert glob["total_cost_usd"] == 0.5


def test_get_history_tool_hides_legacy_markers(route_env):
    JSONLBackend().write(_legacy_marker("tool", 50.0))
    _spend("tool", 0.5)
    s = route_env.server
    out = json.loads(asyncio.run(s.arbitrage_get_history(s.GetHistoryInput(session="tool"))))
    assert [r["provider_id"] for r in out["records"]] == ["groq"]
    assert out["summary"]["total_cost_usd"] == 0.5
    rep = json.loads(asyncio.run(s.arbitrage_spend_report(s.SpendReportInput(session="tool"))))
    assert rep["total_cost_usd"] == 0.5
    health = json.loads(asyncio.run(s.arbitrage_provider_health(s.ProviderHealthInput())))
    assert MARKER not in health["providers"]


# ─── 5. route_completion alerts after many calls ──────────────────────────────

def _route(server, session, monkeypatch):
    from client import CompletionResult
    async def fake(provider, model, api_key, messages, system_prompt, max_tokens):
        return CompletionResult(text="ok", input_tokens_used=0, output_tokens_used=0,
                                latency_ms=1, model_id="m", raw_response={})
    monkeypatch.setattr(server, "call_provider", fake)
    return json.loads(asyncio.run(server.arbitrage_route_completion(server.RouteCompletionInput(
        messages=[{"role": "user", "content": "hi"}], api_keys={"groq": "sk-test-FAKE"},
        session=session, health_aware=False))))


def test_route_alert_at_80_percent_after_many_calls(route_env, monkeypatch):
    history.set_budget("many", 1.0)
    _spend("many", 0.001, n=850)
    b = _route(route_env.server, "many", monkeypatch)["budget"]
    assert b["spent_this_month_usd"] == pytest.approx(0.85)
    assert b["alert"] == "You've used 85.0% of your monthly budget."


def test_route_over_budget_after_many_calls(route_env, monkeypatch):
    history.set_budget("lots", 1.0)
    _spend("lots", 0.001, n=1200)
    b = _route(route_env.server, "lots", monkeypatch)["budget"]
    assert b["spent_this_month_usd"] == pytest.approx(1.2)
    assert b["over_budget"] is True
    assert b["alert"] == "You are over your monthly budget."


def test_route_over_budget_after_many_calls_upstash(route_env, upstash, monkeypatch):
    history.set_budget("ulots", 1.0)
    _spend("ulots", 0.002, n=600)
    b = _route(route_env.server, "ulots", monkeypatch)["budget"]
    assert b["spent_this_month_usd"] == pytest.approx(1.2)
    assert b["alert"] == "You are over your monthly budget."
