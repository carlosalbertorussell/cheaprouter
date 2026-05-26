"""
cheaprouter — MCP server for cheap token routing.

BYOK (Bring Your Own Keys): callers supply their provider API keys per request.
The server holds no credentials — routing intelligence is the value, not API access.

Tools:
  arbitrage_get_pricing       — Full price comparison table for a tier + volume
  arbitrage_estimate_cost     — Pre-flight cost estimate before committing a request
  arbitrage_route_completion  — Route and execute a completion on the cheapest provider
  arbitrage_provider_status   — Availability and config status per provider
  arbitrage_get_history       — Past routing decisions with spend and savings summary

Run (local):
  python server.py --stdio     (stdio — for Claude Desktop / local MCP clients)
  python server.py             (streamable HTTP on port 8000 — default, matches MCPize cloud)

Deploy (MCPize):
  mcpize deploy                (no secrets needed — users supply keys per request)
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
_PORT = int(os.environ.get("PORT", 8081))

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict, field_validator

from providers import PROVIDERS, VALID_TIERS, resolve_api_key
from pricing import estimate_all_providers, format_pricing_table
from router import route
from client import call_provider
from history import log_decision, read_history, history_summary

mcp = FastMCP(
    "cheaprouter",
    host="0.0.0.0",
    port=_PORT,
)

# ─── Shared field definitions ─────────────────────────────────────────────────

TIER_DESC = "Capability tier: 'tier_fast' (cheap/fast), 'tier_balanced' (mid-range), 'tier_powerful' (flagship)"

API_KEYS_DESC = (
    "Your provider API keys as a dict. Supply only the providers you want eligible. "
    'Example: {"anthropic": "sk-ant-...", "openai": "sk-...", "groq": "gsk_...", '
    '"deepseek": "sk-...", "gemini": "AIza...", "mistral": "...", "qwen": "sk-..."}. '
    "Keys are used only for this request and never stored or logged."
)

PROVIDER_IDS = ["anthropic", "openai", "gemini", "groq", "mistral", "deepseek", "qwen", "grok"]


# ─── Input models ─────────────────────────────────────────────────────────────

class GetPricingInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    tier: str = Field(..., description=TIER_DESC)
    input_tokens: int = Field(1000, description="Estimated input token count", ge=1, le=2_000_000)
    output_tokens: int = Field(500, description="Estimated output token count", ge=1, le=500_000)
    api_keys: dict[str, str] = Field(default_factory=dict, description=API_KEYS_DESC)

    @field_validator("tier")
    @classmethod
    def validate_tier(cls, v: str) -> str:
        if v not in VALID_TIERS:
            raise ValueError(f"tier must be one of: {sorted(VALID_TIERS)}")
        return v


class EstimateCostInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    tier: str = Field(..., description=TIER_DESC)
    input_tokens: int = Field(..., description="Estimated input token count", ge=1, le=2_000_000)
    output_tokens: int = Field(..., description="Estimated output token count", ge=1, le=500_000)
    api_keys: dict[str, str] = Field(default_factory=dict, description=API_KEYS_DESC)
    latency_sensitive: bool = Field(False, description="Exclude high-latency providers (>300ms, mainly CN region)")
    excluded_providers: list[str] = Field(default_factory=list, description="Provider IDs to exclude")
    allowed_regions: Optional[list[str]] = Field(None, description="Restrict to these regions only, e.g. ['us', 'eu']")

    @field_validator("tier")
    @classmethod
    def validate_tier(cls, v: str) -> str:
        if v not in VALID_TIERS:
            raise ValueError(f"tier must be one of: {sorted(VALID_TIERS)}")
        return v


class RouteCompletionInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    messages: list[dict] = Field(..., description='Conversation messages: [{"role": "user", "content": "..."}]', min_length=1)
    tier: str = Field("tier_fast", description=TIER_DESC)
    api_keys: dict[str, str] = Field(..., description=API_KEYS_DESC)
    system_prompt: Optional[str] = Field(None, description="Optional system prompt")
    max_tokens: int = Field(2048, description="Max output tokens", ge=1, le=32_000)
    latency_sensitive: bool = Field(False, description="Exclude high-latency providers (>300ms)")
    excluded_providers: list[str] = Field(default_factory=list, description="Provider IDs to skip")
    allowed_regions: Optional[list[str]] = Field(None, description="Restrict routing to these regions")
    estimated_output_tokens: int = Field(500, description="Estimated output tokens for pre-flight cost routing", ge=1, le=500_000)

    @field_validator("tier")
    @classmethod
    def validate_tier(cls, v: str) -> str:
        if v not in VALID_TIERS:
            raise ValueError(f"tier must be one of: {sorted(VALID_TIERS)}")
        return v


class ProviderStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: Optional[str] = Field(None, description="Specific provider to check. If omitted, returns all providers.")
    api_keys: dict[str, str] = Field(default_factory=dict, description=API_KEYS_DESC)


class GetHistoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(20, description="Number of recent records to return", ge=1, le=100)
    summary_only: bool = Field(False, description="If True, return aggregated stats only")


# ─── Tools ────────────────────────────────────────────────────────────────────

@mcp.tool(
    name="arbitrage_get_pricing",
    annotations={
        "title": "Get Provider Pricing Comparison",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def arbitrage_get_pricing(params: GetPricingInput) -> str:
    """
    Return a full price comparison table across all seven providers for a given
    capability tier and token volume.

    Pass api_keys to see which providers you have access to. Providers without
    a key are shown with a warning but pricing is still displayed for comparison.

    Args:
        params (GetPricingInput):
            - tier (str): Capability tier
            - input_tokens (int): Estimated input tokens (default 1000)
            - output_tokens (int): Estimated output tokens (default 500)
            - api_keys (dict): Your provider API keys (optional — for availability info)

    Returns:
        str: Markdown table sorted cheapest first, plus a cost spread summary.
    """
    estimates = estimate_all_providers(PROVIDERS, params.tier, params.input_tokens, params.output_tokens, params.api_keys)
    table = format_pricing_table(estimates)

    cheapest = estimates[0]
    priciest = estimates[-1]
    configured = [e for e in estimates if e.is_configured]

    lines = [
        f"## Token pricing — `{params.tier}` tier",
        f"**Volume:** {params.input_tokens:,} input · {params.output_tokens:,} output tokens\n",
        table,
        "",
    ]
    if configured:
        cheapest_cfg = configured[0]
        lines.append(f"**Cheapest (your keys):** {cheapest_cfg.provider_name} (`{cheapest_cfg.model_id}`) — ${cheapest_cfg.total_cost_usd:.6f}")
    else:
        lines.append("**No api_keys supplied** — pass your keys to see availability and get routing.")
    lines.append(f"**Overall cheapest:** {cheapest.provider_name} — ${cheapest.total_cost_usd:.6f}")
    lines.append(f"**Most expensive:** {priciest.provider_name} — ${priciest.total_cost_usd:.6f}")
    if priciest.total_cost_usd > 0:
        ratio = priciest.total_cost_usd / cheapest.total_cost_usd
        lines.append(f"**Price spread:** {ratio:.1f}× between cheapest and most expensive")

    return "\n".join(lines)


@mcp.tool(
    name="arbitrage_estimate_cost",
    annotations={
        "title": "Pre-flight Cost Estimate",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def arbitrage_estimate_cost(params: EstimateCostInput) -> str:
    """
    Pre-flight cost estimate: given your API keys, token counts, and constraints,
    return the recommended provider and full cost breakdown without executing the request.

    Args:
        params (EstimateCostInput):
            - tier (str): Capability tier
            - input_tokens (int): Estimated input tokens
            - output_tokens (int): Estimated output tokens
            - api_keys (dict): Your provider API keys
            - latency_sensitive (bool): Exclude high-latency providers
            - excluded_providers (list[str]): Provider IDs to skip
            - allowed_regions (list[str]): Restrict to these regions

    Returns:
        str: JSON routing decision — winner, ranked alternatives, exclusions, savings.
    """
    decision = route(
        providers=PROVIDERS,
        tier=params.tier,
        input_tokens=params.input_tokens,
        output_tokens=params.output_tokens,
        user_keys=params.api_keys,
        latency_sensitive=params.latency_sensitive,
        excluded_providers=params.excluded_providers,
        allowed_regions=params.allowed_regions,
    )

    if not decision.winner:
        return json.dumps({
            "error": "No eligible providers found. Check that you supplied api_keys for at least one provider.",
            "excluded": decision.excluded,
            "known_providers": PROVIDER_IDS,
        }, indent=2)

    return json.dumps(decision.to_dict(), indent=2)


@mcp.tool(
    name="arbitrage_route_completion",
    annotations={
        "title": "Route and Execute Completion",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def arbitrage_route_completion(params: RouteCompletionInput) -> str:
    """
    Route a completion request to the cheapest available provider (from your keys)
    and execute it. Returns the response with a full routing audit record.

    API keys are used only for this request and are never stored or logged.

    Args:
        params (RouteCompletionInput):
            - messages (list[dict]): Conversation messages
            - tier (str): Capability tier (default: tier_fast)
            - api_keys (dict): Your provider API keys — required
            - system_prompt (str, optional): System prompt
            - max_tokens (int): Max output tokens (default 2048)
            - latency_sensitive (bool): Exclude high-latency providers
            - excluded_providers (list[str]): Provider IDs to skip
            - allowed_regions (list[str]): Restrict to these regions
            - estimated_output_tokens (int): Used for pre-flight routing

    Returns:
        str: JSON with completion text, routing metadata, actual token usage,
             actual cost, and savings vs. most expensive alternative.
    """
    total_chars = sum(len(m.get("content", "")) for m in params.messages)
    if params.system_prompt:
        total_chars += len(params.system_prompt)
    estimated_input = max(1, total_chars // 4)

    decision = route(
        providers=PROVIDERS,
        tier=params.tier,
        input_tokens=estimated_input,
        output_tokens=params.estimated_output_tokens,
        user_keys=params.api_keys,
        latency_sensitive=params.latency_sensitive,
        excluded_providers=params.excluded_providers,
        allowed_regions=params.allowed_regions,
    )

    if not decision.winner:
        return json.dumps({
            "error": "No eligible providers found. Check that api_keys includes at least one valid provider key.",
            "excluded": decision.excluded,
            "known_providers": PROVIDER_IDS,
        }, indent=2)

    winner = decision.winner
    provider = PROVIDERS[winner.provider_id]
    model = provider.models[params.tier]
    api_key = resolve_api_key(winner.provider_id, params.api_keys)

    try:
        completion = await call_provider(
            provider=provider,
            model=model,
            api_key=api_key,
            messages=params.messages,
            system_prompt=params.system_prompt,
            max_tokens=params.max_tokens,
        )
    except Exception as exc:
        error_msg = str(exc)
        log_decision(decision.to_dict(), error=error_msg)
        return json.dumps({
            "error": f"Provider call failed: {error_msg}",
            "provider_attempted": winner.provider_name,
            "routing": decision.to_dict(),
        }, indent=2)

    actual_input_cost = (completion.input_tokens_used / 1_000_000) * model.input_price_per_1m
    actual_output_cost = (completion.output_tokens_used / 1_000_000) * model.output_price_per_1m
    actual_total_cost = actual_input_cost + actual_output_cost

    log_decision(
        decision_dict=decision.to_dict(),
        completion_text=completion.text,
        actual_input_tokens=completion.input_tokens_used,
        actual_output_tokens=completion.output_tokens_used,
        actual_latency_ms=completion.latency_ms,
    )

    return json.dumps({
        "response": completion.text,
        "routing": {
            "provider": winner.provider_name,
            "provider_id": winner.provider_id,
            "model": completion.model_id,
            "tier": params.tier,
            "region": winner.region,
        },
        "cost": {
            "actual_input_tokens": completion.input_tokens_used,
            "actual_output_tokens": completion.output_tokens_used,
            "actual_cost_usd": round(actual_total_cost, 8),
            "savings": decision.savings,
        },
        "latency_ms": completion.latency_ms,
        "excluded_providers": decision.excluded,
    }, indent=2)


@mcp.tool(
    name="arbitrage_provider_status",
    annotations={
        "title": "Provider Availability Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def arbitrage_provider_status(params: ProviderStatusInput) -> str:
    """
    Return configuration and availability status for one or all providers.

    Pass your api_keys to see which providers you have access to. Shows
    region, latency, and pricing per tier for each provider.

    Args:
        params (ProviderStatusInput):
            - provider_id (str, optional): Specific provider ID. If omitted, returns all.
            - api_keys (dict): Your provider API keys (to show availability)

    Returns:
        str: JSON with per-provider status, model IDs, pricing per tier.
    """
    targets = {}
    if params.provider_id:
        if params.provider_id not in PROVIDERS:
            return json.dumps({"error": f"Unknown provider '{params.provider_id}'. Known: {PROVIDER_IDS}"})
        targets = {params.provider_id: PROVIDERS[params.provider_id]}
    else:
        targets = PROVIDERS

    statuses = {}
    for pid, prov in targets.items():
        key_available = bool(resolve_api_key(pid, params.api_keys))
        statuses[pid] = {
            "name": prov.name,
            "key_provided": key_available,
            "region": prov.region,
            "latency_ms_from_bsas": prov.latency_ms,
            "protocol": prov.protocol,
            "models": {
                tier: {
                    "model_id": m.model_id,
                    "input_price_per_1m_usd": m.input_price_per_1m,
                    "output_price_per_1m_usd": m.output_price_per_1m,
                    "context_window": m.context_window,
                }
                for tier, m in prov.models.items()
            },
        }

    keyed_count = sum(1 for s in statuses.values() if s["key_provided"])
    return json.dumps({
        "providers": statuses,
        "summary": {
            "total": len(statuses),
            "keys_provided": keyed_count,
            "keys_missing": len(statuses) - keyed_count,
        },
    }, indent=2)


@mcp.tool(
    name="arbitrage_get_history",
    annotations={
        "title": "Routing History and Spend Summary",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def arbitrage_get_history(params: GetHistoryInput) -> str:
    """
    Return past routing decisions with cost tracking and savings analysis.

    Note: history is server-side and aggregated across all users. It records
    which providers were selected, token usage, cost, and latency — never API keys.

    Args:
        params (GetHistoryInput):
            - limit (int): Number of recent records (default 20, max 100)
            - summary_only (bool): If True, return aggregate stats only

    Returns:
        str: JSON with routing history or aggregate summary.
    """
    if params.summary_only:
        return json.dumps(history_summary(params.limit), indent=2)

    records = read_history(params.limit)
    if not records:
        return json.dumps({"message": "No routing history found.", "records": []})

    return json.dumps({"summary": history_summary(params.limit), "records": records}, indent=2)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--stdio" in sys.argv:
        mcp.run()
    else:
        mcp.run(transport="http", host="0.0.0.0", port=_PORT)
