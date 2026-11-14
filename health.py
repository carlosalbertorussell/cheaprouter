"""
Provider health tracking for cheaprouter (S8, made abuse-resistant in ST5).

Turns the routing history (storage.py) into a live signal: a provider that has
been failing recently is *deprioritized* in routing — pushed toward the back of
the cost ranking — rather than excluded outright. It stays eligible, so a brief
blip never leaves a caller with no provider; it just won't be chosen while any
healthy alternative exists, whatever that alternative costs.

Health is scored on the server's global history, shared across every caller, so
it must not be something one caller can steer for everyone else (ST5):

  1. Only provider-side failures count. A failure counts against a provider only
     when its record says `transient: true` — a 429, a 5xx, a timeout or a
     connection error, classified at write time by client.is_transient_error.
     Bad keys (401/403) and bad requests (400/404/422) are the caller's problem
     and are ignored entirely: they count neither for nor against the provider.
  2. Legacy records (written before ST5, with no `transient` field) are
     classified conservatively: a legacy success still counts as a success, a
     legacy failure does not count at all. The pre-ST5 `error_class` cannot
     reliably tell a timeout from a bad key, and guessing wrong would let old
     caller errors keep a provider deprioritized. The cost is that pre-ST5
     outages are forgotten at deploy; new records rebuild the signal within one
     window. A failure record whose `transient` is None (unclassified) is
     treated the same way.
  3. Each session's say is capped. Within the window, at most
     HEALTH_MAX_PER_SESSION records per (session, provider) are scored — the
     newest ones — so one caller hammering a provider, with failures or with
     successes, cannot outvote everyone else. Records carry only the hashed
     session key; this module never sees a raw token. All callers that pass no
     session share the "anon" key, and so share one cap.
  4. One session alone cannot condemn a provider. A provider is marked
     unhealthy only when its scored failures come from at least
     HEALTH_MIN_SESSIONS distinct sessions (default 2). Without this, a caller
     flooding the shared recent list with its own 429s could push everyone
     else's records out of the window and leave only its capped failures. A
     single-user server (every call from one session, e.g. local stdio with no
     token) should set HEALTH_MIN_SESSIONS=1 to get deprioritization back.

The window is HEALTH_WINDOW *scored* records: up to HEALTH_SCAN_FACTOR times that
many raw records are read (one request) so that ignored caller errors cannot
flood the window and push real evidence out of it.

Limit, stated plainly: session tokens are chosen by the caller, so someone who
rotates tokens can still look like several callers. The cap stops a single
caller from dominating; it is not Sybil-proof.

This reads history unscoped, directly through history.read_history — it is an
internal, server-side aggregate, never the caller-facing history tool, whose
reads are scoped to the caller (ST5). It reads only metadata (provider_id,
success, transient, session hash) — no keys, no content.

Health score: the success rate over the scored records for a provider, with a
minimum sample size before the signal is trusted. Providers with no scored
history are treated as healthy — absence of evidence is not evidence of failure.
"""

from __future__ import annotations

import os
from collections import defaultdict

from history import read_history


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


# How many recent scored records to consider when scoring health.
HEALTH_WINDOW = int(os.getenv("HEALTH_WINDOW", "50"))

# Minimum scored records for a provider before its score is trusted. Below this,
# the provider is treated as healthy regardless — too little data to condemn it.
HEALTH_MIN_SAMPLES = int(os.getenv("HEALTH_MIN_SAMPLES", "3"))

# Success-rate at or below this marks a provider unhealthy (deprioritized).
# Default 0.5 → a provider failing more than half its recent calls drops back.
HEALTH_THRESHOLD = float(os.getenv("HEALTH_THRESHOLD", "0.5"))

# Most records one session can contribute to one provider's score (ST5).
HEALTH_MAX_PER_SESSION = _int_env("HEALTH_MAX_PER_SESSION", 5)

# Distinct sessions with a scored failure needed before a provider is unhealthy.
HEALTH_MIN_SESSIONS = _int_env("HEALTH_MIN_SESSIONS", 2)

# Raw records read per scored record, so ignored records can't crowd out real ones.
HEALTH_SCAN_FACTOR = _int_env("HEALTH_SCAN_FACTOR", 4)


def counts_for_health(record: dict) -> bool:
    """
    Whether a history record is evidence about the provider's health.

    Successes always count. A failure counts only when it is explicitly marked
    transient (provider-side). Caller errors, unclassified failures and legacy
    failures (no `transient` field) do not count.
    """
    if record.get("success"):
        return True
    return record.get("transient") is True


def provider_health(limit: int = None) -> dict[str, dict]:
    """
    Compute a health snapshot per provider from recent global history.

    Returns { provider_id: {score, samples, healthy, sessions, failing_sessions} }.
      - score:    success rate over the scored records, in [0.0, 1.0]
      - samples:  number of records the score is based on
      - healthy:  False only when samples >= HEALTH_MIN_SAMPLES AND
                  score <= HEALTH_THRESHOLD AND
                  failing_sessions >= HEALTH_MIN_SESSIONS
      - sessions: number of distinct sessions those records came from
      - failing_sessions: how many of those sessions had a scored failure
    Providers with no scored records do not appear (callers treat missing as healthy).
    """
    window = limit if limit is not None else HEALTH_WINDOW
    records = read_history(limit=window * HEALTH_SCAN_FACTOR)   # newest first

    totals: dict[str, int] = defaultdict(int)
    successes: dict[str, int] = defaultdict(int)
    per_session: dict[tuple, int] = defaultdict(int)
    sessions: dict[str, set] = defaultdict(set)
    failing: dict[str, set] = defaultdict(set)
    scored = 0
    for r in records:
        if scored >= window:
            break
        pid = r.get("provider_id")
        if not pid or pid.startswith("__"):   # skip budget markers etc.
            continue
        if not counts_for_health(r):
            continue                          # caller error / legacy failure
        sess = r.get("session") or "anon"
        if per_session[(sess, pid)] >= HEALTH_MAX_PER_SESSION:
            continue                          # this session has had its say
        per_session[(sess, pid)] += 1
        sessions[pid].add(sess)
        scored += 1
        totals[pid] += 1
        if r.get("success"):
            successes[pid] += 1
        else:
            failing[pid].add(sess)

    snapshot: dict[str, dict] = {}
    for pid, n in totals.items():
        score = successes[pid] / n if n else 1.0
        healthy = not (n >= HEALTH_MIN_SAMPLES and score <= HEALTH_THRESHOLD
                       and len(failing[pid]) >= HEALTH_MIN_SESSIONS)
        snapshot[pid] = {
            "score": round(score, 3),
            "samples": n,
            "healthy": healthy,
            "sessions": len(sessions[pid]),
            "failing_sessions": len(failing[pid]),
        }
    return snapshot


def is_healthy(provider_id: str, snapshot: dict[str, dict]) -> bool:
    """A provider is healthy unless the snapshot explicitly marks it unhealthy."""
    entry = snapshot.get(provider_id)
    if entry is None:
        return True   # no recent data → healthy
    return entry["healthy"]
