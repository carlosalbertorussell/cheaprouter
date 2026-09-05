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
from health import provider_health
from client import call_provider, is_transient_error
from tokens import count_message_tokens
from history import (
    log_decision, read_history, history_summary,
    spend_report, set_budget, budget_status,
)

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
    session: Optional[str] = Field(None, description="Opaque session token to attribute spend to you. Reused across requests to track your own cumulative spend; never mapped to your identity.")
    health_aware: bool = Field(True, description="When True (default), providers that have been failing recently are deprioritized — moved behind healthy providers of similar price, but never excluded. Set False for pure cheapest-first routing.")
    max_failover: int = Field(2, description="On a transient error (429/5xx/timeout), how many additional providers to try, in ranked order, before giving up. 0 disables failover. Non-transient errors (bad key, bad request) never fail over.", ge=0, le=7)

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
    session: Optional[str] = Field(None, description="Scope history to your session token. Omit to see the global (unscoped) history.")


class SpendReportInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session: Optional[str] = Field(None, description="Scope the report to your session token. Omit for global spend.")
    limit: int = Field(500, description="Max records to aggregate", ge=1, le=2000)
    since: Optional[str] = Field(None, description="ISO date/datetime (e.g. '2026-09-01'); exclude older records.")


class SetBudgetInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session: str = Field(..., description="Your session token — the budget is scoped to it.")
    monthly_usd: float = Field(..., description="Monthly spend budget in USD.", gt=0)


class ProviderHealthInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window: Optional[int] = Field(None, description="How many recent routing records to score over (default 50).", ge=1, le=2000)


class CountTokensInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    messages: list[dict] = Field(..., description='Conversation messages: [{"role": "user", "content": "..."}]', min_length=1)
    system_prompt: Optional[str] = Field(None, description="Optional system prompt to include in the count.")


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
    estimated_input = count_message_tokens(params.messages, params.system_prompt)

    decision = route(
        providers=PROVIDERS,
        tier=params.tier,
        input_tokens=estimated_input,
        output_tokens=params.estimated_output_tokens,
        user_keys=params.api_keys,
        latency_sensitive=params.latency_sensitive,
        excluded_providers=params.excluded_providers,
        allowed_regions=params.allowed_regions,
        health_aware=params.health_aware,
    )

    if not decision.winner:
        return json.dumps({
            "error": "No eligible providers found. Check that api_keys includes at least one valid provider key.",
            "excluded": decision.excluded,
            "known_providers": PROVIDER_IDS,
        }, indent=2)

    # Failover loop (S2): walk the ranked pool in order. On a transient error
    # (429/5xx/timeout) move to the next provider; on a non-transient error (bad
    # key/request) stop immediately since it would fail identically everywhere.
    # Every failed attempt is logged as a failure, which feeds S8 health scoring.
    ranked = decision.ranked_pool or [decision.winner.provider_id]
    max_attempts = 1 + max(0, params.max_failover)
    sequence = ranked[:max_attempts]

    completion = None
    used = None                 # CostEstimate of the provider that succeeded
    attempts: list[dict] = []   # per-provider attempt log for the response

    for pid in sequence:
        est = next((e for e in decision.all_estimates if e.provider_id == pid), None)
        if est is None:
            continue
        provider = PROVIDERS[pid]
        model = provider.models[params.tier]
        api_key = resolve_api_key(pid, params.api_keys)

        try:
            completion = await call_provider(
                provider=provider,
                model=model,
                api_key=api_key,
                messages=params.messages,
                system_prompt=params.system_prompt,
                max_tokens=params.max_tokens,
            )
            used = est
            attempts.append({"provider_id": pid, "outcome": "success"})
            # Log the successful decision, attributed to the provider that worked.
            success_cost = (
                (completion.input_tokens_used / 1_000_000) * model.input_price_per_1m
                + (completion.output_tokens_used / 1_000_000) * model.output_price_per_1m
            )
            log_decision(
                decision_dict={**decision.to_dict(),
                               "winner": {**decision.to_dict()["winner"],
                                          "provider_id": pid,
                                          "provider_name": provider.name,
                                          "region": provider.region}},
                session=params.session,
                actual_input_tokens=completion.input_tokens_used,
                actual_output_tokens=completion.output_tokens_used,
                actual_latency_ms=completion.latency_ms,
                actual_cost_usd=success_cost,
            )
            break
        except Exception as exc:
            transient = is_transient_error(exc)
            attempts.append({
                "provider_id": pid,
                "outcome": "transient_error" if transient else "error",
                "error_class": str(exc).split(":")[0][:60],
            })
            # Log this provider's failure (feeds S8 health), attributed to it.
            log_decision(
                decision_dict={**decision.to_dict(),
                               "winner": {"provider_id": pid,
                                          "provider_name": provider.name,
                                          "region": provider.region,
                                          "total_cost_usd": est.total_cost_usd}},
                session=params.session,
                error=str(exc),
            )
            if not transient:
                break   # non-transient → no point trying other providers

    if completion is None:
        return json.dumps({
            "error": "All attempted providers failed.",
            "attempts": attempts,
            "routing": decision.to_dict(),
        }, indent=2)

    model = PROVIDERS[used.provider_id].models[params.tier]
    actual_input_cost = (completion.input_tokens_used / 1_000_000) * model.input_price_per_1m
    actual_output_cost = (completion.output_tokens_used / 1_000_000) * model.output_price_per_1m
    actual_total_cost = actual_input_cost + actual_output_cost

    result = {
        "response": completion.text,
        "routing": {
            "provider": used.provider_name,
            "provider_id": used.provider_id,
            "model": completion.model_id,
            "tier": params.tier,
            "region": used.region,
            "failed_over": len(attempts) > 1,
            "attempts": attempts,
        },
        "cost": {
            "actual_input_tokens": completion.input_tokens_used,
            "actual_output_tokens": completion.output_tokens_used,
            "actual_cost_usd": round(actual_total_cost, 8),
            "savings": decision.savings,
        },
        "latency_ms": completion.latency_ms,
        "excluded_providers": decision.excluded,
        "deprioritized_providers": decision.deprioritized or [],
    }

    # Budget alert (S1.5): if the caller set a budget for this session, surface usage.
    if params.session:
        status = budget_status(params.session)
        if status:
            result["budget"] = status
            if status["over_budget"]:
                result["budget"]["alert"] = "You are over your monthly budget."
            elif status["used_pct"] >= 80:
                result["budget"]["alert"] = f"You've used {status['used_pct']}% of your monthly budget."

    return json.dumps(result, indent=2)


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

    Records carry routing metadata only — provider, tier, region, token counts,
    cost, latency, savings — never API keys and never message content. Pass a
    `session` token to see only your own records; omit it for the global history.

    Args:
        params (GetHistoryInput):
            - limit (int): Number of recent records (default 20, max 100)
            - summary_only (bool): If True, return aggregate stats only
            - session (str, optional): Scope to your session token

    Returns:
        str: JSON with routing history or aggregate summary.
    """
    if params.summary_only:
        return json.dumps(history_summary(params.limit, params.session), indent=2)

    records = read_history(params.limit, params.session)
    if not records:
        return json.dumps({"message": "No routing history found.", "records": []})

    return json.dumps(
        {"summary": history_summary(params.limit, params.session), "records": records},
        indent=2,
    )


@mcp.tool(
    name="arbitrage_spend_report",
    annotations={
        "title": "Spend Report",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def arbitrage_spend_report(params: SpendReportInput) -> str:
    """
    Break down spend by provider, by tier, and by day, with total saved vs. the
    most expensive alternative at each routing decision.

    Pass a `session` token to scope the report to your own spend. Requires the
    persistent store (Upstash) to be useful across redeploys; with local JSONL
    it reflects only the current container's history.

    Args:
        params (SpendReportInput):
            - session (str, optional): Scope to your session token
            - limit (int): Max records to aggregate (default 500)
            - since (str, optional): ISO date; exclude older records

    Returns:
        str: JSON with spend_by_provider, spend_by_tier, spend_by_day, and totals.
    """
    return json.dumps(spend_report(params.session, params.limit, params.since), indent=2)


@mcp.tool(
    name="arbitrage_set_budget",
    annotations={
        "title": "Set Monthly Budget",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def arbitrage_set_budget(params: SetBudgetInput) -> str:
    """
    Set a monthly USD spend budget for your session. Once set, every
    arbitrage_route_completion call made with the same session token returns a
    budget status, with an alert when you cross 80% or go over.

    Args:
        params (SetBudgetInput):
            - session (str): Your session token (required — the budget is scoped to it)
            - monthly_usd (float): Monthly budget in USD

    Returns:
        str: JSON confirming the stored budget, plus current status.
    """
    stored = set_budget(params.session, params.monthly_usd)
    status = budget_status(params.session)
    return json.dumps({"budget_set": stored, "current_status": status}, indent=2)


@mcp.tool(
    name="arbitrage_provider_health",
    annotations={
        "title": "Provider Health",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def arbitrage_provider_health(params: ProviderHealthInput) -> str:
    """
    Report recent health per provider, derived from routing history.

    For each provider seen in the recent window, returns its success rate, the
    number of records scored, and whether it is currently healthy. A provider is
    marked unhealthy only once it has enough recent samples and its success rate
    has dropped to or below the threshold — at which point routing automatically
    deprioritizes it (moves it behind healthy providers, without excluding it).

    Providers absent from the report have no recent history and are treated as
    healthy by the router.

    Args:
        params (ProviderHealthInput):
            - window (int, optional): How many recent records to score (default 50)

    Returns:
        str: JSON mapping provider_id -> {score, samples, healthy}, plus the
             thresholds in effect.
    """
    from health import HEALTH_MIN_SAMPLES, HEALTH_THRESHOLD, HEALTH_WINDOW
    snap = provider_health(limit=params.window)
    unhealthy = [pid for pid, h in snap.items() if not h["healthy"]]
    return json.dumps({
        "providers": snap,
        "unhealthy": unhealthy,
        "thresholds": {
            "window": params.window or HEALTH_WINDOW,
            "min_samples": HEALTH_MIN_SAMPLES,
            "unhealthy_at_success_rate_below_or_equal": HEALTH_THRESHOLD,
        },
        "note": "Unhealthy providers are deprioritized in routing, never excluded.",
    }, indent=2)


@mcp.tool(
    name="arbitrage_count_tokens",
    annotations={
        "title": "Count Input Tokens",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def arbitrage_count_tokens(params: CountTokensInput) -> str:
    """
    Count the input tokens for a set of messages (plus optional system prompt),
    using the same tokenizer cheaprouter uses for pre-flight cost routing.

    Useful for previewing how large a request is before sending it, or feeding
    the count into arbitrage_estimate_cost for an exact price comparison.

    Args:
        params (CountTokensInput):
            - messages (list[dict]): Conversation messages
            - system_prompt (str, optional): System prompt to include

    Returns:
        str: JSON with the total input token count and the counting method in use.
    """
    from tokens import _get_encoder
    total = count_message_tokens(params.messages, params.system_prompt)
    method = "tiktoken:o200k_base" if _get_encoder() is not None else "heuristic"
    return json.dumps({"input_tokens": total, "method": method}, indent=2)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--stdio" in sys.argv:
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")
