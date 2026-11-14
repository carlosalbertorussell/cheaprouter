"""Tests for provider health tracking and health-aware routing (S8)."""

import pytest
import history
from providers import PROVIDERS
from router import route


@pytest.fixture
def local_backend(tmp_path, monkeypatch):
    f = tmp_path / "h.jsonl"
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.setenv("ARBITRAGE_HISTORY_FILE", str(f))
    return f


def _decision(pid, name):
    return {"winner": {"provider_id": pid, "provider_name": name, "region": "us",
                       "total_cost_usd": 0.001}, "tier": "tier_fast", "savings": {}}


_n = iter(range(10**9))


def _log(pid, name, success, session=None, transient=True, error="RateLimitError: 429"):
    """One routing record. By default each call is a different session, so the
    per-session cap never interferes unless a test pins `session`."""
    history.log_decision(_decision(pid, name),
                         session=session or f"user-{next(_n)}",
                         actual_cost_usd=0.001,
                         error=None if success else error,
                         transient=None if success else transient)


ALL_KEYS = {p: "k" for p in PROVIDERS}


# ─── health scoring ───────────────────────────────────────────────────────────

def test_no_history_means_no_snapshot(local_backend):
    import health
    assert health.provider_health() == {}


def test_healthy_provider_scored(local_backend):
    import health
    for _ in range(5):
        _log("groq", "Groq", True)
    snap = health.provider_health()
    assert snap["groq"]["score"] == 1.0
    assert snap["groq"]["healthy"] is True


def test_failing_provider_marked_unhealthy(local_backend):
    import health
    for _ in range(5):
        _log("groq", "Groq", False)
    snap = health.provider_health()
    assert snap["groq"]["score"] == 0.0
    assert snap["groq"]["healthy"] is False


def test_below_min_samples_stays_healthy(local_backend, monkeypatch):
    import health
    # 2 failures only, min_samples default 3 → not enough to condemn
    _log("groq", "Groq", False)
    _log("groq", "Groq", False)
    snap = health.provider_health()
    assert snap["groq"]["samples"] == 2
    assert snap["groq"]["healthy"] is True


def test_mixed_success_rate(local_backend):
    import health
    for _ in range(3):
        _log("openai", "OpenAI", True)
    for _ in range(1):
        _log("openai", "OpenAI", False)
    snap = health.provider_health()
    assert snap["openai"]["score"] == 0.75   # 3/4
    assert snap["openai"]["healthy"] is True  # above 0.5 threshold


# ─── health-aware routing ─────────────────────────────────────────────────────

def test_unhealthy_provider_deprioritized_not_excluded(local_backend):
    # Groq is the cheapest at tier_fast. Make it unhealthy; it must still be in
    # the pool but no longer the winner.
    for _ in range(5):
        _log("groq", "Groq", False)
    d = route(PROVIDERS, "tier_fast", 1000, 500, user_keys=ALL_KEYS, health_aware=True)
    assert d.winner.provider_id != "groq"          # deprioritized
    assert "groq" in d.deprioritized
    # still eligible — not in excluded
    assert all(e["provider_id"] != "groq" for e in d.excluded)


def test_health_aware_off_keeps_cheapest(local_backend):
    for _ in range(5):
        _log("groq", "Groq", False)
    d = route(PROVIDERS, "tier_fast", 1000, 500, user_keys=ALL_KEYS, health_aware=False)
    assert d.winner.provider_id == "groq"          # pure cost order
    assert d.deprioritized == []


def test_all_unhealthy_still_returns_a_winner(local_backend):
    # If every provider is unhealthy, routing must still pick one (cheapest).
    for pid in ("groq", "qwen", "gemini", "mistral", "openai", "grok", "deepseek", "anthropic"):
        for _ in range(5):
            _log(pid, pid, False)
    d = route(PROVIDERS, "tier_fast", 1000, 500, user_keys=ALL_KEYS, health_aware=True)
    assert d.winner is not None                     # never stranded
    assert d.winner.provider_id == "groq"           # cheapest among all-unhealthy


def test_healthy_cheaper_beats_healthy_pricier(local_backend):
    # Sanity: with everyone healthy, health-aware routing == cost order.
    _log("groq", "Groq", True)
    d = route(PROVIDERS, "tier_fast", 1000, 500, user_keys=ALL_KEYS, health_aware=True)
    assert d.winner.provider_id == "groq"


def test_deprioritized_in_decision_dict(local_backend):
    for _ in range(5):
        _log("groq", "Groq", False)
    d = route(PROVIDERS, "tier_fast", 1000, 500, user_keys=ALL_KEYS, health_aware=True)
    assert "deprioritized" in d.to_dict()
    assert "groq" in d.to_dict()["deprioritized"]


# ─── ST5: abuse resistance ────────────────────────────────────────────────────

def _bad_key(pid="groq", name="Groq", session="mallory"):
    _log(pid, name, False, session=session, transient=False,
         error="Client error '401 Unauthorized' for url 'https://example.invalid'")


def test_bad_key_failures_from_one_session_never_count(local_backend):
    import health
    for _ in range(50):
        _bad_key()
    snap = health.provider_health()
    assert "groq" not in snap                      # ignored entirely, not scored
    assert health.is_healthy("groq", snap)
    d = route(PROVIDERS, "tier_fast", 1000, 500, user_keys=ALL_KEYS, health_aware=True)
    assert d.winner.provider_id == "groq" and d.deprioritized == []


def test_bad_request_failures_never_count(local_backend):
    import health
    for code in ("400 Bad Request", "403 Forbidden", "404 Not Found"):
        for _ in range(5):
            _log("groq", "Groq", False, transient=False,
                 error=f"Client error '{code}' for url 'https://example.invalid'")
    assert "groq" not in health.provider_health()


def test_bad_key_spam_cannot_flood_out_real_evidence(local_backend):
    import health
    for _ in range(4):
        _log("groq", "Groq", False)                # real transient failures
    for _ in range(100):                           # 2x the default window of noise
        _bad_key()
    snap = health.provider_health()
    assert snap["groq"]["samples"] == 4 and snap["groq"]["healthy"] is False


def test_transient_failures_across_sessions_mark_unhealthy(local_backend):
    import health
    for i in range(6):
        _log("groq", "Groq", False, session=f"caller-{i}")
    snap = health.provider_health()
    assert snap["groq"]["healthy"] is False
    assert snap["groq"]["sessions"] == 6
    d = route(PROVIDERS, "tier_fast", 1000, 500, user_keys=ALL_KEYS, health_aware=True)
    assert d.winner.provider_id != "groq" and "groq" in d.deprioritized


def test_one_session_spamming_transient_failures_is_capped(local_backend):
    import health
    for i in range(10):
        _log("groq", "Groq", True, session=f"good-{i}")     # 10 callers doing fine
    for _ in range(60):
        _log("groq", "Groq", False, session="spammer")      # one caller, 429 storm
    snap = health.provider_health()
    cap = health.HEALTH_MAX_PER_SESSION
    assert snap["groq"]["samples"] == 10 + cap               # spammer counted `cap` times
    assert snap["groq"]["score"] == round(10 / (10 + cap), 3)
    assert snap["groq"]["healthy"] is True


def test_one_session_flooding_the_window_cannot_condemn(local_backend):
    # A flood big enough to push every other record out of the scan leaves only
    # the spammer's capped failures: one session, so not enough to condemn.
    import health
    for i in range(10):
        _log("groq", "Groq", True, session=f"good-{i}")
    for _ in range(health.HEALTH_WINDOW * health.HEALTH_SCAN_FACTOR + 50):
        _log("groq", "Groq", False, session="spammer")
    snap = health.provider_health()
    assert snap["groq"]["samples"] == health.HEALTH_MAX_PER_SESSION
    assert snap["groq"]["failing_sessions"] == 1
    assert snap["groq"]["healthy"] is True
    d = route(PROVIDERS, "tier_fast", 1000, 500, user_keys=ALL_KEYS, health_aware=True)
    assert d.winner.provider_id == "groq" and d.deprioritized == []


def test_single_user_server_can_opt_back_in(local_backend, monkeypatch):
    # HEALTH_MIN_SESSIONS=1: one session's transient failures are enough.
    import health
    monkeypatch.setattr(health, "HEALTH_MIN_SESSIONS", 1)
    for _ in range(5):
        _log("groq", "Groq", False, session="only-me")
    assert health.provider_health()["groq"]["healthy"] is False
    d = route(PROVIDERS, "tier_fast", 1000, 500, user_keys=ALL_KEYS, health_aware=True)
    assert "groq" in d.deprioritized


def test_two_sessions_are_enough_by_default(local_backend):
    import health
    for sess in ("a", "a", "b", "b"):
        _log("groq", "Groq", False, session=sess)
    snap = health.provider_health()
    assert snap["groq"]["failing_sessions"] == 2 and snap["groq"]["healthy"] is False


def test_session_cap_also_limits_successes(local_backend):
    # A caller spamming successes can't mask an outage others are seeing.
    import health
    for _ in range(100):
        _log("groq", "Groq", True, session="booster")
    for i in range(6):
        _log("groq", "Groq", False, session=f"victim-{i}")
    snap = health.provider_health()
    assert snap["groq"]["samples"] == 6 + health.HEALTH_MAX_PER_SESSION
    assert snap["groq"]["healthy"] is False


def test_cap_is_configurable(local_backend, monkeypatch):
    import health
    monkeypatch.setattr(health, "HEALTH_MAX_PER_SESSION", 2)
    for _ in range(10):
        _log("groq", "Groq", False, session="solo")
    monkeypatch.setattr(health, "HEALTH_MIN_SESSIONS", 1)
    snap = health.provider_health()
    assert snap["groq"]["samples"] == 2                       # below min samples
    assert snap["groq"]["healthy"] is True


def test_window_counts_scored_records(local_backend):
    import health
    for _ in range(5):
        _log("groq", "Groq", False)
    _log("openai", "OpenAI", True)
    snap = health.provider_health(limit=1)
    assert list(snap) == ["openai"]


def _legacy(local_backend, pid, success, error_class):
    """A pre-ST5 record: no `transient` field at all."""
    import json
    from datetime import datetime, timezone
    rec = {"timestamp": datetime.now(timezone.utc).isoformat(), "session": f"s_{next(_n):016x}",
           "provider_id": pid, "provider_name": pid, "region": "us", "tier": "tier_fast",
           "input_tokens": None, "output_tokens": None, "cost_usd": 0.0, "saved_usd": 0.0,
           "latency_ms": None, "success": success, "error_class": error_class}
    with open(local_backend, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def test_legacy_failures_do_not_count(local_backend):
    import health
    for err in ("Server error '503 Service Unavailable' for url 'https",
                "Client error '401 Unauthorized' for url 'https", "ReadTimeout", None):
        for _ in range(3):
            _legacy(local_backend, "groq", False, err)
    assert "groq" not in health.provider_health()


def test_legacy_successes_still_count(local_backend):
    import health
    for _ in range(3):
        _legacy(local_backend, "groq", True, None)
    _log("groq", "Groq", False)
    snap = health.provider_health()
    assert snap["groq"]["samples"] == 4 and snap["groq"]["score"] == 0.75


def test_unclassified_new_failure_does_not_count(local_backend):
    import health
    for _ in range(5):
        _log("groq", "Groq", False, transient=None)
    assert "groq" not in health.provider_health()


def test_transient_flag_written_on_records(local_backend):
    _log("groq", "Groq", True)
    _log("groq", "Groq", False, transient=True)
    _bad_key()
    recs = history.read_history(limit=3)            # newest first
    assert [r["transient"] for r in recs] == [False, True, False]
