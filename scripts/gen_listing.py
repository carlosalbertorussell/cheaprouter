#!/usr/bin/env python3
"""
Regenerate the pricing table in MCPIZE_LISTING.md from prices.json (ST4).

The block between the BEGIN/END markers is owned by this script; everything
else in the listing is hand-written. Run from anywhere:

    python scripts/gen_listing.py           # rewrite the block in place
    python scripts/gen_listing.py --check   # exit 1 if the block is out of date

tests/test_listing.py runs the --check logic, so CI fails when the listing's
prices drift from prices.json.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRICES = ROOT / "prices.json"
LISTING = ROOT / "MCPIZE_LISTING.md"

BEGIN = "<!-- BEGIN GENERATED: prices (scripts/gen_listing.py) -->"
END = "<!-- END GENERATED: prices -->"

TIER_ORDER = ["tier_fast", "tier_balanced", "tier_powerful"]
TIER_LABEL = {"tier_fast": "fast", "tier_balanced": "balanced", "tier_powerful": "powerful"}
CARRIED_MARK = "CARRIED, NOT RE-READ"


def _fmt(x) -> str:
    """Price with at least two decimals, no trailing float noise."""
    if x is None:
        return "—"
    s = f"{float(x):.6f}".rstrip("0")
    whole, _, frac = s.partition(".")
    return f"{whole}.{frac.ljust(2, '0')}"


def carried_providers(table: dict) -> list[str]:
    """Providers whose prices.json notes say their prices were carried, not re-read."""
    notes = table.get("notes", {})
    return [pid for pid in table["providers"]
            if any(k.startswith(pid) and CARRIED_MARK in str(v) for k, v in notes.items())]


def tier_models(entry) -> list:
    """A tier's models: one object (the original form) or a list of them (SC3a).
    Same rule as pricing_table.tier_models, without importing it (which would
    load the shipped prices.json rather than the table passed in)."""
    return [entry] if isinstance(entry, dict) else list(entry)


def rows(table: dict) -> list[str]:
    """One row per provider per distinct model/price, tiers merged (e.g. fast/balanced).
    A tier listing several models (SC3a) gives each its own row."""
    carried = set(carried_providers(table))
    out = []
    for pid, p in table["providers"].items():
        groups: list[tuple[tuple, list[str]]] = []
        for tier in TIER_ORDER:
            for m in tier_models(p["models"][tier]):
                key = (m["model_id"], m["input_price_per_1m"], m["output_price_per_1m"],
                       m.get("cached_input_price_per_1m"))
                for k, tiers in groups:
                    if k == key:
                        tiers.append(TIER_LABEL[tier])
                        break
                else:
                    groups.append((key, [TIER_LABEL[tier]]))
        status = "carried, not re-read" if pid in carried else "re-read"
        for (model_id, inp, outp, cache), tiers in groups:
            out.append(f"| {p['name']} | {'/'.join(tiers)} | {model_id} | {_fmt(inp)} "
                       f"| {_fmt(outp)} | {_fmt(cache)} | {status} |")
    return out


def tier_winners(table: dict, input_tokens: int = 1000, output_tokens: int = 500) -> dict:
    """What route() picks per tier with every key supplied, no constraints, no
    cache and health ignored — computed, never hand-written."""
    sys.path.insert(0, str(ROOT))
    from providers import _build_providers
    from router import route
    registry = _build_providers(table)
    keys = {pid: "listing" for pid in registry}
    out = {}
    for tier in TIER_ORDER:
        w = route(registry, tier, input_tokens, output_tokens, keys, health_aware=False).winner
        out[tier] = f"{w.provider_name} {w.model_id}"
    return out


def render(table: dict) -> str:
    v = table["verified_at"]
    carried = [table["providers"][pid]["name"] for pid in carried_providers(table)]
    lines = [
        BEGIN,
        f"Prices are USD per 1M tokens from `prices.json` (verified_at {v}, "
        f"max_age_days {table['max_age_days']}). Cache = the cache-read rate cheaprouter "
        f"models; — means cached tokens are billed at the full input rate.",
        "",
        "| Provider | Tier | Model | Input | Output | Cache | Status |",
        "|----------|------|-------|------:|-------:|------:|--------|",
        *rows(table),
        "",
    ]
    if carried:
        lines.append(
            f"**Carried, not re-read on {v}:** {', '.join(carried)}. `prices.json` keeps "
            f"their previous values because no priced page could be read; treat them as "
            f"unverified until the next sweep.")
        lines.append("")
    lines.append(
        "These are list prices at standard context. DeepSeek is encoded at off-peak rates "
        "(peak is about 2x), Qwen at the Singapore endpoint with qwen-plus at the "
        "non-thinking rate. See the `notes` in `prices.json` for every caveat. Your "
        "negotiated rates may differ.")
    w = tier_winners(table)
    lines.append("")
    lines.append(
        f"With every key supplied, no constraints and no cache, routing picks "
        f"**{w['tier_fast']}** for fast, **{w['tier_balanced']}** for balanced and "
        f"**{w['tier_powerful']}** for powerful (1,000 input / 500 output tokens). "
        f"Other volumes, cache hits, `latency_sensitive` or excluded providers can "
        f"change the winner; `arbitrage_estimate_cost` shows it for your request.")
    lines.append(END)
    return "\n".join(lines)


def splice(listing: str, block: str) -> str:
    start, end = listing.index(BEGIN), listing.index(END) + len(END)
    return listing[:start] + block + listing[end:]


def main(argv: list[str]) -> int:
    table = json.loads(PRICES.read_text())
    current = LISTING.read_text()
    updated = splice(current, render(table))
    if "--check" in argv:
        if updated != current:
            print("MCPIZE_LISTING.md pricing block is out of date; run scripts/gen_listing.py")
            return 1
        print("MCPIZE_LISTING.md pricing block matches prices.json")
        return 0
    LISTING.write_text(updated)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
