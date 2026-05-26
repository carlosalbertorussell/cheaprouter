"""
Routing history logger for cheaprouter.

Appends each routing decision and completion result to a JSONL file.
Each line is a self-contained JSON record.

Default path: ./routing_history.jsonl
Override with env var ARBITRAGE_HISTORY_FILE.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

HISTORY_FILE = Path(os.getenv("ARBITRAGE_HISTORY_FILE", "/tmp/routing_history.jsonl"))


def log_decision(
    decision_dict: dict,
    completion_text: Optional[str] = None,
    actual_input_tokens: Optional[int] = None,
    actual_output_tokens: Optional[int] = None,
    actual_latency_ms: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    """Append one routing record to the history file."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "routing": decision_dict,
        "completion": {
            "success": error is None,
            "error": error,
            "actual_input_tokens": actual_input_tokens,
            "actual_output_tokens": actual_output_tokens,
            "actual_latency_ms": actual_latency_ms,
            "response_preview": (completion_text or "")[:200],
        },
    }
    with open(HISTORY_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def read_history(limit: int = 50) -> list[dict]:
    """Read the last `limit` records from the history file, newest first."""
    if not HISTORY_FILE.exists():
        return []
    lines = HISTORY_FILE.read_text(encoding="utf-8").strip().splitlines()
    recent = lines[-limit:][::-1]  # reverse so newest first
    records = []
    for line in recent:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def history_summary(limit: int = 50) -> dict:
    """
    Aggregate stats over the last `limit` records.
    Returns total spend, total savings, provider usage counts.
    """
    records = read_history(limit)
    if not records:
        return {"total_records": 0, "message": "No history found."}

    total_cost = 0.0
    total_saved = 0.0
    provider_counts: dict[str, int] = {}
    successful = 0
    failed = 0

    for r in records:
        routing = r.get("routing", {})
        winner = routing.get("winner")
        completion = r.get("completion", {})

        if completion.get("success"):
            successful += 1
        else:
            failed += 1

        if winner:
            pid = winner.get("provider_id", "unknown")
            provider_counts[pid] = provider_counts.get(pid, 0) + 1
            total_cost += winner.get("total_cost_usd", 0)

        savings = routing.get("savings")
        if savings:
            total_saved += savings.get("saved_usd", 0)

    return {
        "total_records": len(records),
        "successful": successful,
        "failed": failed,
        "total_cost_usd": round(total_cost, 6),
        "total_saved_usd": round(total_saved, 6),
        "provider_usage": provider_counts,
    }
