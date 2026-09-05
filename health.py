"""
Provider health tracking for cheaprouter (S8).

Turns the routing history (storage.py) into a live signal: a provider that has
been failing recently is *deprioritized* in routing — pushed toward the back of
the cost ranking — rather than excluded outright. It stays eligible, so a brief
blip never leaves a caller with no provider; it just won't be chosen while a
healthy, comparably-priced alternative exists.

This is the read-side companion to the failure records log_decision() already
writes. It reads only metadata (provider_id, success, timestamp) — no keys, no
content — so it inherits S1's privacy guarantees for free.

Health score: the recent success rate for a provider over the last
HEALTH_WINDOW records, with a minimum sample size before the signal is trusted.
Providers with no recent history are treated as healthy (score 1.0) — absence of
evidence is not evidence of failure.
"""

from __future__ import annotations

import os
from collections import defaultdict
from typing import Optional

from history import read_history

# How many recent records to consider when scoring health.
HEALTH_WINDOW = int(os.getenv("HEALTH_WINDOW", "50"))

# Minimum records for a provider before its score is trusted. Below this, the
# provider is treated as healthy regardless — too little data to condemn it.
HEALTH_MIN_SAMPLES = int(os.getenv("HEALTH_MIN_SAMPLES", "3"))

# Success-rate at or below this marks a provider unhealthy (deprioritized).
# Default 0.5 → a provider failing more than half its recent calls drops back.
HEALTH_THRESHOLD = float(os.getenv("HEALTH_THRESHOLD", "0.5"))


def provider_health(limit: int = None) -> dict[str, dict]:
    """
    Compute a health snapshot per provider from recent global history.

    Returns { provider_id: {score, samples, healthy} }.
      - score:   recent success rate in [0.0, 1.0]
      - samples: number of records the score is based on
      - healthy: False only when samples >= HEALTH_MIN_SAMPLES AND
                 score <= HEALTH_THRESHOLD
    Providers absent from history do not appear here (callers treat missing as healthy).
    """
    window = limit if limit is not None else HEALTH_WINDOW
    records = read_history(limit=window)

    totals: dict[str, int] = defaultdict(int)
    successes: dict[str, int] = defaultdict(int)
    for r in records:
        pid = r.get("provider_id")
        if not pid or pid.startswith("__"):   # skip budget markers etc.
            continue
        totals[pid] += 1
        if r.get("success"):
            successes[pid] += 1

    snapshot: dict[str, dict] = {}
    for pid, n in totals.items():
        score = successes[pid] / n if n else 1.0
        healthy = not (n >= HEALTH_MIN_SAMPLES and score <= HEALTH_THRESHOLD)
        snapshot[pid] = {
            "score": round(score, 3),
            "samples": n,
            "healthy": healthy,
        }
    return snapshot


def is_healthy(provider_id: str, snapshot: dict[str, dict]) -> bool:
    """A provider is healthy unless the snapshot explicitly marks it unhealthy."""
    entry = snapshot.get(provider_id)
    if entry is None:
        return True   # no recent data → healthy
    return entry["healthy"]
