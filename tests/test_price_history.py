"""Tests for the price-history trajectory record (SP1b)."""

import json
import pytest
import price_history as ph
from price_history import FileHistoryBackend, build_entry, record_diff


@pytest.fixture
def be(tmp_path):
    return FileHistoryBackend(str(tmp_path / "hist.jsonl"))


# ─── entry schema ─────────────────────────────────────────────────────────────

def test_build_entry_shape():
    e = build_entry(provider="deepseek", model_id="deepseek-v4-flash", tier="tier_fast",
                    field="input_price_per_1m", old=0.27, new=0.22,
                    provenance_tier="verified", source="https://x/pricing")
    assert e["provider"] == "deepseek" and e["old"] == 0.27 and e["new"] == 0.22
    assert e["provenance_tier"] == "verified"
    assert "ts" in e


def test_build_entry_rejects_bad_tier():
    with pytest.raises(ValueError):
        build_entry(provider="x", model_id="m", tier="tier_fast", field="input_price_per_1m",
                    old=1.0, new=2.0, provenance_tier="not-a-tier")


def test_build_entry_allows_none_old():
    e = build_entry(provider="x", model_id="m", tier="tier_fast", field="cached_input_price_per_1m",
                    old=None, new=0.1, provenance_tier="verified")
    assert e["old"] is None and e["new"] == 0.1


# ─── file backend round-trip ──────────────────────────────────────────────────

def test_append_read_roundtrip(be):
    be.append(build_entry(provider="groq", model_id="g", tier="tier_fast",
                          field="input_price_per_1m", old=0.05, new=0.04,
                          provenance_tier="proxy"))
    rows = be.read()
    assert len(rows) == 1 and rows[0]["provider"] == "groq"


def test_read_filters(be):
    for p in ("groq", "groq", "deepseek"):
        be.append(build_entry(provider=p, model_id="m", tier="tier_fast",
                              field="input_price_per_1m", old=1.0, new=2.0,
                              provenance_tier="verified"))
    assert len(be.read(provider="groq")) == 2
    assert len(be.read(provider="deepseek")) == 1


def test_read_newest_first(be):
    be.append(build_entry(provider="x", model_id="m", tier="tier_fast",
                          field="input_price_per_1m", old=1, new=2, provenance_tier="verified", ts="2026-01-01T00:00:00Z"))
    be.append(build_entry(provider="x", model_id="m", tier="tier_fast",
                          field="input_price_per_1m", old=2, new=3, provenance_tier="verified", ts="2026-02-01T00:00:00Z"))
    rows = be.read()
    assert rows[0]["new"] == 3   # newest first


def test_metadata_only_no_leak(be):
    # even if someone shoves content/keys into an entry dict, append sanitizes it out
    be.append({"provider": "x", "model_id": "m", "tier": "tier_fast",
               "field": "input_price_per_1m", "old": 1, "new": 2,
               "provenance_tier": "verified", "ts": "t",
               "api_key": "sk-LEAK", "content": "secret prompt"})
    raw = open(be.path).read()
    assert "sk-LEAK" not in raw and "secret prompt" not in raw


# ─── record_diff (table -> history) ───────────────────────────────────────────

def _table(price):
    return {"providers": {"deepseek": {"models": {"tier_fast": {
        "model_id": "deepseek-v4-flash", "input_price_per_1m": price,
        "output_price_per_1m": 0.66}}}}}


def test_record_diff_detects_change(be):
    entries = record_diff(_table(0.27), _table(0.22), provenance_tier="verified",
                          source="src", backend=be)
    changed = [e for e in entries if e["field"] == "input_price_per_1m"]
    assert len(changed) == 1
    assert changed[0]["old"] == 0.27 and changed[0]["new"] == 0.22


def test_record_diff_no_change_no_entry(be):
    entries = record_diff(_table(0.27), _table(0.27), provenance_tier="verified", backend=be)
    assert entries == []


def test_record_diff_new_field_recorded(be):
    old = {"providers": {"x": {"models": {"tier_fast": {"model_id": "m",
           "input_price_per_1m": 1.0, "output_price_per_1m": 2.0}}}}}
    new = {"providers": {"x": {"models": {"tier_fast": {"model_id": "m",
           "input_price_per_1m": 1.0, "output_price_per_1m": 2.0,
           "cached_input_price_per_1m": 0.1}}}}}
    entries = record_diff(old, new, provenance_tier="verified", backend=be)
    cache = [e for e in entries if e["field"] == "cached_input_price_per_1m"]
    assert len(cache) == 1 and cache[0]["old"] is None and cache[0]["new"] == 0.1


# ─── trajectory & summary ─────────────────────────────────────────────────────

def test_trajectory_oldest_first(be, monkeypatch):
    monkeypatch.setattr(ph, "get_backend", lambda: be)
    be.append(build_entry(provider="d", model_id="m", tier="tier_fast",
                          field="input_price_per_1m", old=0.3, new=0.27, provenance_tier="verified", ts="2026-01-01T00:00:00Z"))
    be.append(build_entry(provider="d", model_id="m", tier="tier_fast",
                          field="input_price_per_1m", old=0.27, new=0.22, provenance_tier="verified", ts="2026-03-01T00:00:00Z"))
    traj = ph.trajectory("d", "m", "input_price_per_1m")
    assert [p["price"] for p in traj] == [0.27, 0.22]   # oldest -> newest


def test_summary(be, monkeypatch):
    monkeypatch.setattr(ph, "get_backend", lambda: be)
    for p in ("groq", "deepseek", "deepseek"):
        be.append(build_entry(provider=p, model_id="m", tier="tier_fast",
                              field="input_price_per_1m", old=1, new=2, provenance_tier="verified"))
    s = ph.summary()
    assert s["total_changes"] == 3
    assert s["changes_by_provider"]["deepseek"] == 2
