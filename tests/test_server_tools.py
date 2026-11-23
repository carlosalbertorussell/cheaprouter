"""Smoke + contract tests for every MCP tool in server.py that the routing tests
don't already exercise, plus input-model validation (bad tier, bounds, extras)."""

import asyncio
import json

import pytest
from pydantic import ValidationError

import history

FAKE = "sk-test-FAKE"


def _run(coro):
    return asyncio.run(coro)


def _log(pid, name, ok=True, session=None, cost=0.01):
    history.log_decision({"winner": {"provider_id": pid, "provider_name": name, "region": "us"},
                          "tier": "tier_fast", "savings": {"saved_usd": 0.001}},
                         session=session, actual_cost_usd=cost,
                         error=None if ok else "RateLimitError: 429",
                         transient=None if ok else True)


# ─── arbitrage_get_pricing ────────────────────────────────────────────────────

def test_get_pricing_with_keys(route_env):
    s = route_env.server
    out = _run(s.arbitrage_get_pricing(s.GetPricingInput(tier="tier_fast", input_tokens=2000,
                                                         output_tokens=100)))
    assert "## Token pricing — `tier_fast` tier" in out
    assert "2,000 input · 100 output tokens" in out
    assert "| # | Provider |" in out
    # one table row per provider, ranked 1..8
    rows = [l for l in out.splitlines() if l.startswith("| ") and l[2].isdigit()]
    assert [r.split("|")[1].strip() for r in rows] == [str(i) for i in range(1, 9)]
    assert "**No api_keys supplied**" in out       # no keys passed
    assert "**Overall cheapest:**" in out and "**Price spread:**" in out
    assert "Stale prices" not in out


def test_get_pricing_cheapest_among_callers_keys(route_env):
    s = route_env.server
    out = _run(s.arbitrage_get_pricing(s.GetPricingInput(
        tier="tier_fast", api_keys={"anthropic": FAKE})))
    import providers
    m = providers.PROVIDERS["anthropic"].model_for("tier_fast")
    assert f"**Cheapest (your keys):** Anthropic (`{m.model_id}`)" in out
    assert "No api_keys supplied" not in out
    assert out.count("✅") == 1                    # only the keyed provider is configured


def test_get_pricing_stale_banner_but_not_refused(route_env):
    route_env.write_table(verified_at="2020-01-01")
    s = route_env.server
    out = _run(s.arbitrage_get_pricing(s.GetPricingInput(tier="tier_balanced")))
    assert out.startswith("> ⚠️ **Stale prices.** Last verified 2020-01-01")
    assert "routing is refused until prices are re-verified" in out
    assert "## Token pricing" in out              # still informational


# ─── arbitrage_estimate_cost / provider_status edge paths ─────────────────────

def test_estimate_cost_no_winner(route_env):
    s = route_env.server
    out = json.loads(_run(s.arbitrage_estimate_cost(s.EstimateCostInput(
        tier="tier_fast", input_tokens=10, output_tokens=10))))
    assert out["error"].startswith("No eligible providers found")
    assert len(out["excluded"]) == 8
    assert all(e["reason"] == "no API key provided for this provider" for e in out["excluded"])
    assert out["known_providers"] == s.PROVIDER_IDS


def test_estimate_cost_success_carries_price_table(route_env):
    s = route_env.server
    out = json.loads(_run(s.arbitrage_estimate_cost(s.EstimateCostInput(
        tier="tier_fast", input_tokens=10, output_tokens=10, api_keys={"groq": FAKE}))))
    assert out["winner"]["provider_id"] == "groq"
    assert out["price_table"]["stale"] is False


def test_provider_status_single_provider(route_env):
    s = route_env.server
    out = json.loads(_run(s.arbitrage_provider_status(s.ProviderStatusInput(
        provider_id="groq", api_keys={"groq": FAKE}))))
    assert list(out["providers"]) == ["groq"]
    g = out["providers"]["groq"]
    assert g["key_provided"] is True and g["protocol"] == "openai"
    assert set(g["models"]) == {"tier_fast", "tier_balanced", "tier_powerful"}
    assert out["summary"] == {"total": 1, "keys_provided": 1, "keys_missing": 0}


def test_provider_status_unknown_provider(route_env):
    s = route_env.server
    out = json.loads(_run(s.arbitrage_provider_status(s.ProviderStatusInput(provider_id="nope"))))
    assert out["error"].startswith("Unknown provider 'nope'")
    assert "groq" in out["error"]


# ─── history / spend / budget / health tools ──────────────────────────────────

def test_get_history_empty(route_env):
    s = route_env.server
    out = json.loads(_run(s.arbitrage_get_history(s.GetHistoryInput())))
    assert out == {"message": "No routing history found.", "records": [], "scope": "anon"}


def test_get_history_records_summary_and_scope(route_env):
    s = route_env.server
    _log("groq", "Groq", session="ann")
    _log("openai", "OpenAI", ok=False, session="ann")
    _log("groq", "Groq", session="ben")
    out = json.loads(_run(s.arbitrage_get_history(s.GetHistoryInput(session="ann"))))
    assert [r["provider_id"] for r in out["records"]] == ["openai", "groq"]   # newest first
    assert out["summary"]["total_records"] == 2
    assert out["summary"]["successful"] == 1 and out["summary"]["failed"] == 1
    assert out["scope"] == "session"
    one = json.loads(_run(s.arbitrage_get_history(s.GetHistoryInput(session="ann", limit=1))))
    assert len(one["records"]) == 1                      # limit honoured
    summ = json.loads(_run(s.arbitrage_get_history(s.GetHistoryInput(session="ann",
                                                                     summary_only=True))))
    assert summ["total_records"] == 2 and "records" not in summ
    assert summ["provider_usage"] == {"groq": 1, "openai": 1}
    assert summ["scope"] == "session"


def test_spend_report_tool(route_env):
    s = route_env.server
    _log("groq", "Groq", session="ann", cost=0.25)
    _log("openai", "OpenAI", session="ann", cost=0.5)
    out = json.loads(_run(s.arbitrage_spend_report(s.SpendReportInput(session="ann"))))
    assert out["total_cost_usd"] == 0.75
    assert out["spend_by_provider"] == {"Groq": 0.25, "OpenAI": 0.5}
    assert out["spend_by_tier"] == {"tier_fast": 0.75}
    assert list(out["spend_by_day"].values()) == [0.75]
    assert out["scope"] == "session"
    future = json.loads(_run(s.arbitrage_spend_report(s.SpendReportInput(session="ann",
                                                                         since="2999-01-01"))))
    assert future == {"total_records": 0, "message": "No spend in range.", "scope": "session"}


def test_set_budget_tool(route_env):
    s = route_env.server
    _log("groq", "Groq", session="cat", cost=2.5)
    out = json.loads(_run(s.arbitrage_set_budget(s.SetBudgetInput(session="cat", monthly_usd=10))))
    assert out["budget_set"] == {"session_scoped": True, "monthly_budget_usd": 10.0}
    st = out["current_status"]
    assert st["monthly_budget_usd"] == 10.0 and st["spent_this_month_usd"] == 2.5
    assert st["used_pct"] == 25.0 and st["over_budget"] is False


def test_provider_health_tool(route_env):
    s = route_env.server
    for i in range(4):
        _log("groq", "Groq", ok=False, session=f"user{i}")
    _log("openai", "OpenAI", ok=True)
    out = json.loads(_run(s.arbitrage_provider_health(s.ProviderHealthInput())))
    assert out["providers"]["groq"] == {"score": 0.0, "samples": 4, "healthy": False,
                                        "sessions": 4, "failing_sessions": 4}
    assert out["providers"]["openai"]["healthy"] is True
    assert out["unhealthy"] == ["groq"]
    import health
    assert out["thresholds"] == {"window": health.HEALTH_WINDOW,
                                 "min_samples": health.HEALTH_MIN_SAMPLES,
                                 "unhealthy_at_success_rate_below_or_equal": health.HEALTH_THRESHOLD,
                                 "max_per_session": health.HEALTH_MAX_PER_SESSION,
                                 "min_failing_sessions": 2,   # HTTP default
                                 "scan_factor": health.HEALTH_SCAN_FACTOR}
    # a window of 1 only sees the newest record (openai)
    one = json.loads(_run(s.arbitrage_provider_health(s.ProviderHealthInput(window=1))))
    assert list(one["providers"]) == ["openai"] and one["thresholds"]["window"] == 1


# ─── count_tokens / check_price_drift ─────────────────────────────────────────

def test_count_tokens_tool(route_env):
    s = route_env.server
    from tokens import count_message_tokens, _get_encoder
    msgs = [{"role": "user", "content": "Hello there, how are you today?"}]
    out = json.loads(_run(s.arbitrage_count_tokens(s.CountTokensInput(messages=msgs,
                                                                      system_prompt="Be terse."))))
    assert out["input_tokens"] == count_message_tokens(msgs, "Be terse.")
    assert out["input_tokens"] > count_message_tokens(msgs)
    assert out["method"] == ("tiktoken:o200k_base" if _get_encoder() else "heuristic")


def test_count_tokens_tool_reports_heuristic(route_env, monkeypatch):
    import tokens
    monkeypatch.setattr(tokens, "_get_encoder", lambda: None)
    s = route_env.server
    out = json.loads(_run(s.arbitrage_count_tokens(s.CountTokensInput(
        messages=[{"role": "user", "content": "one two three"}]))))
    assert out == {"input_tokens": tokens._heuristic_tokens("one two three") + 4,
                   "method": "heuristic"}


def _feed_with_openai_drift():
    from pricing_table import raw_table
    t = raw_table()
    feed = []
    for pid, p in t["providers"].items():
        r = p.get("refresh") or {}
        if r.get("strategy") != "openrouter":
            continue
        for tier, or_id in r["ids"].items():
            m = p["models"][tier]
            inp = m["input_price_per_1m"] * (2 if (pid, tier) == ("openai", "tier_fast") else 1)
            pricing = {"prompt": str(inp / 1e6), "completion": str(m["output_price_per_1m"] / 1e6)}
            if "cached_input_price_per_1m" in m:
                pricing["input_cache_read"] = str(m["cached_input_price_per_1m"] / 1e6)
            feed.append({"id": or_id, "pricing": pricing})
    return {"data": feed}


def test_check_price_drift_tool_with_proposal(route_env, monkeypatch):
    import refresh
    feed = _feed_with_openai_drift()
    monkeypatch.setattr(refresh, "_fetch_json", lambda url: feed)
    s = route_env.server
    out = json.loads(_run(s.arbitrage_check_price_drift(s.CheckPriceDriftInput(propose=True))))
    rep = out["report"]
    assert rep["any_drift"] is True
    assert rep["summary"]["drifts"] == 1 and rep["summary"]["matches"] == 5
    drifted = [p for p in rep["providers"] if p["status"] == "drift"]
    assert [p["provider_id"] for p in drifted] == ["openai"]
    ch = drifted[0]["changes"]
    assert [(c["tier"], c["field"]) for c in ch] == [("tier_fast", "input_price_per_1m")]
    assert ch[0]["new"] == pytest.approx(ch[0]["old"] * 2)
    cand = out["candidate_table"]
    assert cand["_candidate"] is True
    assert cand["providers"]["openai"]["models"]["tier_fast"]["input_price_per_1m"] == ch[0]["new"]
    from pricing_table import raw_table
    assert cand["verified_at"] == raw_table()["verified_at"]      # never advanced
    assert "NOT been written to disk" in out["candidate_note"]


def test_check_price_drift_tool_no_proposal_without_drift(route_env, monkeypatch):
    import refresh
    monkeypatch.setattr(refresh, "_fetch_json", lambda url: None)   # feed outage
    s = route_env.server
    out = json.loads(_run(s.arbitrage_check_price_drift(s.CheckPriceDriftInput(propose=True))))
    assert out["report"]["any_drift"] is False
    assert out["report"]["summary"]["fetch_failures"] == 6
    assert "candidate_table" not in out


# ─── input-model validation ───────────────────────────────────────────────────

MSG = [{"role": "user", "content": "x"}]


@pytest.mark.parametrize("model,kwargs", [
    ("GetPricingInput", {}),
    ("EstimateCostInput", {"input_tokens": 1, "output_tokens": 1}),
    ("RouteCompletionInput", {"messages": MSG, "api_keys": {}}),
])
def test_bad_tier_rejected(route_env, model, kwargs):
    cls = getattr(route_env.server, model)
    with pytest.raises(ValidationError, match="tier must be one of"):
        cls(tier="tier_bogus", **kwargs)
    assert cls(tier="tier_fast", **kwargs).tier == "tier_fast"


@pytest.mark.parametrize("model,kwargs", [
    ("GetPricingInput", {"tier": "tier_fast", "input_tokens": 0}),
    ("GetPricingInput", {"tier": "tier_fast", "input_tokens": 2_000_001}),
    ("GetPricingInput", {"tier": "tier_fast", "output_tokens": 500_001}),
    ("EstimateCostInput", {"tier": "tier_fast", "input_tokens": 1, "output_tokens": 0}),
    ("EstimateCostInput", {"tier": "tier_fast", "input_tokens": 1, "output_tokens": 1,
                           "cached_input_tokens": -1}),
    ("RouteCompletionInput", {"messages": [], "api_keys": {}}),
    ("RouteCompletionInput", {"messages": MSG, "api_keys": {}, "max_tokens": 0}),
    ("RouteCompletionInput", {"messages": MSG, "api_keys": {}, "max_tokens": 32_001}),
    ("RouteCompletionInput", {"messages": MSG, "api_keys": {}, "max_failover": -1}),
    ("RouteCompletionInput", {"messages": MSG, "api_keys": {}, "max_failover": 8}),
    ("RouteCompletionInput", {"messages": MSG}),                         # api_keys required
    ("GetHistoryInput", {"limit": 0}),
    ("GetHistoryInput", {"limit": 101}),
    ("SpendReportInput", {"limit": 2001}),
    ("SetBudgetInput", {"session": "s", "monthly_usd": 0}),
    ("SetBudgetInput", {"monthly_usd": 5}),                              # session required
    ("ProviderHealthInput", {"window": 0}),
    ("ProviderHealthInput", {"window": 2001}),
    ("CountTokensInput", {"messages": []}),
])
def test_bounds_rejected(route_env, model, kwargs):
    with pytest.raises(ValidationError):
        getattr(route_env.server, model)(**kwargs)


@pytest.mark.parametrize("model,kwargs", [
    ("GetPricingInput", {"tier": "tier_fast"}),
    ("EstimateCostInput", {"tier": "tier_fast", "input_tokens": 1, "output_tokens": 1}),
    ("RouteCompletionInput", {"messages": MSG, "api_keys": {}}),
    ("ProviderStatusInput", {}),
    ("GetHistoryInput", {}),
    ("SpendReportInput", {}),
    ("SetBudgetInput", {"session": "s", "monthly_usd": 1}),
    ("ProviderHealthInput", {}),
    ("CountTokensInput", {"messages": MSG}),
    ("CheckPriceDriftInput", {}),
])
def test_extra_fields_forbidden(route_env, model, kwargs):
    cls = getattr(route_env.server, model)
    cls(**kwargs)                                         # valid baseline
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        cls(**kwargs, api_key="sk-test-FAKE")


def test_all_tools_registered(route_env):
    tools = asyncio.run(route_env.server.mcp.list_tools())
    assert {t.name for t in tools} == {
        "arbitrage_get_pricing", "arbitrage_estimate_cost", "arbitrage_route_completion",
        "arbitrage_provider_status", "arbitrage_get_history", "arbitrage_spend_report",
        "arbitrage_set_budget", "arbitrage_provider_health", "arbitrage_count_tokens",
        "arbitrage_check_price_drift"}


def test_provider_health_reports_effective_min_sessions(route_env, monkeypatch):
    s = route_env.server
    import providers

    def thresholds():
        return json.loads(_run(s.arbitrage_provider_health(
            s.ProviderHealthInput())))["thresholds"]["min_failing_sessions"]
    assert thresholds() == 2                                  # HTTP
    runs = []
    monkeypatch.setattr(s.mcp, "run", lambda transport: runs.append(transport))
    s.main(["server.py", "--stdio"])                          # marks local after import
    assert runs == ["stdio"] and providers.is_local_transport() is True
    assert thresholds() == 1
    monkeypatch.setenv("HEALTH_MIN_SESSIONS", "4")
    assert thresholds() == 4
