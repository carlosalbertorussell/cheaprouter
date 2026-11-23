"""SC3a regression: the shipped prices.json routes exactly as it did before
multiple models per tier existed.

tests/fixtures/shipped_routing_snapshot.json was generated from the pre-SC3a
code (commit 213ec79) over a grid of tiers, volumes, cache splits, key sets,
latency sensitivity and region filters. Each case records the winner, the
ranked failover pool, the full estimate order, the exclusions and the savings.
The projection keeps only fields that existed before SC3a, so keys SC3a adds
(ranked_entries, excluded[].model_id, savings.vs_model_id) do not affect it.

Never regenerate the fixture to make this test pass: a difference means the
shipped table now routes differently, which SC3a must not do.
"""

import json
from pathlib import Path

import providers
from router import route

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "shipped_routing_snapshot.json"

TIERS = ["tier_fast", "tier_balanced", "tier_powerful"]
VOLUMES = [(1000, 500), (200_000, 500), (20, 50_000)]
CACHE_FRACTIONS = [0.0, 0.5]
ALL = ["anthropic", "openai", "gemini", "groq", "mistral", "deepseek", "qwen", "grok"]
KEYSETS = {
    "all": ALL,
    "none": [],
    "groq": ["groq"],
    "anthropic+openai": ["anthropic", "openai"],
    "no_groq": [p for p in ALL if p != "groq"],
    "cn_only": ["deepseek", "qwen"],
}
OPTIONS = {
    "plain": {},
    "latency_sensitive": {"latency_sensitive": True},
    "us_eu_only": {"allowed_regions": ["us", "eu"]},
    "exclude_groq_openai": {"excluded_providers": ["groq", "openai"]},
}

OLD_SAVINGS_KEYS = ("saved_usd", "saved_pct", "vs_provider", "vs_cost_usd")


def shipped_registry():
    return providers._build_providers(json.loads((ROOT / "prices.json").read_text()))


def project(d) -> dict:
    return {
        "winner": None if d.winner is None else
        [d.winner.provider_id, d.winner.model_id, d.winner.total_cost_usd],
        "ranked_pool": list(d.ranked_pool or []),
        "configured": [e.provider_id for e in d.all_estimates if e.is_configured],
        # The first four words name the rule ("no API key provided", "region 'cn'
        # is globally", "latency 390ms exceeds threshold", ...); enough to pin it.
        "excluded": [[x["provider_id"], " ".join(x["reason"].split()[:4])]
                     for x in d.excluded],
        "savings": None if d.savings is None else
        {k: d.savings[k] for k in OLD_SAVINGS_KEYS},
        "deprioritized": list(d.deprioritized or []),
    }


def cases():
    for tier in TIERS:
        for inp, out in VOLUMES:
            for frac in CACHE_FRACTIONS:
                for kname, keys in KEYSETS.items():
                    for oname, opts in OPTIONS.items():
                        yield (f"{tier}|{inp}/{out}|cache{frac}|{kname}|{oname}",
                               dict(tier=tier, input_tokens=inp, output_tokens=out,
                                    cached_input_tokens=int(inp * frac),
                                    user_keys={k: "sk-test-FAKE" for k in keys},
                                    health_aware=False, **opts))


def estimate_order(d) -> list:
    """Every estimate, in ranked order. It depends only on tier, volume and
    cache split, so it is stored once per such group, not per case."""
    return [[e.provider_id, e.model_id, e.total_cost_usd] for e in d.all_estimates]


def compute() -> dict:
    reg = shipped_registry()
    out = {"cases": {}, "estimates": {}}
    for name, kw in cases():
        d = route(reg, **kw)
        out["cases"][name] = project(d)
        group = "|".join(name.split("|")[:3])
        order = estimate_order(d)
        assert out["estimates"].setdefault(group, order) == order
    return out


def test_shipped_table_routes_identically_to_pre_sc3a_snapshot():
    expected = json.loads(FIXTURE.read_text())
    actual = compute()
    for section in ("estimates", "cases"):
        exp, act = expected[section], actual[section]
        assert sorted(act) == sorted(exp)
        diffs = [name for name in exp if act[name] != exp[name]]
        assert not diffs, f"{len(diffs)} {section} changed, first: {diffs[0]}: " \
                          f"{act[diffs[0]]} != {exp[diffs[0]]}"


def test_snapshot_grid_is_not_trivial():
    expected = json.loads(FIXTURE.read_text())
    assert len(expected["cases"]) == 3 * 3 * 2 * len(KEYSETS) * len(OPTIONS)
    assert len(expected["estimates"]) == 3 * 3 * 2
    winners = {tuple(v["winner"][:2]) for v in expected["cases"].values() if v["winner"]}
    assert len(winners) >= 5   # many different winners across the grid


def test_shipped_decision_dict_keeps_every_pre_sc3a_key():
    reg = shipped_registry()
    d = route(reg, "tier_fast", 1000, 500, user_keys={p: "k" for p in ALL},
              health_aware=False).to_dict()
    assert {"winner", "all_estimates", "excluded", "savings", "tier",
            "latency_sensitive", "deprioritized", "ranked_pool"} <= set(d)
    assert all(isinstance(p, str) for p in d["ranked_pool"])


if __name__ == "__main__":   # used once, at 213ec79, to write the fixture
    FIXTURE.write_text(json.dumps(compute(), separators=(",", ":"), sort_keys=True) + "\n")
