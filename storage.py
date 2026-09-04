"""
Pluggable storage backend for cheaprouter routing history (S1.1).

Two backends, selected automatically at import time:

  - SupabaseBackend  — used when SUPABASE_URL and SUPABASE_KEY are set.
                       Durable across MCPize redeploys. Records go to the
                       `routing_history` table.
  - JSONLBackend     — fallback for local/self-hosted use. Appends to the
                       file at ARBITRAGE_HISTORY_FILE (default /tmp).

Both implement the same StorageBackend interface so history.py never needs to
know which is active. Self-hosters are never forced onto Supabase (Invariant C).

PRIVACY (Invariants A & B): records carry only routing metadata — timestamp,
provider, tier, region, token counts, cost, latency, savings, success flag, and
an opaque session token. Never API keys, never message content. The record
schema is enforced by build_record(); callers cannot inject arbitrary fields.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

import httpx


# ─── Record construction (single source of truth for the schema) ──────────────

# The exact set of fields allowed in a stored record. Anything not here is
# dropped before persistence — this is what makes the privacy invariant testable.
ALLOWED_FIELDS = frozenset({
    "timestamp",
    "session",
    "provider_id",
    "provider_name",
    "region",
    "tier",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "saved_usd",
    "latency_ms",
    "success",
    "error_class",
})


def build_record(
    *,
    session: str,
    provider_id: Optional[str],
    provider_name: Optional[str],
    region: Optional[str],
    tier: Optional[str],
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    cost_usd: Optional[float],
    saved_usd: Optional[float],
    latency_ms: Optional[int],
    success: bool,
    error: Optional[str] = None,
) -> dict:
    """
    Build a privacy-clean history record. This is the ONLY way records are made.

    Note there is deliberately no field for message content or API keys. `error`
    is reduced to an error *class* (the exception type name), never the full
    message, since provider error bodies can echo request content.
    """
    error_class = None
    if error:
        # Keep only a short, safe classifier — never the full error body.
        error_class = error.strip().split(":")[0][:60]
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session": session,
        "provider_id": provider_id,
        "provider_name": provider_name,
        "region": region,
        "tier": tier,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round(cost_usd, 8) if cost_usd is not None else None,
        "saved_usd": round(saved_usd, 8) if saved_usd is not None else None,
        "latency_ms": latency_ms,
        "success": success,
        "error_class": error_class,
    }


def sanitize(record: dict) -> dict:
    """Drop any field not in ALLOWED_FIELDS. Defense in depth for the invariant."""
    return {k: v for k, v in record.items() if k in ALLOWED_FIELDS}


# ─── Backend interface ────────────────────────────────────────────────────────

class StorageBackend(Protocol):
    def write(self, record: dict) -> None: ...
    def read(self, session: Optional[str], limit: int) -> list[dict]: ...
    @property
    def name(self) -> str: ...


# ─── JSONL backend (local / self-hosted fallback) ─────────────────────────────

class JSONLBackend:
    def __init__(self, path: Optional[str] = None):
        self.path = Path(path or os.getenv("ARBITRAGE_HISTORY_FILE", "/tmp/routing_history.jsonl"))

    @property
    def name(self) -> str:
        return "jsonl"

    def write(self, record: dict) -> None:
        rec = sanitize(record)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def read(self, session: Optional[str], limit: int) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        out = []
        for line in reversed(lines):  # newest first
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if session is not None and rec.get("session") != session:
                continue
            out.append(rec)
            if len(out) >= limit:
                break
        return out


# ─── Supabase backend (durable cloud store) ───────────────────────────────────

class SupabaseBackend:
    """
    Minimal Supabase REST (PostgREST) client — no SDK dependency.
    Table `routing_history` with columns matching ALLOWED_FIELDS.
    """

    def __init__(self, url: str, key: str, table: str = "routing_history"):
        self.base = url.rstrip("/")
        self.key = key
        self.table = table

    @property
    def name(self) -> str:
        return "supabase"

    @property
    def _headers(self) -> dict:
        return {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }

    def write(self, record: dict) -> None:
        rec = sanitize(record)
        url = f"{self.base}/rest/v1/{self.table}"
        try:
            with httpx.Client(timeout=10.0) as c:
                r = c.post(url, headers={**self._headers, "Prefer": "return=minimal"}, json=rec)
                r.raise_for_status()
        except Exception:
            # History is best-effort — a store failure must never break routing.
            pass

    def read(self, session: Optional[str], limit: int) -> list[dict]:
        url = f"{self.base}/rest/v1/{self.table}"
        params = {"select": "*", "order": "timestamp.desc", "limit": str(limit)}
        if session is not None:
            params["session"] = f"eq.{session}"
        try:
            with httpx.Client(timeout=10.0) as c:
                r = c.get(url, headers=self._headers, params=params)
                r.raise_for_status()
                return r.json()
        except Exception:
            return []


# ─── Backend selection ────────────────────────────────────────────────────────

def get_backend() -> StorageBackend:
    """Supabase when configured, else JSONL. Called fresh so tests can monkeypatch env."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if url and key:
        return SupabaseBackend(url, key)
    return JSONLBackend()
