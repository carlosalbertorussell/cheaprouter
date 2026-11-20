"""Tests for the SC7 scheduled drift check: the engine in refresh.py and the
scripts/drift_check.py CLI. The OpenRouter feed is always stubbed (no network)."""

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

import refresh

REPO = Path(__file__).resolve().parent.parent
REAL_TEXT = (REPO / "prices.json").read_text(encoding="utf-8")
REAL = json.loads(REAL_TEXT)


def _load_cli():
    spec = importlib.util.spec_from_file_location("drift_check", REPO / "scripts" / "drift_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


drift_check = _load_cli()


# ─── fixtures / helpers ───────────────────────────────────────────────────────

def _feed_for(table, overrides=None, drop=()):
    """An OpenRouter /models response matching the table exactly, with per-field
    overrides {(provider, tier, field): per_1m_price} and dropped OpenRouter ids."""
    overrides = overrides or {}
    by_id = {}
    for pid, prov in table["providers"].items():
        refresh_cfg = prov.get("refresh") or {}
        if refresh_cfg.get("strategy") != "openrouter":
            continue
        for tier, or_id in refresh_cfg["ids"].items():
            m = prov["models"][tier]
            pricing = by_id.setdefault(or_id, {})
            for field, key in refresh._OR_FIELD_KEYS.items():
                if field in m:
                    v = overrides.get((pid, tier, field), m[field])
                    pricing[key] = repr(v / 1_000_000)
    return {"data": [{"id": i, "pricing": p} for i, p in by_id.items() if i not in drop]}


@pytest.fixture
def prices_file(tmp_path, monkeypatch):
    """A copy of the shipped prices.json the CLI reads via ARBITRAGE_PRICES_FILE."""
    pf = tmp_path / "prices.json"
    pf.write_text(REAL_TEXT, encoding="utf-8")
    monkeypatch.setenv("ARBITRAGE_PRICES_FILE", str(pf))
    monkeypatch.setattr(drift_check, "_today", lambda: date(2026, 11, 20))
    return pf


def _stub_feed(monkeypatch, feed):
    calls = []

    def fake(url):
        calls.append(url)
        return feed

    monkeypatch.setattr(refresh, "_fetch_json", fake)
    return calls


def _change(pid, tier, field, old, new):
    return {"tier": tier, "field": field, "old": old, "new": new}


def _report(*providers):
    return {"checked_at": "2026-11-20T06:00:00+00:00", "providers": list(providers)}


def _prov(pid, status, changes=(), strategy="openrouter"):
    return {"provider_id": pid, "strategy": strategy, "status": status,
            "detail": "", "changes": list(changes)}


TABLE = {
    "verified_at": "2026-11-14", "max_age_days": 45,
    "providers": {
        "openai": {"models": {"tier_fast": {"model_id": "gpt-6-luna", "input_price_per_1m": 0.10,
                                            "output_price_per_1m": 0.50}}},
        "zero": {"models": {"tier_fast": {"model_id": "z", "input_price_per_1m": 0,
                                          "output_price_per_1m": 1.0}}},
    },
}


# ─── classification ───────────────────────────────────────────────────────────

def test_threshold_boundary_exactly_n_pct_is_significant():
    # 0.10 -> 0.105 is exactly 5% (float noise gives 4.999999...).
    assert refresh.is_significant(0.10, 0.105, 5.0)
    assert refresh.is_significant(0.10, 0.095, 5.0)
    assert not refresh.is_significant(0.10, 0.1049, 5.0)
    assert refresh.is_significant(2.0, 2.2, 10.0)
    assert not refresh.is_significant(2.0, 2.19, 10.0)


def test_table_price_zero():
    assert refresh.drift_pct(0, 0.5) is None
    assert refresh.drift_pct(0, 0) == 0.0
    assert refresh.is_significant(0, 0.01, 5.0)
    assert not refresh.is_significant(0, 0, 5.0)
    rep = _report(_prov("zero", "drift", [_change("zero", "tier_fast", "input_price_per_1m", 0.0, 0.2)]))
    cls = refresh.classify_drift(rep, TABLE, 5.0)
    assert cls["significant"] == [{"provider": "zero", "tier": "tier_fast", "model_id": "z",
                                   "field": "input_price_per_1m", "table": 0.0, "feed": 0.2,
                                   "pct": None}]
    md = refresh.render_drift_markdown(refresh.build_sla_report(rep, TABLE, date(2026, 11, 20)), {})
    assert "new (table 0)" in md


def test_classify_splits_significant_and_minor():
    rep = _report(_prov("openai", "drift", [
        _change("openai", "tier_fast", "input_price_per_1m", 0.10, 0.12),
        _change("openai", "tier_fast", "output_price_per_1m", 0.50, 0.51),
    ]))
    cls = refresh.classify_drift(rep, TABLE, 5.0)
    assert [c["field"] for c in cls["significant"]] == ["input_price_per_1m"]
    assert cls["significant"][0]["pct"] == 20.0
    assert cls["significant"][0]["model_id"] == "gpt-6-luna"
    assert [c["field"] for c in cls["minor"]] == ["output_price_per_1m"]
    assert cls["minor"][0]["pct"] == 2.0


def test_manual_fetch_failed_and_other_strategies_never_classified():
    rep = _report(
        _prov("groq", "manual", strategy="manual"),
        _prov("grok", "fetch_failed"),
        _prov("apico", "drift", [_change("apico", "tier_fast", "input_price_per_1m", 1.0, 9.0)],
              strategy="json_api"),
        _prov("openai", "match"),
    )
    cls = refresh.classify_drift(rep, TABLE, 5.0)
    assert cls["significant"] == [] and cls["minor"] == []
    assert cls["fetch_failed"] == ["grok"]
    assert cls["manual"] == ["apico", "groq"]


def test_no_feed_signal():
    assert refresh.no_feed_signal(_report(_prov("a", "fetch_failed"), _prov("b", "fetch_failed"),
                                          _prov("g", "manual", strategy="manual")))
    assert not refresh.no_feed_signal(_report(_prov("a", "fetch_failed"), _prov("b", "match")))
    assert refresh.no_feed_signal(_report(_prov("g", "manual", strategy="manual")))


# ─── fingerprint ──────────────────────────────────────────────────────────────

def test_fingerprint_stable_and_order_independent():
    a = {"provider": "openai", "tier": "tier_fast", "field": "input_price_per_1m",
         "table": 0.1, "feed": 0.12}
    b = {"provider": "grok", "tier": "tier_powerful", "field": "output_price_per_1m",
         "table": 25.0, "feed": 30.0}
    fp = refresh.drift_fingerprint([a, b])
    assert fp == refresh.drift_fingerprint([b, a])
    assert fp == refresh.drift_fingerprint([dict(b), dict(a)])
    expected = "\n".join(sorted(["openai|tier_fast|input_price_per_1m|0.1|0.12",
                                 "grok|tier_powerful|output_price_per_1m|25.0|30.0"]))
    assert fp == hashlib.sha256(expected.encode()).hexdigest()
    assert fp != refresh.drift_fingerprint([a, dict(b, feed=31.0)])
    assert refresh.drift_fingerprint([]) == hashlib.sha256(b"").hexdigest()
    # extra keys (pct, model_id) do not affect it
    assert fp == refresh.drift_fingerprint([dict(a, pct=20.0, model_id="m"), b])


def test_fingerprint_changes_when_only_the_table_value_changes():
    # main edits a field while the feed stays the same: the PR must be refreshed.
    a = {"provider": "openai", "tier": "tier_fast", "field": "input_price_per_1m",
         "table": 0.1, "feed": 0.12}
    assert refresh.drift_fingerprint([a]) != refresh.drift_fingerprint([dict(a, table=0.11)])


# ─── expiry ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("today, days, warn", [
    (date(2026, 11, 20), 39, False),     # the contract's example
    (date(2026, 12, 18), 11, False),
    (date(2026, 12, 19), 10, True),      # boundary: exactly D days left warns
    (date(2026, 12, 29), 0, True),
    (date(2027, 1, 2), -4, True),        # already expired
])
def test_expiry_maths(today, days, warn):
    e = refresh.expiry_status({"verified_at": "2026-11-14", "max_age_days": 45}, today, 10)
    assert e == {"verified_at": "2026-11-14", "max_age_days": 45, "expires_on": "2026-12-29",
                 "days_to_expiry": days, "expiry_warning": warn}


def test_expiry_markdown_states():
    rep = _report(_prov("openai", "match"))
    for today, text in ((date(2026, 11, 20), "OK, 39 day(s) left."),
                        (date(2026, 12, 25), "**Warning: expires in 4 day(s).**"),
                        (date(2027, 1, 2), "**Warning: expired 4 day(s) ago.**")):
        md = refresh.render_drift_markdown(refresh.build_sla_report(rep, TABLE, today), {})
        assert text in md


# ─── proposal ─────────────────────────────────────────────────────────────────

def test_dump_like_round_trip_is_byte_identical():
    assert refresh.dump_like(json.loads(REAL_TEXT), REAL_TEXT) == REAL_TEXT
    assert refresh.dump_like(refresh.apply_significant(json.loads(REAL_TEXT), []), REAL_TEXT) == REAL_TEXT


def test_dump_like_keeps_trailing_newline_and_escaping_choice():
    assert refresh.dump_like({"a": 1}, '{\n  "a": 1\n}') == '{\n  "a": 1\n}'
    assert refresh.dump_like({"a": "—"}, '{\n  "a": "\\u2014"\n}\n') == '{\n  "a": "\\u2014"\n}\n'
    assert refresh.dump_like({"a": "—"}, '{\n  "a": "—"\n}\n') == '{\n  "a": "—"\n}\n'


def test_apply_significant_changes_only_those_fields():
    t = json.loads(REAL_TEXT)
    t["_candidate"] = True
    sig = [{"provider": "openai", "tier": "tier_fast", "field": "input_price_per_1m", "feed": 0.12}]
    out = refresh.apply_significant(t, sig)
    assert "_candidate" not in out
    assert out["providers"]["openai"]["models"]["tier_fast"]["input_price_per_1m"] == 0.12
    assert out["verified_at"] == REAL["verified_at"]
    assert out["verified_by"] == REAL["verified_by"]
    out["providers"]["openai"]["models"]["tier_fast"]["input_price_per_1m"] = \
        REAL["providers"]["openai"]["models"]["tier_fast"]["input_price_per_1m"]
    del t["_candidate"]
    assert out == t
    assert t["providers"]["openai"]["models"]["tier_fast"]["input_price_per_1m"] != 0.12  # no mutation


# ─── Markdown ─────────────────────────────────────────────────────────────────

def test_markdown_has_checklist_urls_warning_and_marker():
    rep = _report(
        _prov("openai", "drift", [_change("openai", "tier_fast", "input_price_per_1m", 0.10, 0.12),
                                  _change("openai", "tier_fast", "output_price_per_1m", 0.50, 0.51)]),
        _prov("zero", "drift", [_change("zero", "tier_fast", "output_price_per_1m", 1.0, 0.8)]),
        _prov("grok", "fetch_failed"),
        _prov("groq", "manual", strategy="manual"),
    )
    sla = refresh.build_sla_report(rep, TABLE, date(2026, 11, 20))
    md = refresh.render_drift_markdown(sla, REAL["pricing_urls"])
    assert ("- [ ] openai tier_fast `gpt-6-luna` `input_price_per_1m`: 0.1 → 0.12, check on "
            + REAL["pricing_urls"]["openai"]) in md
    assert ("- [ ] zero tier_fast `z` `output_price_per_1m`: 1 → 0.8, check on "
            "(no pricing URL in prices.json)") in md
    assert "**These numbers come from the OpenRouter feed (proxy provenance)" in md
    assert "`verified_at` and `verified_by`" in md
    assert md.rstrip("\n").endswith(f"<!-- sc7-fingerprint: {sla['fingerprint']} -->")
    assert "| openai | tier_fast | `gpt-6-luna` | `input_price_per_1m` | 0.1 → 0.12 | +20% |" in md
    assert "| zero | tier_fast | `z` | `output_price_per_1m` | 1 → 0.8 | -20% |" in md
    minor = md.split("### Minor drifts (1")[1]
    assert "`output_price_per_1m` | 0.5 → 0.51 | +2% |" in minor
    assert "Fetch failed (no signal, never treated as drift): grok" in md
    assert "Manual (needs a manual check on the provider's page): groq" in md
    # minor drift is not in the checklist
    checklist = md.split("### Verification checklist")[1].split("###")[0]
    assert "0.51" not in checklist


def test_markdown_without_changes():
    sla = refresh.build_sla_report(_report(_prov("openai", "match")), TABLE, date(2026, 11, 20))
    md = refresh.render_drift_markdown(sla, {})
    assert "### Significant changes (0)" in md and "None." in md
    assert "Fetch failed (no signal, never treated as drift): none" in md


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _run(tmp_path, *extra):
    js, md, prop = tmp_path / "d.json", tmp_path / "d.md", tmp_path / "proposal.json"
    code = drift_check.main(["--json-out", str(js), "--md-out", str(md),
                             "--write-proposal", str(prop), *extra])
    return code, js, md, prop


def test_cli_exit_0_no_drift(prices_file, tmp_path, monkeypatch):
    calls = _stub_feed(monkeypatch, _feed_for(REAL))
    code, js, md, prop = _run(tmp_path)
    assert code == 0
    assert calls == [refresh.OPENROUTER_MODELS_URL]
    data = json.loads(js.read_text())
    assert data["significant"] == [] and data["minor"] == [] and data["fetch_failed"] == []
    assert data["manual"] == ["groq", "qwen"]
    assert data["expires_on"] == "2026-12-29" and data["days_to_expiry"] == 39
    assert data["expiry_warning"] is False and data["threshold_pct"] == 5.0
    assert set(data) == {"checked_at", "verified_at", "max_age_days", "expires_on",
                         "days_to_expiry", "expiry_warning", "threshold_pct", "significant",
                         "minor", "fetch_failed", "manual", "fingerprint"}
    assert md.exists()
    assert not prop.exists()        # proposal only on significant drift


def test_cli_exit_10_proposal_applies_only_significant(prices_file, tmp_path, monkeypatch):
    feed = _feed_for(REAL, overrides={
        ("openai", "tier_fast", "input_price_per_1m"): 0.12,      # +20% -> significant
        ("grok", "tier_powerful", "output_price_per_1m"):         # +1% -> minor
            REAL["providers"]["grok"]["models"]["tier_powerful"]["output_price_per_1m"] * 1.01,
    }, drop=("x-ai/grok-4.3",))   # grok has a missing id but still drifts
    _stub_feed(monkeypatch, feed)
    code, js, md, prop = _run(tmp_path)
    assert code == 10
    data = json.loads(js.read_text())
    assert [(c["provider"], c["field"]) for c in data["significant"]] == [("openai", "input_price_per_1m")]
    assert [(c["provider"], c["field"]) for c in data["minor"]] == [("grok", "output_price_per_1m")]
    assert f"<!-- sc7-fingerprint: {data['fingerprint']} -->" in md.read_text()

    text = prop.read_text(encoding="utf-8")
    proposal = json.loads(text)
    assert "_candidate" not in proposal
    assert proposal["verified_at"] == REAL["verified_at"]
    assert proposal["verified_by"] == REAL["verified_by"]
    assert proposal["providers"]["openai"]["models"]["tier_fast"]["input_price_per_1m"] == 0.12
    assert proposal["providers"]["grok"] == REAL["providers"]["grok"]       # minor not applied
    # Byte-level: exactly one line differs from prices.json.
    diff = [(a, b) for a, b in zip(REAL_TEXT.splitlines(), text.splitlines()) if a != b]
    assert len(REAL_TEXT.splitlines()) == len(text.splitlines())
    assert diff == [('          "input_price_per_1m": 0.1,', '          "input_price_per_1m": 0.12,')]
    assert text.endswith("}\n")


def test_cli_minor_only_exits_0_without_proposal(prices_file, tmp_path, monkeypatch):
    m = REAL["providers"]["openai"]["models"]["tier_fast"]
    _stub_feed(monkeypatch, _feed_for(REAL, overrides={
        ("openai", "tier_fast", "input_price_per_1m"): m["input_price_per_1m"] * 1.04}))
    code, js, md, prop = _run(tmp_path)
    assert code == 0 and not prop.exists()
    assert len(json.loads(js.read_text())["minor"]) == 1
    # a lower threshold makes the same drift significant
    code, js, md, prop = _run(tmp_path, "--threshold-pct", "4")
    assert code == 10 and prop.exists()


def test_cli_manual_and_fetch_failed_never_proposed(prices_file, tmp_path, monkeypatch):
    # every grok id missing from the feed -> grok fetch_failed; manual providers untouched
    _stub_feed(monkeypatch, _feed_for(REAL, overrides={
        ("openai", "tier_fast", "input_price_per_1m"): 1.0},
        drop=("x-ai/grok-4.3", "x-ai/grok-4.7")))
    code, js, md, prop = _run(tmp_path)
    assert code == 10
    data = json.loads(js.read_text())
    assert data["fetch_failed"] == ["grok"]
    proposal = json.loads(prop.read_text())
    for pid in ("grok", "groq", "qwen"):
        assert proposal["providers"][pid] == REAL["providers"][pid]
    assert {c["provider"] for c in data["significant"]} == {"openai"}


def test_cli_exit_2_when_feed_fails_everywhere(prices_file, tmp_path, monkeypatch):
    _stub_feed(monkeypatch, None)
    code, js, md, prop = _run(tmp_path)
    assert code == 2
    data = json.loads(js.read_text())
    assert len(data["fetch_failed"]) == 6 and data["significant"] == []
    # The table is valid, so the expiry fields are there for drift_pr.py to act on.
    assert data["expires_on"] == "2026-12-29" and "expiry_warning" in data
    assert md.exists() and not prop.exists()


def test_cli_exit_2_on_invalid_table(tmp_path, monkeypatch):
    bad = tmp_path / "prices.json"
    bad.write_text('{"schema_version": 1}', encoding="utf-8")
    monkeypatch.setenv("ARBITRAGE_PRICES_FILE", str(bad))
    calls = _stub_feed(monkeypatch, None)
    code, js, md, prop = _run(tmp_path)
    assert code == 2 and calls == []
    assert "price table invalid" in json.loads(js.read_text())["error"]
    assert "Check failed" in md.read_text()


def test_cli_exit_2_on_invalid_table_subprocess(tmp_path):
    # A real process: the table is rejected before any fetch, so no network.
    bad = tmp_path / "prices.json"
    bad.write_text("not json", encoding="utf-8")
    env = {"ARBITRAGE_PRICES_FILE": str(bad), "PATH": "/usr/bin:/bin"}
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "drift_check.py")],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2, r.stderr
    assert "PRICE TABLE ERROR" in r.stderr


def test_cli_exit_1_on_unexpected_error(prices_file, tmp_path, monkeypatch):
    def boom(table=None):
        raise RuntimeError("boom")
    monkeypatch.setattr(refresh, "check_drift", boom)
    code, *_ = _run(tmp_path)
    assert code == 1


def test_cli_bad_arguments_exit_1(capsys):
    with pytest.raises(SystemExit) as e:
        drift_check.main(["--threshold-pct", "-1"])
    assert e.value.code == 1
    with pytest.raises(SystemExit) as e:
        drift_check.main(["--no-such-flag"])
    assert e.value.code == 1


def test_cli_expiry_warning_does_not_change_exit_code(prices_file, tmp_path, monkeypatch):
    _stub_feed(monkeypatch, _feed_for(REAL))
    monkeypatch.setattr(drift_check, "_today", lambda: date(2026, 12, 25))
    code, js, md, prop = _run(tmp_path, "--expiry-warn-days", "5")
    assert code == 0
    data = json.loads(js.read_text())
    assert data["days_to_expiry"] == 4 and data["expiry_warning"] is True


def test_cli_outputs_optional(prices_file, monkeypatch, capsys):
    _stub_feed(monkeypatch, _feed_for(REAL))
    assert drift_check.main([]) == 0
    out = capsys.readouterr().out
    assert "significant: 0" in out and "fingerprint:" in out


def test_today_is_utc_date():
    assert isinstance(drift_check._today(), date)


# ─── odd feed values (review finding 1) ───────────────────────────────────────

@pytest.mark.parametrize("raw", ["-1", "-0.000001", "0", "0.0", 0, 0.0, -1, "inf", "-inf",
                                 "Infinity", "nan", "NaN", True, False, "", "  ", "abc",
                                 [], {}, None, "1e400"])
def test_or_price_rejects_values_that_are_not_real_prices(raw):
    assert refresh._or_price({"prompt": raw}, "prompt") is None


@pytest.mark.parametrize("raw,expected", [("0.0000025", 2.5), (" 0.0000025 ", 2.5),
                                          (2.5e-06, 2.5), ("1e-7", 0.1), (1, 1_000_000.0)])
def test_or_price_accepts_finite_positive_numbers(raw, expected):
    assert refresh._or_price({"prompt": raw}, "prompt") == pytest.approx(expected)


def _feed_with(table, raw):
    """_feed_for(table) with raw OpenRouter pricing values {(or_id, key): raw}."""
    feed = _feed_for(table)
    for m in feed["data"]:
        for (or_id, key), value in raw.items():
            if m["id"] == or_id:
                m["pricing"][key] = value
    return feed


def test_unusable_field_is_skipped_and_usable_fields_still_compared(monkeypatch):
    # anthropic: haiku's prompt is the variable-price marker "-1", but its completion
    # really drifted (0.5 -> 0.6). Only the usable field becomes a change.
    _stub_feed(monkeypatch, _feed_with(REAL, {
        ("anthropic/claude-haiku-5.5", "prompt"): "-1",
        ("anthropic/claude-haiku-5.5", "completion"): "0.0000006",
    }))
    rep = refresh.check_drift(copy.deepcopy(REAL))
    p = next(x for x in rep["providers"] if x["provider_id"] == "anthropic")
    assert p["status"] == "drift"
    assert [(c["tier"], c["field"], c["new"]) for c in p["changes"]] == [
        ("tier_fast", "output_price_per_1m", 0.6)]
    assert "unusable" in p["detail"] and "input_price_per_1m" in p["detail"]


def test_provider_with_only_unusable_fields_is_fetch_failed_not_drift(monkeypatch):
    # Consistent with a missing model id: no usable change -> fetch_failed, which is
    # never drift and never proposed (rather than a misleading "match").
    _stub_feed(monkeypatch, _feed_with(REAL, {
        ("openai/gpt-6-luna", "prompt"): "inf",
        ("openai/gpt-6-luna", "completion"): True,
    }))
    rep = refresh.check_drift(copy.deepcopy(REAL))
    p = next(x for x in rep["providers"] if x["provider_id"] == "openai")
    assert p["status"] == "fetch_failed" and p["changes"] == []
    sla = refresh.build_sla_report(rep, REAL, date(2026, 11, 20))
    assert "openai" in sla["fetch_failed"] and sla["significant"] == []


def test_absent_feed_key_is_not_counted_as_unusable(monkeypatch):
    # A key the feed simply does not carry (no cache price) stays silently skipped.
    feed = _feed_for(REAL)
    for m in feed["data"]:
        m["pricing"].pop("input_cache_read", None)
    _stub_feed(monkeypatch, feed)
    rep = refresh.check_drift(copy.deepcopy(REAL))
    assert {p["status"] for p in rep["providers"] if p["strategy"] == "openrouter"} == {"match"}


def test_reviewer_odd_feed_proposes_nothing_and_stays_strict_json(prices_file, tmp_path, monkeypatch):
    # The reviewer's demo feed: -1, inf, 0, True and nan must never become prices.
    _stub_feed(monkeypatch, _feed_with(REAL, {
        ("anthropic/claude-haiku-5.5", "prompt"): "-1",
        ("anthropic/claude-haiku-5.5", "completion"): "-1",
        ("anthropic/claude-sonnet-5.5", "prompt"): "inf",
        ("anthropic/claude-opus-5.5", "prompt"): "0",
        ("anthropic/claude-opus-5.5", "completion"): True,
        ("openai/gpt-6-luna", "prompt"): "nan",
    }))
    code, js, md, prop = _run(tmp_path)
    data = json.loads(js.read_text())
    assert code == 0 and data["significant"] == [] and data["minor"] == []
    assert not prop.exists()
    assert set(data["fetch_failed"]) == {"anthropic", "openai"}


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"), 0, 0.0, -1.0, True, "0.1"])
def test_apply_significant_refuses_non_finite_or_non_positive(bad):
    sig = [{"provider": "openai", "tier": "tier_fast", "field": "input_price_per_1m",
            "table": 0.1, "feed": bad}]
    with pytest.raises(ValueError):
        refresh.apply_significant(TABLE, sig)


def test_cli_never_writes_a_bad_proposal(prices_file, tmp_path, monkeypatch):
    # Second line of defence: even if a bad number reached the significant list,
    # the proposal writer refuses it and the run fails (exit 1) with no proposal.
    real_build = refresh.build_sla_report

    def poisoned(*a, **kw):
        sla = real_build(*a, **kw)
        sla["significant"] = [{"provider": "openai", "tier": "tier_fast", "model_id": "gpt-6-luna",
                               "field": "input_price_per_1m", "table": 0.1,
                               "feed": float("inf"), "pct": None}]
        return sla
    _stub_feed(monkeypatch, _feed_for(REAL))
    monkeypatch.setattr(refresh, "build_sla_report", poisoned)
    monkeypatch.setattr(refresh, "render_drift_markdown", lambda *a, **k: "md\n")
    code, js, md, prop = _run(tmp_path)
    assert code == 1 and not prop.exists()
