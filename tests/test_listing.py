"""MCPIZE_LISTING.md's pricing block must match prices.json (ST4)."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("gen_listing", ROOT / "scripts" / "gen_listing.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def table():
    return json.loads((ROOT / "prices.json").read_text())


def _block(text, gen):
    return text[text.index(gen.BEGIN):text.index(gen.END) + len(gen.END)]


def test_listing_prices_match_prices_json(gen, table):
    listing = (ROOT / "MCPIZE_LISTING.md").read_text()
    assert _block(listing, gen) == gen.render(table), \
        "MCPIZE_LISTING.md is out of date: run python scripts/gen_listing.py"


def test_check_mode_passes_on_repo(gen):
    assert gen.main(["--check"]) == 0


def test_every_model_in_prices_json_is_listed(gen, table):
    block = gen.render(table)
    for p in table["providers"].values():
        for m in p["models"].values():
            assert f"| {m['model_id']} |" in block


def test_a_price_change_is_detected(gen, table):
    t = copy.deepcopy(table)
    t["providers"]["groq"]["models"]["tier_fast"]["input_price_per_1m"] = 0.06
    assert gen.render(t) != gen.render(table)
    assert "| llama-3.1-8b-instant | 0.06 |" in gen.render(t)


def test_carried_providers_from_notes(gen, table):
    assert gen.carried_providers(table) == ["groq", "qwen"]
    t = copy.deepcopy(table)
    t["notes"] = {}
    assert gen.carried_providers(t) == []
    assert "Carried, not re-read on" not in gen.render(t)


def test_tier_winners_are_computed(gen, table):
    w = gen.tier_winners(table)
    assert w == {"tier_fast": "Groq llama-3.1-8b-instant",
                 "tier_balanced": "DeepSeek deepseek-flash",
                 "tier_powerful": "Groq llama-3.3-70b-versatile"}


def test_format_prices(gen):
    assert [gen._fmt(x) for x in (0.1, 2, 0.025, 0.003, None)] == \
        ["0.10", "2.00", "0.025", "0.003", "—"]


def test_listing_drops_retired_models_and_unsupported_claims():
    text = (ROOT / "MCPIZE_LISTING.md").read_text()
    for gone in ("claude-haiku-4-5", "gpt-4o", "| o3 |", "deepseek-v4-flash", "grok-4.6",
                 "verified 2026-10-12", "semantically equivalent", "clears the tier",
                 "clears\n> the task"):
        assert gone not in text, gone
