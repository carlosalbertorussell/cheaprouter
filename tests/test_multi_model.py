"""SC3a: a provider may list several models in a tier.

Every test here builds a SYNTHETIC table; none touches the shipped prices.json
(tests/test_shipped_routing_snapshot.py pins that one). Three providers:

  alpha (us, 180ms)  fast: [alpha-mini, alpha-nano]   (nano, listed second, is cheapest)
                     balanced: [alpha-mid, alpha-zz]  (zz ties beta's and gamma's mids)
                     powerful: [alpha-max]            (a list of one)
  beta  (us, 180ms)  fast: beta-fast                  balanced: [beta-mid-b, beta-mid-a] (exact tie)
  gamma (eu, 220ms)  fast: gamma-small (ties beta-fast on cost)   balanced: gamma-mid (ties too)
"""

import asyncio
import copy
import importlib
import importlib.util
import json
import random
from datetime import date
from pathlib import Path

import httpx
import pytest

import pricing_table as pt
import providers
import refresh
from pricing import estimate_all_providers
from router import route

ROOT = Path(__file__).resolve().parent.parent
FAKE = "sk-test-FAKE"
KEYS = {"alpha": FAKE, "beta": FAKE, "gamma": FAKE}


def _m(mid, inp, out, cache=None, ctx=100_000):
    m = {"model_id": mid, "input_price_per_1m": inp, "output_price_per_1m": out,
         "context_window": ctx}
    if cache is not None:
        m["cached_input_price_per_1m"] = cache
    return m


def _prov(name, region, host, models, refresh_cfg=None):
    p = {"name": name, "region": region, "base_url": f"https://{host}",
         "api_key_env": f"{name.upper()}_SC3A_TEST_KEY", "protocol": "openai", "models": models}
    if refresh_cfg is not None:
        p["refresh"] = refresh_cfg
    return p


def synthetic_table() -> dict:
    return {
        "schema_version": 1, "verified_at": "2026-11-20", "max_age_days": 45,
        "verified_by": "synthetic SC3a test table", "notes": {},
        "pricing_urls": {"alpha": "https://alpha.test/pricing"},
        "providers": {
            "alpha": _prov("Alpha", "us", "api.alpha.test", {
                "tier_fast": [_m("alpha-mini", 0.08, 0.30), _m("alpha-nano", 0.05, 0.20, cache=0.01)],
                "tier_balanced": [_m("alpha-mid", 1.0, 4.0), _m("alpha-zz", 0.5, 2.0)],
                "tier_powerful": [_m("alpha-max", 3.0, 12.0)],
            }, {"strategy": "openrouter", "ids": {
                "tier_fast": {"alpha-mini": "alpha/mini", "alpha-nano": "alpha/nano"},
                "tier_balanced": {"alpha-mid": "alpha/mid"},     # alpha-zz not mapped
                "tier_powerful": "alpha/max",                    # a list of one may use a string
            }}),
            "beta": _prov("Beta", "us", "api.beta.test", {
                "tier_fast": _m("beta-fast", 0.10, 0.40),
                "tier_balanced": [_m("beta-mid-b", 0.5, 2.0), _m("beta-mid-a", 0.5, 2.0)],
                "tier_powerful": _m("beta-max", 2.0, 8.0),
            }, {"strategy": "openrouter", "ids": {"tier_fast": "beta/fast"}}),
            "gamma": _prov("Gamma", "eu", "api.gamma.test", {
                "tier_fast": [_m("gamma-small", 0.10, 0.40)],
                "tier_balanced": _m("gamma-mid", 0.5, 2.0),
                "tier_powerful": _m("gamma-max", 5.0, 20.0),
            }, {"strategy": "manual"}),
        },
    }


def registry(table=None):
    return providers._build_providers(table or synthetic_table())


@pytest.fixture(autouse=True)
def _synthetic_ids_resolve(monkeypatch):
    """resolve_api_key only knows provider ids in providers.PROVIDERS, so the
    synthetic ids must be registered for BYOK keys to count (route_env reloads
    replace this with the same synthetic registry)."""
    monkeypatch.setattr(providers, "PROVIDERS", registry())


def entries(d):
    return [(e["provider_id"], e["model_id"]) for e in d.ranked_entries]


# ─── schema and validation ────────────────────────────────────────────────────

def test_registry_normalises_every_tier_to_a_list():
    reg = registry()
    assert [m.model_id for m in reg["alpha"].models_for("tier_fast")] == ["alpha-mini", "alpha-nano"]
    assert [m.model_id for m in reg["beta"].models_for("tier_fast")] == ["beta-fast"]
    assert reg["alpha"].model_for("tier_fast").model_id == "alpha-mini"       # first listed
    assert reg["alpha"].model_for("tier_fast", "alpha-nano").cached_input_price_per_1m == 0.01
    assert reg["alpha"].model_for("tier_fast", "nope") is None
    assert reg["alpha"].models_for("tier_unknown") == []


def _broken(edit):
    t = synthetic_table()
    edit(t["providers"])
    return t


@pytest.mark.parametrize("edit, match", [
    (lambda p: p["alpha"]["models"].__setitem__("tier_fast", []), "empty list"),
    (lambda p: p["alpha"]["models"]["tier_fast"].append(_m("alpha-mini", 9, 9)),
     "lists model_id 'alpha-mini' more than once"),
    (lambda p: p["beta"]["models"].__setitem__("tier_fast", "beta-fast"),
     "must be a model object or a non-empty list"),
    (lambda p: p["alpha"]["models"]["tier_fast"].append("alpha-x"), "model #2 is not a model object"),
    (lambda p: p["alpha"]["models"]["tier_fast"][1].pop("output_price_per_1m"),
     r"model #1 missing fields: \['output_price_per_1m'\]"),
    (lambda p: p["alpha"]["models"]["tier_fast"][0].__setitem__("input_price_per_1m", -1),
     "model #0 has invalid input_price_per_1m"),
    (lambda p: p["alpha"]["models"]["tier_fast"][0].__setitem__("model_id", ""), "invalid model_id"),
    (lambda p: p["alpha"]["refresh"]["ids"].__setitem__("tier_fast", "alpha/mini"),
     "is one id but the tier lists 2 models"),
    (lambda p: p["alpha"]["refresh"]["ids"]["tier_fast"].__setitem__("alpha-gone", "a/g"),
     "names model_id 'alpha-gone'"),
    (lambda p: p["alpha"]["refresh"]["ids"]["tier_fast"].__setitem__("alpha-mini", ""),
     "must be a non-empty string"),
    (lambda p: p["alpha"]["refresh"]["ids"].__setitem__("tier_fast", ["alpha/mini"]),
     "must be a string or a"),
    (lambda p: p["beta"]["refresh"]["ids"].__setitem__("tier_mega", "beta/mega"), "names tier 'tier_mega'"),
    (lambda p: p["beta"]["refresh"].__setitem__("ids", ["beta/fast"]), "refresh.ids must be an object"),
])
def test_validation_errors_are_loud(edit, match):
    t = _broken(edit)
    with pytest.raises(pt.PriceTableError, match=match):
        pt.validate_providers(t["providers"])
    with pytest.raises(pt.PriceTableError, match=match):
        providers._build_providers(t)       # synthetic tables get the same checks


def test_duplicate_model_id_in_different_tiers_is_fine():
    t = synthetic_table()
    t["providers"]["beta"]["models"]["tier_powerful"] = _m("beta-fast", 0.10, 0.40)
    pt.validate_providers(t["providers"])      # the shipped table does this (groq 120b)


def test_loader_rejects_a_bad_list_tier_at_import(tmp_path, monkeypatch):
    import pricing_table
    t = _broken(lambda p: p["alpha"]["models"].__setitem__("tier_fast", []))
    f = tmp_path / "prices.json"
    f.write_text(json.dumps(t))
    monkeypatch.setenv("ARBITRAGE_PRICES_FILE", str(f))
    try:
        # reload redefines PriceTableError before raising, so match by name
        with pytest.raises(Exception, match="empty list") as exc:
            importlib.reload(pricing_table)
        assert type(exc.value).__name__ == "PriceTableError"
        f.write_text(json.dumps(synthetic_table()))
        pt = importlib.reload(pricing_table)
        assert isinstance(pt.raw_table()["providers"]["alpha"]["models"]["tier_fast"], list)
    finally:
        monkeypatch.delenv("ARBITRAGE_PRICES_FILE")
        importlib.reload(pricing_table)


# ─── ranking ──────────────────────────────────────────────────────────────────

def test_cheapest_model_across_providers_and_models_wins():
    d = route(registry(), "tier_fast", 1000, 500, user_keys=KEYS, health_aware=False)
    assert (d.winner.provider_id, d.winner.model_id) == ("alpha", "alpha-nano")
    assert entries(d) == [("alpha", "alpha-nano"), ("alpha", "alpha-mini"),
                          ("beta", "beta-fast"), ("gamma", "gamma-small")]
    assert d.ranked_pool == ["alpha", "beta", "gamma"]       # provider ids, each once
    assert len(d.all_estimates) == 4
    # savings baseline is the priciest model in the pool (beta-fast and gamma-small
    # tie; the first in ranked order is named)
    assert d.savings["vs_provider"] == "Beta" and d.savings["vs_model_id"] == "beta-fast"


def test_second_listed_model_can_win_even_when_its_provider_is_not_cheapest_first():
    # Only beta and alpha keyed; alpha's second-listed model still beats beta.
    d = route(registry(), "tier_fast", 1000, 500, user_keys={"beta": FAKE, "alpha": FAKE},
              health_aware=False)
    assert d.winner.model_id == "alpha-nano"
    assert {x["provider_id"] for x in d.excluded} == {"gamma"}
    assert d.excluded[0]["model_id"] == "gamma-small"


def test_cache_split_can_flip_the_winning_model_within_a_provider():
    t = synthetic_table()
    # alpha-mini gets a deep cache discount; alpha-nano none.
    t["providers"]["alpha"]["models"]["tier_fast"][0]["cached_input_price_per_1m"] = 0.001
    t["providers"]["alpha"]["models"]["tier_fast"][1].pop("cached_input_price_per_1m")
    reg = registry(t)
    plain = route(reg, "tier_fast", 100_000, 10, user_keys={"alpha": FAKE}, health_aware=False)
    cached = route(reg, "tier_fast", 100_000, 10, user_keys={"alpha": FAKE}, health_aware=False,
                   cached_input_tokens=100_000)
    assert plain.winner.model_id == "alpha-nano"
    assert cached.winner.model_id == "alpha-mini"


def _shuffled(seed):
    t = synthetic_table()
    rng = random.Random(seed)
    items = list(t["providers"].items())
    rng.shuffle(items)
    for _, p in items:
        for tier, entry in p["models"].items():
            if isinstance(entry, list):
                rng.shuffle(entry)
    t["providers"] = dict(items)
    return t


@pytest.mark.parametrize("tier", ["tier_fast", "tier_balanced", "tier_powerful"])
def test_tie_break_is_deterministic_across_provider_and_model_order(tier):
    ref = entries(route(registry(), tier, 1000, 500, user_keys=KEYS, health_aware=False))
    for seed in range(25):
        d = route(registry(_shuffled(seed)), tier, 1000, 500, user_keys=KEYS, health_aware=False)
        assert entries(d) == ref


def test_tie_break_cost_then_latency_then_provider_then_model_id():
    est = estimate_all_providers(registry(), "tier_balanced", 1000, 500, KEYS)
    order = [(e.provider_id, e.model_id) for e in est]
    # alpha-zz, beta-mid-a, beta-mid-b, gamma-mid all cost 0.0015. alpha and beta
    # are us (180ms), gamma eu (220ms): provider_id decides alpha < beta, model_id
    # decides beta-mid-a < beta-mid-b (listed b first), latency puts gamma after.
    assert order == [("alpha", "alpha-zz"), ("beta", "beta-mid-a"), ("beta", "beta-mid-b"),
                     ("gamma", "gamma-mid"), ("alpha", "alpha-mid")]
    assert len({e.total_cost_usd for e in est[:4]}) == 1


def test_health_is_per_provider_and_sinks_every_model_of_it():
    snap = {"alpha": {"score": 0.0, "samples": 20, "healthy": False}}
    d = route(registry(), "tier_fast", 1000, 500, user_keys=KEYS, health_snapshot=snap)
    assert entries(d) == [("beta", "beta-fast"), ("gamma", "gamma-small"),
                          ("alpha", "alpha-nano"), ("alpha", "alpha-mini")]
    assert d.deprioritized == ["alpha"]                     # once, not per model
    assert d.ranked_pool == ["beta", "gamma", "alpha"]


def test_decision_dict_has_entries_and_keeps_shapes():
    d = route(registry(), "tier_fast", 1000, 500, user_keys=KEYS, health_aware=False).to_dict()
    assert d["ranked_entries"][:2] == [{"provider_id": "alpha", "model_id": "alpha-nano"},
                                       {"provider_id": "alpha", "model_id": "alpha-mini"}]
    assert d["ranked_pool"] == ["alpha", "beta", "gamma"]
    assert d["winner"]["model_id"] == "alpha-nano"
    empty = route(registry(), "tier_fast", 1000, 500, user_keys={}, health_aware=False).to_dict()
    assert empty["ranked_entries"] == [] and empty["ranked_pool"] == []
    assert len(empty["excluded"]) == 4 and all("model_id" in x for x in empty["excluded"])


# ─── single object vs list of one ─────────────────────────────────────────────

def _as_list_of_one(t):
    t = copy.deepcopy(t)
    for p in t["providers"].values():
        for tier, entry in list(p["models"].items()):
            if isinstance(entry, dict):
                p["models"][tier] = [entry]
    return t


def _as_single(t):
    t = copy.deepcopy(t)
    for p in t["providers"].values():
        for tier, entry in list(p["models"].items()):
            if isinstance(entry, list) and len(entry) == 1:
                p["models"][tier] = entry[0]
    return t


@pytest.mark.parametrize("tier", ["tier_fast", "tier_balanced", "tier_powerful"])
@pytest.mark.parametrize("keys", [KEYS, {"beta": FAKE}, {"gamma": FAKE, "alpha": FAKE}])
def test_single_object_and_list_of_one_route_identically(tier, keys):
    a = route(registry(_as_single(synthetic_table())), tier, 5000, 700, user_keys=keys,
              health_aware=False, cached_input_tokens=1000)
    b = route(registry(_as_list_of_one(synthetic_table())), tier, 5000, 700, user_keys=keys,
              health_aware=False, cached_input_tokens=1000)
    assert a.to_dict() == b.to_dict()


def test_shipped_shape_as_list_of_one_routes_like_the_shipped_shape():
    # The shipped table's structure (single objects) rewritten as lists of one
    # routes the same: read-only use of its numbers, nothing written back.
    shipped = json.loads((ROOT / "prices.json").read_text())
    providers.PROVIDERS = registry(shipped)      # restored by the autouse monkeypatch
    keys = {pid: FAKE for pid in shipped["providers"]}
    for tier in ("tier_fast", "tier_balanced", "tier_powerful"):
        a = route(registry(shipped), tier, 1000, 500, user_keys=keys, health_aware=False)
        b = route(registry(_as_list_of_one(shipped)), tier, 1000, 500, user_keys=keys,
                  health_aware=False)
        assert a.to_dict() == b.to_dict()


# ─── server tools, end to end ─────────────────────────────────────────────────

@pytest.fixture
def env(route_env):
    """route_env with the synthetic table in place of the shipped providers."""
    def use(table):
        route_env.write_table(lambda t: t.update(providers=table["providers"],
                                                 pricing_urls=table["pricing_urls"]))
        return route_env.server
    route_env.use = use
    route_env.use(synthetic_table())
    return route_env


def _ok(model):
    return httpx.Response(200, json={"model": model, "choices": [{"message": {"content": "ok"}}],
                                     "usage": {"prompt_tokens": 3, "completion_tokens": 1}})


NOT_FOUND = {"error": {"message": "The model does not exist", "code": "model_not_found"}}


def _handler(plan):
    """plan: {model_id: callable(model_id) -> Response}; unlisted models answer 200."""
    def handle(req):
        model = json.loads(req.content)["model"]
        return plan.get(model, _ok)(model)
    return handle


def _route(server, **kw):
    base = dict(messages=[{"role": "user", "content": "hello"}], tier="tier_fast",
                health_aware=False, session="sc3a", api_keys=dict(KEYS))
    base.update(kw)
    return json.loads(asyncio.run(server.arbitrage_route_completion(
        server.RouteCompletionInput(**base))))


def _sent(http):
    return [(r.url.host, json.loads(r.content)["model"]) for r in http.requests]


def test_route_uses_the_winning_model(env, mock_http):
    http = mock_http(_handler({}))
    out = _route(env.server)
    r = out["routing"]
    assert (r["provider_id"], r["model_id"], r["model"]) == ("alpha", "alpha-nano", "alpha-nano")
    assert _sent(http) == [("api.alpha.test", "alpha-nano")]
    assert r["attempts"] == [{"provider_id": "alpha", "model_id": "alpha-nano",
                              "outcome": "success", "streamed": False}]
    # actual cost priced on the model that answered: 3 in, 1 out at nano rates
    assert out["cost"]["actual_cost_usd"] == pytest.approx(3 * 0.05e-6 + 1 * 0.20e-6)
    assert out["cost"]["savings"]["vs_model_id"] == "beta-fast"


def test_model_unavailable_fails_over_to_same_providers_next_model(env, mock_http):
    http = mock_http(_handler({"alpha-nano": lambda m: httpx.Response(404, json=NOT_FOUND)}))
    out = _route(env.server)
    r = out["routing"]
    assert _sent(http) == [("api.alpha.test", "alpha-nano"), ("api.alpha.test", "alpha-mini")]
    assert [(a["provider_id"], a["model_id"], a["outcome"]) for a in r["attempts"]] == [
        ("alpha", "alpha-nano", "model_unavailable"), ("alpha", "alpha-mini", "success")]
    assert (r["provider_id"], r["model_id"], r["failed_over"]) == ("alpha", "alpha-mini", True)
    assert r["warnings"] == ["alpha model alpha-nano reported unavailable; update prices.json"]
    # actual cost is alpha-mini's, not the routed winner's
    assert out["cost"]["actual_cost_usd"] == pytest.approx(3 * 0.08e-6 + 1 * 0.30e-6)


def test_single_key_caller_falls_back_within_the_provider(env, mock_http):
    mock_http(_handler({"alpha-nano": lambda m: httpx.Response(400, json=NOT_FOUND)}))
    out = _route(env.server, api_keys={"alpha": FAKE})
    assert out["routing"]["model_id"] == "alpha-mini"


def test_both_models_retired_then_next_provider(env, mock_http):
    gone = lambda m: httpx.Response(404, json=NOT_FOUND)
    http = mock_http(_handler({"alpha-nano": gone, "alpha-mini": gone}))
    out = _route(env.server, max_failover=2)
    assert [h for h, _ in _sent(http)] == ["api.alpha.test", "api.alpha.test", "api.beta.test"]
    assert out["routing"]["model_id"] == "beta-fast"
    assert len(out["routing"]["warnings"]) == 2


def test_max_failover_counts_models_not_providers(env, mock_http):
    gone = lambda m: httpx.Response(404, json=NOT_FOUND)
    http = mock_http(_handler({"alpha-nano": gone, "alpha-mini": gone}))
    out = _route(env.server, max_failover=1)
    assert out["error"] == "All attempted providers failed."
    assert len(http.requests) == 2


@pytest.mark.parametrize("code", [401, 403])
def test_bad_key_still_stops(env, mock_http, code):
    http = mock_http(_handler({"alpha-nano": lambda m: httpx.Response(code, json=NOT_FOUND)}))
    out = _route(env.server)
    assert out["error"] == "All attempted providers failed."
    assert [(a["model_id"], a["outcome"]) for a in out["attempts"]] == [("alpha-nano", "error")]
    assert len(http.requests) == 1
    assert "warnings" not in out["routing"]


def test_transient_error_walks_entries_in_ranked_order(env, mock_http):
    http = mock_http(_handler({"alpha-nano": lambda m: httpx.Response(503)}))
    out = _route(env.server)
    assert out["routing"]["model_id"] == "alpha-mini"
    assert out["routing"]["attempts"][0]["outcome"] == "transient_error"


def test_streaming_fails_over_to_next_model_before_first_token(env, mock_http):
    def handle(req):
        model = json.loads(req.content)["model"]
        if model == "alpha-nano":
            return httpx.Response(404, json=NOT_FOUND)
        lines = [f'data: {{"model":"{model}","choices":[{{"delta":{{"content":"ok"}}}}]}}',
                 'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1}}',
                 "data: [DONE]"]
        return httpx.Response(200, content=("\n".join(lines) + "\n").encode())
    mock_http(handle)
    out = _route(env.server, stream=True)
    assert out["response"] == "ok"
    assert [(a["model_id"], a["outcome"]) for a in out["routing"]["attempts"]] == [
        ("alpha-nano", "model_unavailable"), ("alpha-mini", "success")]


def test_failed_model_unavailable_is_not_counted_against_health(env, mock_http):
    import health
    mock_http(_handler({"alpha-nano": lambda m: httpx.Response(404, json=NOT_FOUND)}))
    for s in ("s1", "s2", "s3"):
        _route(env.server, session=s)
    lines = [json.loads(x) for x in env.history.read_text().splitlines()]
    fail = [x for x in lines if x.get("success") is False]
    assert fail and all(x["transient"] is False for x in fail)
    assert "model_id" not in fail[0]                          # history schema unchanged
    assert health.is_healthy("alpha", health.provider_health())


def test_estimate_cost_lists_every_model(env):
    out = json.loads(asyncio.run(env.server.arbitrage_estimate_cost(
        env.server.EstimateCostInput(tier="tier_fast", input_tokens=1000, output_tokens=500,
                                     api_keys={"alpha": FAKE}))))
    assert [(e["provider_id"], e["model_id"]) for e in out["all_estimates"]] == [
        ("alpha", "alpha-nano"), ("alpha", "alpha-mini"), ("beta", "beta-fast"),
        ("gamma", "gamma-small")]
    assert out["ranked_entries"] == [{"provider_id": "alpha", "model_id": "alpha-nano"},
                                     {"provider_id": "alpha", "model_id": "alpha-mini"}]
    assert out["ranked_pool"] == ["alpha"]
    assert out["savings"]["vs_model_id"] == "alpha-mini"


def test_get_pricing_lists_every_model(env):
    md = asyncio.run(env.server.arbitrage_get_pricing(
        env.server.GetPricingInput(tier="tier_balanced", api_keys={"beta": FAKE})))
    for mid in ("alpha-mid", "alpha-zz", "beta-mid-a", "beta-mid-b", "gamma-mid"):
        assert f"`{mid}`" in md
    assert md.index("`alpha-zz`") < md.index("`beta-mid-a`") < md.index("`beta-mid-b`")
    assert "**Cheapest (your keys):** Beta (`beta-mid-a`)" in md


def test_provider_status_mirrors_the_table_shape(env):
    out = json.loads(asyncio.run(env.server.arbitrage_provider_status(
        env.server.ProviderStatusInput())))
    alpha, beta = out["providers"]["alpha"]["models"], out["providers"]["beta"]["models"]
    assert [m["model_id"] for m in alpha["tier_fast"]] == ["alpha-mini", "alpha-nano"]
    assert alpha["tier_powerful"]["model_id"] == "alpha-max"      # list of one -> object
    assert beta["tier_fast"] == {"model_id": "beta-fast", "input_price_per_1m_usd": 0.1,
                                 "output_price_per_1m_usd": 0.4, "context_window": 100_000}


def test_pricing_server_get_and_list(env, monkeypatch):
    import pricing_server as ps
    monkeypatch.setattr(ps, "PROVIDERS", providers.PROVIDERS)
    run = lambda c: json.loads(asyncio.run(c))
    many = run(ps.pricing_get(ps.PricingGetInput(provider="alpha", tier="tier_fast")))
    assert "model_id" not in many
    assert [m["model_id"] for m in many["models"]] == ["alpha-mini", "alpha-nano"]
    assert many["models"][1]["price_per_1m_usd"] == {"input_price_per_1m": 0.05,
                                                     "output_price_per_1m": 0.2,
                                                     "cached_input_price_per_1m": 0.01}
    assert many["provenance"]["source"] == "https://alpha.test/pricing"
    one = run(ps.pricing_get(ps.PricingGetInput(provider="alpha", tier="tier_fast",
                                                model_id="alpha-nano")))
    assert one["model_id"] == "alpha-nano" and "models" not in one
    single = run(ps.pricing_get(ps.PricingGetInput(provider="alpha", tier="tier_powerful")))
    assert single["model_id"] == "alpha-max"                       # list of one: flat shape
    bad = run(ps.pricing_get(ps.PricingGetInput(provider="alpha", tier="tier_fast",
                                                model_id="nope")))
    assert "lists no model 'nope'" in bad["error"] and bad["known"] == ["alpha-mini", "alpha-nano"]
    rows = run(ps.pricing_list(ps.PricingListInput(tier="tier_fast")))["prices"]
    assert [(r["provider"], r["model_id"]) for r in rows] == [
        ("alpha", "alpha-mini"), ("alpha", "alpha-nano"), ("beta", "beta-fast"),
        ("gamma", "gamma-small")]
    assert run(ps.pricing_list(ps.PricingListInput()))["count"] == 12   # 5 + 4 + 3 models


def test_observed_signal_carries_the_model_that_answered(env, mock_http, monkeypatch, tmp_path):
    import price_history as ph
    monkeypatch.setenv("ARBITRAGE_EMIT_OBSERVED", "1")
    be = ph.FileHistoryBackend(str(tmp_path / "ph.jsonl"))
    monkeypatch.setattr(ph, "get_backend", lambda: be)
    mock_http(_handler({"alpha-nano": lambda m: httpx.Response(404, json=NOT_FOUND)}))
    _route(env.server)
    obs = ph.observed_entries()
    assert {(e["model_id"], e["field"], e["new"]) for e in obs} == {
        ("alpha-mini", "input_price_per_1m", 0.08), ("alpha-mini", "output_price_per_1m", 0.3)}
    v = refresh.observed_validation(table=synthetic_table())
    assert v["summary"]["confirms"] == 2 and v["summary"]["diverges"] == 0


# ─── drift (SC2 check, SC7 proposal and fingerprint) ──────────────────────────

def _feed(table, overrides=None):
    """An OpenRouter feed matching every mapped model, with {or_id: {field: per_1m}}."""
    overrides = overrides or {}
    by_id = {}
    for pid, prov in table["providers"].items():
        cfg = prov.get("refresh") or {}
        if cfg.get("strategy") != "openrouter":
            continue
        for tier, ref in cfg["ids"].items():
            ms = prov["models"][tier]
            ms = ms if isinstance(ms, list) else [ms]
            pairs = [(ms[0], ref)] if isinstance(ref, str) else \
                [(next(m for m in ms if m["model_id"] == mid), oid) for mid, oid in ref.items()]
            for m, oid in pairs:
                pricing = by_id.setdefault(oid, {})
                for field, key in refresh._OR_FIELD_KEYS.items():
                    if field in m:
                        v = overrides.get(oid, {}).get(field, m[field])
                        pricing[key] = repr(v / 1_000_000)
    return {"data": [{"id": i, "pricing": p} for i, p in by_id.items()]}


def _check(monkeypatch, table, overrides=None):
    monkeypatch.setattr(refresh, "_fetch_json", lambda url: _feed(table, overrides))
    return refresh.check_drift(table)


def test_check_drift_matches_every_mapped_model(monkeypatch):
    rep = _check(monkeypatch, synthetic_table())
    st = {p["provider_id"]: p["status"] for p in rep["providers"]}
    assert st == {"alpha": "match", "beta": "match", "gamma": "manual"}


def test_check_drift_reports_the_model_that_drifted(monkeypatch):
    t = synthetic_table()
    rep = _check(monkeypatch, t, {"alpha/nano": {"input_price_per_1m": 0.06}})
    alpha = next(p for p in rep["providers"] if p["provider_id"] == "alpha")
    assert alpha["status"] == "drift"
    assert alpha["changes"] == [{"tier": "tier_fast", "model_id": "alpha-nano",
                                 "field": "input_price_per_1m", "old": 0.05, "new": 0.06}]
    assert "tier_fast[alpha-nano].input_price_per_1m" in refresh.format_drift_report(rep)
    cand = refresh.propose_updated_table(rep, t)
    fast = cand["providers"]["alpha"]["models"]["tier_fast"]
    assert [m["input_price_per_1m"] for m in fast] == [0.08, 0.06]     # only nano moved


def _sla(monkeypatch, table, overrides):
    rep = _check(monkeypatch, table, overrides)
    return refresh.build_sla_report(rep, table, date(2026, 11, 21))


def test_sla_proposal_applies_to_the_right_model_byte_for_byte(monkeypatch):
    t = synthetic_table()
    text = json.dumps(t, indent=2) + "\n"
    sla = _sla(monkeypatch, t, {"alpha/mini": {"output_price_per_1m": 0.40},
                                "alpha/nano": {"output_price_per_1m": 0.40}})
    sig = sla["significant"]
    assert [(c["model_id"], c["field"], c["feed"]) for c in sig] == [
        ("alpha-mini", "output_price_per_1m", 0.4), ("alpha-nano", "output_price_per_1m", 0.4)]
    proposal = refresh.dump_like(refresh.apply_significant(t, sig), text)
    expected = copy.deepcopy(t)
    for m in expected["providers"]["alpha"]["models"]["tier_fast"]:
        m["output_price_per_1m"] = 0.4
    assert proposal == json.dumps(expected, indent=2) + "\n"
    import difflib
    diff = [l for l in difflib.ndiff(text.splitlines(), proposal.splitlines())
            if l[:2] in ("- ", "+ ")]
    assert diff == ['-             "output_price_per_1m": 0.3,',
                    '+             "output_price_per_1m": 0.4,',
                    '-             "output_price_per_1m": 0.2,',
                    '+             "output_price_per_1m": 0.4,']
    md = refresh.render_drift_markdown(sla, t["pricing_urls"])
    assert "`alpha-mini`" in md and "`alpha-nano`" in md


def test_fingerprint_tells_models_of_one_tier_apart(monkeypatch):
    t = synthetic_table()
    # Both fast models at 0.05 input, drifting to the same 0.07.
    t["providers"]["alpha"]["models"]["tier_fast"][0]["input_price_per_1m"] = 0.05
    a = _sla(monkeypatch, t, {"alpha/mini": {"input_price_per_1m": 0.07}})
    b = _sla(monkeypatch, t, {"alpha/nano": {"input_price_per_1m": 0.07}})
    assert a["significant"][0]["model_id"] == "alpha-mini"
    assert b["significant"][0]["model_id"] == "alpha-nano"
    assert a["fingerprint"] != b["fingerprint"]
    # without the table the legacy line format is used (and they would collide)
    assert refresh.drift_fingerprint(a["significant"]) == refresh.drift_fingerprint(b["significant"])


def test_fingerprint_of_a_one_model_tier_is_unchanged(monkeypatch):
    t = synthetic_table()
    sla = _sla(monkeypatch, t, {"beta/fast": {"input_price_per_1m": 0.2}})
    c = sla["significant"][0]
    legacy = refresh.hashlib.sha256(
        f"beta|tier_fast|input_price_per_1m|{0.1!r}|{0.2!r}".encode()).hexdigest()
    assert sla["fingerprint"] == legacy
    # the same as a list of one: identical fingerprint and proposal
    sla1 = _sla(monkeypatch, _as_list_of_one(t), {"beta/fast": {"input_price_per_1m": 0.2}})
    assert sla1["fingerprint"] == legacy
    assert c["model_id"] == "beta-fast"


def test_pre_sc3a_change_without_model_id(monkeypatch):
    t = synthetic_table()
    one = {"checked_at": "x", "providers": [{"provider_id": "beta", "strategy": "openrouter",
           "status": "drift", "changes": [{"tier": "tier_fast", "field": "input_price_per_1m",
                                           "old": 0.1, "new": 0.2}]}]}
    cls = refresh.classify_drift(one, t)
    assert cls["significant"][0]["model_id"] == "beta-fast"
    assert refresh.propose_updated_table(one, t)["providers"]["beta"]["models"]["tier_fast"][
        "input_price_per_1m"] == 0.2
    many = copy.deepcopy(one)
    many["providers"][0].update(provider_id="alpha")
    with pytest.raises(ValueError, match="must name a model_id"):
        refresh.classify_drift(many, t)
    with pytest.raises(KeyError, match="must name a model_id"):
        refresh.propose_updated_table(many, t)
    with pytest.raises(KeyError, match="lists no model 'alpha-gone'"):
        refresh.apply_significant(t, [{"provider": "alpha", "tier": "tier_fast",
                                       "model_id": "alpha-gone", "field": "input_price_per_1m",
                                       "feed": 0.1}])


def test_unvalidated_string_id_on_a_multi_model_tier_raises(monkeypatch):
    t = _broken(lambda p: p["alpha"]["refresh"]["ids"].__setitem__("tier_fast", "alpha/mini"))
    monkeypatch.setattr(refresh, "_fetch_json", lambda url: {"data": []})
    with pytest.raises(pt.PriceTableError, match="one id for 2 models"):
        refresh.check_drift(t)


def test_json_api_strategy_skips_multi_model_tiers(monkeypatch):
    t = synthetic_table()
    t["providers"]["alpha"]["refresh"] = {"strategy": "json_api", "url": "https://x.test/p",
                                          "map": {"tier_powerful": {"input_price_per_1m": ["a"]},
                                                  "tier_fast": {"input_price_per_1m": ["a"]}}}
    t["providers"]["beta"]["refresh"] = {"strategy": "manual"}
    monkeypatch.setattr(refresh, "_fetch_json", lambda url: {"a": 4.0})
    alpha = refresh.check_drift(t)["providers"][0]
    assert alpha["changes"] == [{"tier": "tier_powerful", "model_id": "alpha-max",
                                 "field": "input_price_per_1m", "old": 3.0, "new": 4.0}]


# ─── price history ────────────────────────────────────────────────────────────

class _Mem:
    def __init__(self):
        self.rows = []

    def append(self, e):
        self.rows.append(e)


def test_record_diff_matches_models_by_id_within_a_tier():
    import price_history as ph
    old = synthetic_table()
    new = copy.deepcopy(old)
    fast = new["providers"]["alpha"]["models"]["tier_fast"]
    fast.reverse()                                         # reordering is not a change
    fast[0]["input_price_per_1m"] = 0.04                   # alpha-nano moved
    fast.append(_m("alpha-pico", 0.01, 0.02))              # a new model
    be = _Mem()
    out = ph.record_diff(old, new, provenance_tier="verified", backend=be)
    got = {(e["model_id"], e["field"], e["old"], e["new"]) for e in out}
    assert got == {("alpha-nano", "input_price_per_1m", 0.05, 0.04),
                   ("alpha-pico", "input_price_per_1m", None, 0.01),
                   ("alpha-pico", "output_price_per_1m", None, 0.02)}


def test_record_diff_single_model_migration_still_compares_to_the_old_model():
    import price_history as ph
    old = synthetic_table()
    new = copy.deepcopy(old)
    new["providers"]["beta"]["models"]["tier_fast"] = _m("beta-fast-2", 0.12, 0.40)
    out = ph.record_diff(old, new, provenance_tier="verified", backend=_Mem())
    assert [(e["model_id"], e["old"], e["new"]) for e in out] == [("beta-fast-2", 0.1, 0.12)]


# ─── listing generator ────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("gen_listing_sc3a",
                                                  ROOT / "scripts" / "gen_listing.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_listing_gives_each_model_of_a_tier_a_row(gen):
    rows = gen.rows(synthetic_table())
    alpha = [r for r in rows if r.startswith("| Alpha |")]
    assert alpha == [
        "| Alpha | fast | alpha-mini | 0.08 | 0.30 | — | re-read |",
        "| Alpha | fast | alpha-nano | 0.05 | 0.20 | 0.01 | re-read |",
        "| Alpha | balanced | alpha-mid | 1.00 | 4.00 | — | re-read |",
        "| Alpha | balanced | alpha-zz | 0.50 | 2.00 | — | re-read |",
        "| Alpha | powerful | alpha-max | 3.00 | 12.00 | — | re-read |",
    ]
    w = gen.tier_winners(synthetic_table())
    assert w == {"tier_fast": "Alpha alpha-nano", "tier_balanced": "Alpha alpha-zz",
                 "tier_powerful": "Beta beta-max"}
    block = gen.render(synthetic_table())
    assert "**Alpha alpha-nano** for fast" in block


def test_listing_merges_a_model_listed_in_two_tiers(gen):
    t = synthetic_table()
    t["providers"]["beta"]["models"]["tier_powerful"] = [_m("beta-max", 2.0, 8.0),
                                                         _m("beta-mid-a", 0.5, 2.0)]
    beta = [r for r in gen.rows(t) if r.startswith("| Beta |")]
    assert "| Beta | balanced/powerful | beta-mid-a | 0.50 | 2.00 | — | re-read |" in beta
    assert len(beta) == 4


def test_listing_identical_for_single_object_and_list_of_one(gen):
    t = _as_single(synthetic_table())
    assert gen.render(t) == gen.render(_as_list_of_one(t))


def test_tier_models_adapts_both_forms_and_rejects_others():
    assert pt.tier_models({"model_id": "a"}) == [{"model_id": "a"}]
    assert pt.tier_models([{"model_id": "a"}]) == [{"model_id": "a"}]
    with pytest.raises(pt.PriceTableError, match="got str"):
        pt.tier_models("a")
    assert pt.find_model([{"model_id": "a"}, {"model_id": "b"}], "b") == {"model_id": "b"}
    assert pt.find_model({"model_id": "a"}, "b") is None


def test_record_diff_new_tier_records_every_model_as_new():
    import price_history as ph
    old = synthetic_table()
    new = copy.deepcopy(old)
    del old["providers"]["alpha"]["models"]["tier_fast"]
    out = ph.record_diff(old, new, provenance_tier="verified", backend=_Mem())
    assert {(e["model_id"], e["old"]) for e in out} == {("alpha-mini", None), ("alpha-nano", None)}
    assert len(out) == 5      # mini in/out, nano in/out/cache
