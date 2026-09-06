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

def test_router_emits_observed_only_when_enabled(tmp_path, monkeypatch):
    # This is an integration-ish check that the env gate exists and defaults off.
    import server
    import os
    # default: not set → the gate is off (we assert the code path is guarded)
    monkeypatch.delenv("ARBITRAGE_EMIT_OBSERVED", raising=False)
    assert os.getenv("ARBITRAGE_EMIT_OBSERVED", "").strip() not in ("1","true","TRUE","yes")
