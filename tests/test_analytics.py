"""Tests for spend report, budgets, and session attribution (S1)."""

import pytest
import history
from storage import JSONLBackend, build_record


@pytest.fixture
def local_backend(tmp_path, monkeypatch):
    """Force history.py onto an isolated JSONL file for each test."""
    f = tmp_path / "h.jsonl"
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.setenv("ARBITRAGE_HISTORY_FILE", str(f))
    return f


def _decision(pid="groq", name="Groq", region="us", tier="tier_fast", cost=0.001, saved=0.002):
    return {"winner": {"provider_id": pid, "provider_name": name, "region": region,
                       "total_cost_usd": cost}, "tier": tier, "savings": {"saved_usd": saved}}


def test_session_normalization_is_opaque():
    tok = history.normalize_session("my-secret-session-id")
    assert tok.startswith("s_")
    assert "my-secret" not in tok
    # deterministic
    assert tok == history.normalize_session("my-secret-session-id")


def test_log_and_summary_roundtrip(local_backend):
    history.log_decision(_decision(), session="alice",
                         actual_input_tokens=100, actual_output_tokens=50,
                         actual_latency_ms=120, actual_cost_usd=0.0015)
    summ = history.history_summary(session="alice")
    assert summ["total_records"] == 1
    assert summ["successful"] == 1
    assert summ["total_cost_usd"] == 0.0015


def test_spend_report_breakdowns(local_backend):
    history.log_decision(_decision(name="Groq", tier="tier_fast", saved=0.002),
                         session="alice", actual_cost_usd=0.001)
    history.log_decision(_decision(pid="deepseek", name="DeepSeek", tier="tier_balanced", saved=0.02),
                         session="alice", actual_cost_usd=0.003)
    rep = history.spend_report(session="alice")
    assert rep["total_cost_usd"] == 0.004
    assert rep["spend_by_provider"]["Groq"] == 0.001
    assert rep["spend_by_provider"]["DeepSeek"] == 0.003
    assert "tier_fast" in rep["spend_by_tier"]
    assert "tier_balanced" in rep["spend_by_tier"]


def test_spend_report_session_isolation(local_backend):
    history.log_decision(_decision(), session="alice", actual_cost_usd=0.005)
    history.log_decision(_decision(), session="bob", actual_cost_usd=0.009)
    assert history.spend_report(session="alice")["total_cost_usd"] == 0.005
    assert history.spend_report(session="bob")["total_cost_usd"] == 0.009


def test_budget_set_and_status(local_backend):
    history.set_budget("alice", 10.0)
    assert history.get_budget("alice") == 10.0
    st = history.budget_status("alice")
    assert st["monthly_budget_usd"] == 10.0
    assert st["over_budget"] is False


def test_budget_excludes_marker_from_spend(local_backend):
    history.set_budget("alice", 10.0)
    history.log_decision(_decision(), session="alice", actual_cost_usd=2.0)
    st = history.budget_status("alice")
    # the $10 budget marker must not count as spend
    assert st["spent_this_month_usd"] == 2.0
    assert st["used_pct"] == 20.0


def test_budget_over_threshold(local_backend):
    history.set_budget("alice", 1.0)
    history.log_decision(_decision(), session="alice", actual_cost_usd=1.5)
    st = history.budget_status("alice")
    assert st["over_budget"] is True


def test_no_budget_returns_none(local_backend):
    assert history.budget_status("nobody") is None


def test_error_records_marked_failed(local_backend):
    history.log_decision(_decision(), session="alice", error="RateLimitError: 429")
    summ = history.history_summary(session="alice")
    assert summ["failed"] == 1
    assert summ["successful"] == 0
