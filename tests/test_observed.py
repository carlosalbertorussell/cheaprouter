"""Tests for the SP1d waist interaction — observed signal + validation."""

import asyncio, json
import pytest
import price_history as ph
import refresh
import pricing_server as ps


def _run(c): return asyncio.run(c)


@pytest.fixture
def be(tmp_path, monkeypatch):
    b = ph.FileHistoryBackend(str(tmp_path / "h.jsonl"))
    monkeypatch.setattr(ph, "get_backend", lambda: b)
    return b


# ─── record_observed ──────────────────────────────────────────────────────────

def test_record_observed_writes_observed_tier(be):
    ph.record_observed(provider="deepseek", model_id="deepseek-v4-flash", tier="tier_fast",
                       observed_input_per_1m=0.22, observed_output_per_1m=0.66)
    rows = be.read()
    assert len(rows) == 2
    assert all(r["provenance_tier"] == "observed" for r in rows)
    assert all(r["source"] == "router" for r in rows)


def test_record_observed_metadata_only(be):
    ph.record_observed(provider="x", model_id="m", tier="tier_fast", observed_input_per_1m=1.0)
    raw = open(be.path).read()
    # schema-limited: no content/keys possible, and no token counts leaked
    for banned in ("api_key", "content", "messages", "prompt", "input_tokens"):
        assert banned not in raw


def test_observed_entries_filters_to_observed(be):
    ph.record_observed(provider="x", model_id="m", tier="tier_fast", observed_input_per_1m=1.0)
    be.append(ph.build_entry(provider="x", model_id="m", tier="tier_fast",
              field="input_price_per_1m", old=1.0, new=2.0, provenance_tier="verified"))
    obs = ph.observed_entries()
    assert len(obs) == 1 and obs[0]["provenance_tier"] == "observed"


# ─── observed_validation ──────────────────────────────────────────────────────

def _table(inp):
    return {"providers": {"deepseek": {"models": {"tier_fast": {
        "model_id": "deepseek-v4-flash", "input_price_per_1m": inp,
        "output_price_per_1m": 0.66}}}}}


def test_validation_confirms_when_matching(be):
    ph.record_observed(provider="deepseek", model_id="deepseek-v4-flash",
                       tier="tier_fast", observed_input_per_1m=0.22)
    rep = refresh.observed_validation(table=_table(0.22))
    f = next(x for x in rep["findings"] if x["field"] == "input_price_per_1m")
    assert f["status"] == "confirms"
    assert rep["any_divergence"] is False


def test_validation_diverges_when_off(be):
    ph.record_observed(provider="deepseek", model_id="deepseek-v4-flash",
                       tier="tier_fast", observed_input_per_1m=0.50)  # vs verified 0.22 → +127%
    rep = refresh.observed_validation(table=_table(0.22))
    f = next(x for x in rep["findings"] if x["field"] == "input_price_per_1m")
    assert f["status"] == "diverges"
    assert rep["any_divergence"] is True


def test_validation_never_mutates_table(be):
    t = _table(0.22)
    ph.record_observed(provider="deepseek", model_id="deepseek-v4-flash",
                       tier="tier_fast", observed_input_per_1m=0.50)
    refresh.observed_validation(table=t)
    # table price unchanged — observed challenges, never promotes
    assert t["providers"]["deepseek"]["models"]["tier_fast"]["input_price_per_1m"] == 0.22


def test_validation_empty_when_no_observed(be):
    rep = refresh.observed_validation(table=_table(0.22))
    assert rep["summary"]["observed_points"] == 0


# ─── pricing_observed tool ────────────────────────────────────────────────────

def test_pricing_observed_tool(be):
    ph.record_observed(provider="deepseek", model_id="deepseek-v4-flash",
                       tier="tier_fast", observed_input_per_1m=0.22)
    out = json.loads(_run(ps.pricing_observed(ps.PricingObservedInput())))
    assert "validation" in out
    assert out["provenance"]["tier"] == "verified"
    assert "never" in out["validation"]["note"].lower()


# ─── router emits observed only when opted in ─────────────────────────────────

@pytest.mark.parametrize("flag,expect_emit", [(None, False), ("0", False), ("1", True),
                                               ("true", True), ("yes", True)])
def test_router_emits_observed_only_when_enabled(route_env, be, monkeypatch, flag, expect_emit):
    """Run a real route_completion and check whether an observed entry is written."""
    from client import CompletionResult
    if flag is None:
        monkeypatch.delenv("ARBITRAGE_EMIT_OBSERVED", raising=False)
    else:
        monkeypatch.setenv("ARBITRAGE_EMIT_OBSERVED", flag)

    async def fake_call(provider, model, api_key, messages, system_prompt, max_tokens):
        return CompletionResult(text="ok", input_tokens_used=10, output_tokens_used=5,
                                latency_ms=1, model_id="served-model", raw_response={})
    s = route_env.server
    monkeypatch.setattr(s, "call_provider", fake_call)
    out = json.loads(_run(s.arbitrage_route_completion(s.RouteCompletionInput(
        messages=[{"role": "user", "content": "hi"}], tier="tier_fast",
        api_keys={"groq": "sk-test-FAKE"}, health_aware=False))))
    assert out["response"] == "ok"                      # emit never affects the response
    rows = be.read()
    if not expect_emit:
        assert rows == []
        return
    import providers
    m = providers.PROVIDERS["groq"].models["tier_fast"]
    assert {r["field"] for r in rows} == {"input_price_per_1m", "output_price_per_1m"}
    assert all(r["provenance_tier"] == "observed" and r["provider"] == "groq" for r in rows)
    # The table's model ID (so validation can match it), with the provider's
    # reported ID kept apart (ST5).
    assert all(r["model_id"] == m.model_id for r in rows)
    assert all(r["served_model_id"] == "served-model" for r in rows)
    by = {r["field"]: r["new"] for r in rows}
    assert by == {"input_price_per_1m": m.input_price_per_1m,
                  "output_price_per_1m": m.output_price_per_1m}


def test_router_emit_failure_never_breaks_response(route_env, monkeypatch):
    import price_history
    from client import CompletionResult
    monkeypatch.setenv("ARBITRAGE_EMIT_OBSERVED", "1")
    def boom(**kw):
        raise OSError("disk full")
    monkeypatch.setattr(price_history, "record_observed", boom)
    async def fake_call(provider, model, api_key, messages, system_prompt, max_tokens):
        return CompletionResult(text="ok", input_tokens_used=1, output_tokens_used=1,
                                latency_ms=1, model_id="m", raw_response={})
    s = route_env.server
    monkeypatch.setattr(s, "call_provider", fake_call)
    out = json.loads(_run(s.arbitrage_route_completion(s.RouteCompletionInput(
        messages=[{"role": "user", "content": "hi"}], api_keys={"groq": "sk-test-FAKE"},
        health_aware=False))))
    assert out["response"] == "ok"


# ─── ST5: observed entries carry the table's model ID ─────────────────────────

def test_record_observed_keeps_served_id_only_when_different(be):
    ph.record_observed(provider="x", model_id="m", tier="tier_fast",
                       observed_input_per_1m=1.0, served_model_id="m")
    ph.record_observed(provider="x", model_id="m", tier="tier_fast",
                       observed_input_per_1m=1.0, served_model_id="m-2026-11-01")
    rows = be.read()                                  # newest first
    assert rows[0]["served_model_id"] == "m-2026-11-01"
    assert "served_model_id" not in rows[1]


def test_router_observed_entries_match_the_table(route_env, be, monkeypatch):
    """The provider answers with a dated snapshot ID; validation must still match."""
    from client import CompletionResult
    monkeypatch.setenv("ARBITRAGE_EMIT_OBSERVED", "1")

    async def fake_call(provider, model, api_key, messages, system_prompt, max_tokens):
        return CompletionResult(text="ok", input_tokens_used=10, output_tokens_used=5,
                                latency_ms=1, model_id=model.model_id + "-2026-11-01",
                                raw_response={})
    s = route_env.server
    monkeypatch.setattr(s, "call_provider", fake_call)
    _run(s.arbitrage_route_completion(s.RouteCompletionInput(
        messages=[{"role": "user", "content": "hi"}], tier="tier_fast",
        api_keys={"groq": "sk-test-FAKE"}, health_aware=False)))
    import json as _json, os
    table = _json.loads(open(os.environ["ARBITRAGE_PRICES_FILE"]).read())
    rep = refresh.observed_validation(table=table)
    assert rep["summary"]["observed_points"] == 2
    assert rep["summary"]["confirms"] == 2 and rep["any_divergence"] is False
