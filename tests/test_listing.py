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
    assert "| openai/gpt-oss-20b | 0.06 |" in gen.render(t)


def test_carried_providers_from_notes(gen, table):
    # SC1d re-read Groq and Qwen, so nothing in the shipped table is carried.
    assert gen.carried_providers(table) == []
    assert "Carried, not re-read on" not in gen.render(table)
    t = copy.deepcopy(table)
    t["notes"]["groq_lifecycle"] = "CARRIED, NOT RE-READ: test"
    assert gen.carried_providers(t) == ["groq"]
    assert "**Carried, not re-read on 2026-11-14:** Groq." in gen.render(t)
    assert "| Groq | fast | openai/gpt-oss-20b | 0.075 | 0.30 | 0.037 | carried, not re-read |" \
        in gen.render(t)


def test_tier_winners_are_computed(gen, table):
    w = gen.tier_winners(table)
    # tier_balanced is an exact cost tie (Groq gpt-oss-120b and DeepSeek
    # deepseek-flash are both 0.15/0.60); the stable sort keeps prices.json order.
    assert w == {"tier_fast": "Groq openai/gpt-oss-20b",
                 "tier_balanced": "Groq openai/gpt-oss-120b",
                 "tier_powerful": "Groq openai/gpt-oss-120b"}


def test_format_prices(gen):
    assert [gen._fmt(x) for x in (0.1, 2, 0.025, 0.003, None)] == \
        ["0.10", "2.00", "0.025", "0.003", "—"]


def test_listing_drops_retired_models_and_unsupported_claims():
    text = (ROOT / "MCPIZE_LISTING.md").read_text()
    for gone in ("claude-haiku-4-5", "gpt-4o", "| o3 |", "deepseek-v4-flash", "grok-4.6",
                 "llama-3.1-8b-instant", "llama-3.3-70b-versatile", "carried, not re-read",
                 "verified 2026-10-12", "semantically equivalent", "clears the tier",
                 "clears\n> the task"):
        assert gone not in text, gone
