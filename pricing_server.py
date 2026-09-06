"""
pricing_server — CheapRouter Pricing (SP1c).

The second MCP server in this repo: its product is **verified LLM token-price
truth with provenance**. Where server.py (the router) *consumes* prices to route,
this server *serves* the price truth as a product — the "pricing head" on the
shared waist (PRICING_SERVER_BLUEPRINT.md).

Tied at the waist: imports the same spine as the router — prices.json via
pricing_table (the staleness guard + provenance), refresh (the OpenRouter drift
feed), provenance (the three tiers), price_history (the trajectory). Adds NO
routing. Provenance-first: every response leads with tier + verified_at + source.

SP1c ships the READ tools (get / list / drift / history). The operator write path
(pricing_verify) is SP1e; the router->pricing observed signal is SP1d.

Run (local):
  python pricing_server.py --stdio     (stdio — local MCP clients)
  python pricing_server.py             (streamable-http, port from PRICING_PORT/PORT)
"""

import json
import sys
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import os
# Distinct default port from the router (8081) so both can run from one repo.
_PORT = int(os.environ.get("PRICING_PORT", os.environ.get("PORT", 8082)))

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict

from pricing_table import raw_table, price_table_status, table_provenance, is_stale, age_days
from providers import PROVIDERS, VALID_TIERS
from provenance import Tier

mcp = FastMCP("cheaprouter_pricing", host="0.0.0.0", port=_PORT)


# ─── Input models ─────────────────────────────────────────────────────────────

class PricingGetInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    provider: str = Field(..., description="Provider id, e.g. 'anthropic', 'deepseek'.")
    tier: str = Field(..., description="Capability tier: tier_fast / tier_balanced / tier_powerful.")


class PricingListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tier: Optional[str] = Field(None, description="Filter to one tier; omit for all.")


class PricingDriftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Optional[str] = Field(None, description="Filter drift to one provider; omit for all.")


class PricingHistoryInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    provider: Optional[str] = Field(None, description="Filter to one provider.")
    model_id: Optional[str] = Field(None, description="Filter to one model id.")
    field: str = Field("input_price_per_1m", description="Which price field's trajectory: input_price_per_1m / output_price_per_1m / cached_input_price_per_1m.")
    limit: int = Field(200, description="Max records.", ge=1, le=2000)
    trajectory_only: bool = Field(False, description="If True and provider+model_id given, return the oldest->newest price series only.")


# ─── Provenance-first helpers ─────────────────────────────────────────────────

def _table_prov_block() -> dict:
    """The provenance block that leads every response: tier + verified_at + status."""
    prov = table_provenance().to_dict()
    st = price_table_status()
    return {
        "tier": prov["tier"],
        "citable": prov["citable"],
        "verified_at": prov.get("verified_at"),
        "verified_by": prov.get("verified_by"),
        "age_days": st["age_days"],
        "stale": st["stale"],
        "max_age_days": st["max_age_days"],
    }


# ─── Tools ────────────────────────────────────────────────────────────────────

@mcp.tool(
    name="pricing_get",
    annotations={"title": "Get Verified Price", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def pricing_get(params: PricingGetInput) -> str:
    """
    Verified price for one provider/model tier, provenance-first.

    Leads with the provenance (tier, verified_at, source, staleness), then the
    prices. Only the `verified` tier is citable as authoritative.

    Args:
        params (PricingGetInput): provider id + capability tier.

    Returns:
        str: JSON with provenance block + the model's input/output/cache prices.
    """
    if params.tier not in VALID_TIERS:
        return json.dumps({"error": f"unknown tier '{params.tier}'", "valid": sorted(VALID_TIERS)})
    prov = PROVIDERS.get(params.provider)
    if prov is None:
        return json.dumps({"error": f"unknown provider '{params.provider}'",
                           "known": sorted(PROVIDERS)})
    m = prov.models.get(params.tier)
    if m is None:
        return json.dumps({"error": f"provider '{params.provider}' has no '{params.tier}' tier"})

    urls = raw_table().get("pricing_urls", {})
    price = {
        "input_price_per_1m": m.input_price_per_1m,
        "output_price_per_1m": m.output_price_per_1m,
    }
    if m.cached_input_price_per_1m is not None:
        price["cached_input_price_per_1m"] = m.cached_input_price_per_1m

    return json.dumps({
        "provenance": {**_table_prov_block(), "source": urls.get(params.provider)},
        "provider": params.provider,
        "provider_name": prov.name,
        "tier": params.tier,
        "model_id": m.model_id,
        "region": prov.region,
        "context_window": m.context_window,
        "price_per_1m_usd": price,
    }, indent=2)


@mcp.tool(
    name="pricing_list",
    annotations={"title": "List Price Table", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def pricing_list(params: PricingListInput) -> str:
    """
    The whole current verified price table, provenance-first.

    Args:
        params (PricingListInput): optional tier filter.

    Returns:
        str: JSON with the provenance block + every provider/tier price.
    """
    if params.tier and params.tier not in VALID_TIERS:
        return json.dumps({"error": f"unknown tier '{params.tier}'", "valid": sorted(VALID_TIERS)})
    urls = raw_table().get("pricing_urls", {})
    rows = []
    for pid, prov in PROVIDERS.items():
        for tier, m in prov.models.items():
            if params.tier and tier != params.tier:
                continue
            entry = {
                "provider": pid, "provider_name": prov.name, "region": prov.region,
                "tier": tier, "model_id": m.model_id,
                "input_price_per_1m": m.input_price_per_1m,
                "output_price_per_1m": m.output_price_per_1m,
                "source": urls.get(pid),
            }
            if m.cached_input_price_per_1m is not None:
                entry["cached_input_price_per_1m"] = m.cached_input_price_per_1m
            rows.append(entry)
    return json.dumps({"provenance": _table_prov_block(), "count": len(rows), "prices": rows}, indent=2)


@mcp.tool(
    name="pricing_drift",
    annotations={"title": "Price Drift vs Feed", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
async def pricing_drift(params: PricingDriftInput) -> str:
    """
    Where the OpenRouter feed disagrees with the verified table, right now (SC2,
    exposed as a product feature). A drift is a *signal to re-verify*, never an
    auto-applied price — the verified tier only advances by human action.

    Args:
        params (PricingDriftInput): optional provider filter.

    Returns:
        str: JSON drift report (match / drift / fetch_failed / manual per provider).
    """
    from refresh import check_drift
    report = check_drift()
    if params.provider:
        report = {**report, "providers": [p for p in report["providers"]
                                          if p["provider_id"] == params.provider]}
    return json.dumps({
        "provenance": _table_prov_block(),
        "note": "Drift is a re-verify signal from the proxy feed; it never auto-updates the verified table.",
        "drift": report,
    }, indent=2)


@mcp.tool(
    name="pricing_history",
    annotations={"title": "Price History / Trajectory", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
async def pricing_history(params: PricingHistoryInput) -> str:
    """
    How prices have changed over time — the citable, provenanced series that turns
    a snapshot into a KPI. Each history entry carries the provenance tier of the
    change (verified / proxy / observed).

    Args:
        params (PricingHistoryInput): filters + field; trajectory_only for a clean
            oldest->newest series when provider+model_id are given.

    Returns:
        str: JSON with the change records (or the trajectory series) + a summary.
    """
    import price_history as ph
    if params.trajectory_only and params.provider and params.model_id:
        series = ph.trajectory(params.provider, params.model_id, params.field, params.limit)
        return json.dumps({
            "provider": params.provider, "model_id": params.model_id,
            "field": params.field, "points": series, "count": len(series),
        }, indent=2)
    records = ph.history(params.provider, params.model_id, params.limit)
    return json.dumps({
        "summary": ph.summary(params.limit),
        "count": len(records),
        "changes": records,
    }, indent=2)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--stdio" in sys.argv:
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")
