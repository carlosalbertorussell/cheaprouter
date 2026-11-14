"""route_completion / estimate_cost money paths with hand-computed numbers,
history attribution during failover, and budget alerts (S1.5)."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import history
from client import CompletionResult
from storage import JSONLBackend, build_record

FAKE = "sk-test-FAKE"


def _set_prices(t):
    """groq/openai tier_fast at round numbers so costs can be computed by hand."""
    g = t["providers"]["groq"]["models"]["tier_fast"]
    g.update(input_price_per_1m=1.0, output_price_per_1m=2.0)
    g.pop("cached_input_price_per_1m", None)
    o = t["providers"]["openai"]["models"]["tier_fast"]
    o.update(input_price_per_1m=3.0, output_price_per_1m=4.0, cached_input_price_per_1m=0.5)
    m = t["providers"]["mistral"]["models"]["tier_fast"]
    m.update(input_price_per_1m=2.0, output_price_per_1m=3.0)
    m.pop("cached_input_price_per_1m", None)


@pytest.fixture
def env(route_env):
    route_env.write_table(_set_prices)
    return route_env


def _run(coro):
    return asyncio.run(coro)


def _estimate(server, **kw):
    base = dict(tier="tier_fast", input_tokens=1_000_000, output_tokens=500_000,
                api_keys={"groq": FAKE, "openai": FAKE})
    base.update(kw)
    return json.loads(_run(server.arbitrage_estimate_cost(server.EstimateCostInput(**base))))


def _route(server, **kw):
    base = dict(messages=[{"role": "user", "content": "hi"}], tier="tier_fast",
                api_keys={"groq": FAKE}, health_aware=False)
    base.update(kw)
    return json.loads(_run(server.arbitrage_route_completion(server.RouteCompletionInput(**base))))


def _fake_call(responses):
    """responses: list of CompletionResult | Exception, consumed in call order."""
    calls = []
    async def fake(provider, model, api_key, messages, system_prompt, max_tokens):
        calls.append(provider.name)
        r = responses[len(calls) - 1]
        if isinstance(r, Exception):
            raise r
        return r
    fake.calls = calls
    return fake


def _ok(in_tok, out_tok, text="ok"):
    return CompletionResult(text=text, input_tokens_used=in_tok, output_tokens_used=out_tok,
                            latency_ms=42, model_id="served", raw_response={})


def _http_429():
    req = httpx.Request("POST", "https://x")
    return httpx.HTTPStatusError("rate limited", request=req, response=httpx.Response(429, request=req))


# ─── exact savings ────────────────────────────────────────────────────────────

def test_estimate_exact_costs_and_savings(env):
    out = _estimate(env.server)
    w = out["winner"]
    # groq: 1M * $1/1M + 0.5M * $2/1M = 1.0 + 1.0
    assert w["provider_id"] == "groq"
    assert (w["input_cost_usd"], w["output_cost_usd"], w["total_cost_usd"]) == (1.0, 1.0, 2.0)
    # openai: 1M * $3 + 0.5M * $4 = 3.0 + 2.0 = 5.0 → saved 3.0 (60%)
    assert out["savings"] == {"saved_usd": 3.0, "saved_pct": 60.0,
                              "vs_provider": "OpenAI", "vs_cost_usd": 5.0}


def test_estimate_exact_cached_input_split(env):
    out = _estimate(env.server, cached_input_tokens=400_000)
    by = {e["provider_id"]: e for e in out["all_estimates"]}
    # openai: fresh 600k * $3 = 1.8, cached 400k * $0.5 = 0.2 → input 2.0; output 2.0
    o = by["openai"]
    assert (o["input_cost_usd"], o["output_cost_usd"], o["total_cost_usd"]) == (2.0, 2.0, 4.0)
    assert o["cached_input_tokens"] == 400_000 and o["cache_supported"] is True
    # groq has no cache price → cached tokens billed at the full $1 rate
    g = by["groq"]
    assert (g["input_cost_usd"], g["total_cost_usd"]) == (1.0, 2.0)
    assert g["cache_supported"] is False
    assert out["savings"]["saved_usd"] == 2.0 and out["savings"]["saved_pct"] == 50.0


def test_estimate_no_savings_with_single_provider(env):
    out = _estimate(env.server, api_keys={"groq": FAKE})
    assert out["winner"]["provider_id"] == "groq"
    assert out["savings"] is None


def test_route_actual_cost_from_actual_tokens(env, monkeypatch):
    monkeypatch.setattr(env.server, "call_provider", _fake_call([_ok(1000, 500)]))
    out = _route(env.server)
    # 1000 * $1/1M + 500 * $2/1M = 0.001 + 0.001
    assert out["cost"]["actual_cost_usd"] == 0.002
    assert out["cost"]["actual_input_tokens"] == 1000
    assert out["cost"]["actual_output_tokens"] == 500
    rec = history.read_history(limit=5)[0]
    assert rec["cost_usd"] == pytest.approx(0.002)
    assert rec["input_tokens"] == 1000 and rec["output_tokens"] == 500
    assert rec["latency_ms"] == 42


def test_route_actual_cost_uses_provider_that_succeeded(env, monkeypatch):
    # groq fails over to openai: cost must be billed at OPENAI's rates
    fake = _fake_call([_http_429(), _ok(1000, 500)])
    monkeypatch.setattr(env.server, "call_provider", fake)
    out = _route(env.server, api_keys={"groq": FAKE, "openai": FAKE}, max_failover=1)
    assert fake.calls == ["Groq", "OpenAI"]
    # 1000 * $3/1M + 500 * $4/1M = 0.003 + 0.002
    assert out["cost"]["actual_cost_usd"] == 0.005
    assert out["routing"]["provider_id"] == "openai"


# ─── actual savings (ST4) ─────────────────────────────────────────────────────
# Prices: groq 1/2 (no cache), mistral 2/3 (no cache), openai 3/4 (cache 0.5).
# The request is "hi" (pre-flight estimate: 5 input, 500 output tokens); every
# provider answers with 1000 input / 500 output, so the actual figures differ
# from the pre-flight ones.

def test_route_savings_at_actual_tokens(env, monkeypatch):
    monkeypatch.setattr(env.server, "call_provider", _fake_call([_ok(1000, 500)]))
    out = _route(env.server, api_keys={"groq": FAKE, "openai": FAKE})
    # groq 0.001 + 0.001 = 0.002; openai 0.003 + 0.002 = 0.005 → saved 0.003 (60%)
    assert out["cost"]["actual_cost_usd"] == 0.002
    assert out["cost"]["savings"] == {"saved_usd": 0.003, "saved_pct": 60.0,
                                      "vs_provider": "OpenAI", "vs_cost_usd": 0.005,
                                      "vs_provider_id": "openai"}
    # pre-flight: groq 5*1e-6 + 500*2e-6 = 0.001005; openai 0.000015 + 0.002 = 0.002015
    assert out["cost"]["estimated_savings"]["saved_usd"] == 0.00101
    rec = history.read_history(limit=1)[0]
    assert rec["saved_usd"] == pytest.approx(0.003)


def test_route_savings_after_failover_to_baseline_is_zero(env, monkeypatch):
    # groq fails, openai (the most expensive eligible provider) answers: nothing saved.
    monkeypatch.setattr(env.server, "call_provider", _fake_call([_http_429(), _ok(1000, 500)]))
    out = _route(env.server, api_keys={"groq": FAKE, "openai": FAKE}, max_failover=1)
    assert out["routing"]["provider_id"] == "openai"
    assert out["cost"]["savings"] == {"saved_usd": 0.0, "saved_pct": 0.0,
                                      "vs_provider": "OpenAI", "vs_cost_usd": 0.005,
                                      "vs_provider_id": "openai"}


def test_route_savings_after_failover_to_middle_provider(env, monkeypatch):
    monkeypatch.setattr(env.server, "call_provider", _fake_call([_http_429(), _ok(1000, 500)]))
    out = _route(env.server, api_keys={"groq": FAKE, "openai": FAKE, "mistral": FAKE},
                 max_failover=1)
    assert out["routing"]["provider_id"] == "mistral"
    # mistral 1000*2e-6 + 500*3e-6 = 0.0035 vs openai 0.005 → 0.0015 (30%)
    assert out["cost"]["actual_cost_usd"] == 0.0035
    s = out["cost"]["savings"]
    assert (s["saved_usd"], s["saved_pct"], s["vs_provider_id"]) == (0.0015, 30.0, "openai")


def test_route_actual_cost_applies_cache_discount(env, monkeypatch):
    monkeypatch.setattr(env.server, "call_provider", _fake_call([_ok(1000, 500)]))
    out = _route(env.server, api_keys={"openai": FAKE}, cached_input_tokens=400)
    # openai: fresh 600*3e-6 = 0.0018 + cached 400*0.5e-6 = 0.0002 + out 0.002 = 0.004
    assert out["cost"]["actual_cost_usd"] == 0.004
    assert out["cost"]["actual_cached_input_tokens"] == 400
    assert out["cost"]["savings"] is None          # single provider: nothing to compare
    assert history.read_history(limit=1)[0]["cost_usd"] == pytest.approx(0.004)


def test_route_cache_aware_savings_baseline(env, monkeypatch):
    monkeypatch.setattr(env.server, "call_provider", _fake_call([_ok(1000, 500)]))
    out = _route(env.server, api_keys={"groq": FAKE, "openai": FAKE}, cached_input_tokens=400)
    # groq has no cache price → 0.002; openai cache-aware 0.004 → saved 0.002 (50%)
    assert out["cost"]["actual_cost_usd"] == 0.002
    s = out["cost"]["savings"]
    assert (s["saved_usd"], s["saved_pct"], s["vs_cost_usd"]) == (0.002, 50.0, 0.004)


def test_cached_tokens_clamped_to_actual_input(env, monkeypatch):
    monkeypatch.setattr(env.server, "call_provider", _fake_call([_ok(100, 0)]))
    out = _route(env.server, api_keys={"openai": FAKE}, cached_input_tokens=400)
    # only 100 input tokens came back, all of them cached: 100 * 0.5e-6
    assert out["cost"]["actual_cached_input_tokens"] == 100
    assert out["cost"]["actual_cost_usd"] == 0.00005


def test_failed_attempts_log_zero_cost_and_savings(env, monkeypatch):
    monkeypatch.setattr(env.server, "call_provider", _fake_call([_http_429(), _ok(1000, 500)]))
    _route(env.server, api_keys={"groq": FAKE, "openai": FAKE}, max_failover=1, session="zed")
    ok, fail = history.read_history(limit=10, session="zed")
    assert (fail["provider_id"], fail["cost_usd"], fail["saved_usd"]) == ("groq", 0.0, 0.0)
    assert (ok["provider_id"], ok["cost_usd"], ok["saved_usd"]) == ("openai", 0.005, 0.0)
    summ = history.history_summary(limit=10, session="zed")
    assert summ["total_cost_usd"] == 0.005 and summ["total_saved_usd"] == 0.0
    rep = history.spend_report(session="zed")
    assert rep["total_cost_usd"] == 0.005


def test_all_failed_attempts_add_no_spend(env, monkeypatch):
    monkeypatch.setattr(env.server, "call_provider", _fake_call([_http_429(), _http_429()]))
    _route(env.server, api_keys={"groq": FAKE, "openai": FAKE}, max_failover=1, session="yan")
    summ = history.history_summary(limit=10, session="yan")
    assert summ["failed"] == 2
    assert summ["total_cost_usd"] == 0.0 and summ["total_saved_usd"] == 0.0


# ─── history attribution during failover ──────────────────────────────────────

def test_failover_history_attributes_each_attempt(env, monkeypatch):
    monkeypatch.setattr(env.server, "call_provider", _fake_call([_http_429(), _ok(10, 5)]))
    out = _route(env.server, api_keys={"groq": FAKE, "openai": FAKE},
                 max_failover=1, session="alice")
    assert out["routing"]["failed_over"] is True
    recs = history.read_history(limit=10, session="alice")     # newest first
    assert [(r["provider_id"], r["provider_name"], r["success"]) for r in recs] == [
        ("openai", "OpenAI", True), ("groq", "Groq", False)]
    fail, ok = recs[1], recs[0]
    assert fail["error_class"] == "rate limited"
    assert fail["region"] == "us" and fail["tier"] == "tier_fast"
    assert ok["error_class"] is None and ok["input_tokens"] == 10
    assert all(r["session"] == history.normalize_session("alice") for r in recs)


def test_all_fail_logs_one_failure_per_attempt(env, monkeypatch):
    monkeypatch.setattr(env.server, "call_provider", _fake_call([_http_429(), _http_429()]))
    out = _route(env.server, api_keys={"groq": FAKE, "openai": FAKE}, max_failover=5)
    assert out["error"] == "All attempted providers failed."
    recs = history.read_history(limit=10)
    assert sorted(r["provider_id"] for r in recs) == ["groq", "openai"]
    assert not any(r["success"] for r in recs)


def test_non_transient_failure_logged_once(env, monkeypatch):
    req = httpx.Request("POST", "https://x")
    e401 = httpx.HTTPStatusError("unauthorized", request=req, response=httpx.Response(401, request=req))
    monkeypatch.setattr(env.server, "call_provider", _fake_call([e401]))
    out = _route(env.server, api_keys={"groq": FAKE, "openai": FAKE}, max_failover=3)
    assert [a["outcome"] for a in out["attempts"]] == ["error"]
    recs = history.read_history(limit=10)
    assert [(r["provider_id"], r["success"]) for r in recs] == [("groq", False)]


# ─── budgets ──────────────────────────────────────────────────────────────────

def _spend(session, usd):
    history.log_decision({"winner": {"provider_id": "groq", "provider_name": "Groq",
                                     "region": "us"}, "tier": "tier_fast"},
                         session=session, actual_cost_usd=usd)


def test_route_budget_alert_at_80_percent(env, monkeypatch):
    history.set_budget("bob", 1.0)
    _spend("bob", 0.85)
    monkeypatch.setattr(env.server, "call_provider", _fake_call([_ok(10, 5)]))
    out = _route(env.server, session="bob")
    b = out["budget"]
    # 0.85 + this call (10*$1/1M + 5*$2/1M = 0.00002)
    assert b["spent_this_month_usd"] == pytest.approx(0.85002)
    assert b["used_pct"] == 85.0 and b["over_budget"] is False
    assert b["alert"] == "You've used 85.0% of your monthly budget."


def test_route_budget_alert_over_budget(env, monkeypatch):
    history.set_budget("carol", 1.0)
    _spend("carol", 1.5)
    monkeypatch.setattr(env.server, "call_provider", _fake_call([_ok(10, 5)]))
    out = _route(env.server, session="carol")
    assert out["budget"]["over_budget"] is True
    assert out["budget"]["alert"] == "You are over your monthly budget."


def test_route_budget_below_80_has_status_but_no_alert(env, monkeypatch):
    history.set_budget("dan", 10.0)
    _spend("dan", 1.0)
    monkeypatch.setattr(env.server, "call_provider", _fake_call([_ok(10, 5)]))
    out = _route(env.server, session="dan")
    assert out["budget"]["used_pct"] == 10.0
    assert "alert" not in out["budget"]


def test_route_without_session_or_budget_has_no_budget_block(env, monkeypatch):
    monkeypatch.setattr(env.server, "call_provider", _fake_call([_ok(10, 5), _ok(10, 5)]))
    assert "budget" not in _route(env.server)                    # no session
    assert "budget" not in _route(env.server, session="nobudget")  # session, no budget


def test_budget_excludes_previous_month_spend(env):
    history.set_budget("erin", 10.0)
    first_of_month = datetime.now(timezone.utc).replace(day=1, hour=12)
    last_month = first_of_month - timedelta(days=2)
    rec = build_record(session=history.normalize_session("erin"), provider_id="groq",
                       provider_name="Groq", region="us", tier="tier_fast", input_tokens=1,
                       output_tokens=1, cost_usd=7.0, saved_usd=0.0, latency_ms=1, success=True)
    rec["timestamp"] = last_month.isoformat()
    JSONLBackend(str(env.history)).write(rec)
    _spend("erin", 2.0)
    st = history.budget_status("erin")
    assert st["spent_this_month_usd"] == 2.0          # the $7 from last month is ignored
    assert st["used_pct"] == 20.0


def test_latest_budget_wins(env):
    history.set_budget("fay", 10.0)
    history.set_budget("fay", 20.0)
    assert history.get_budget("fay") == 20.0
    _spend("fay", 5.0)
    st = history.budget_status("fay")
    assert st["monthly_budget_usd"] == 20.0 and st["used_pct"] == 25.0


def test_budget_is_session_scoped(env):
    history.set_budget("gus", 1.0)
    _spend("hal", 5.0)
    assert history.budget_status("gus")["spent_this_month_usd"] == 0.0
    assert history.budget_status("hal") is None
