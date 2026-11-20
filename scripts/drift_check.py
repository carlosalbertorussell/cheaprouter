#!/usr/bin/env python3
"""
Scheduled price drift check (SC7): the freshness-SLA loop.

Runs the SC2 drift check (refresh.check_drift, OpenRouter feed) out-of-band,
classifies each change as significant (>= --threshold-pct) or minor, reports the
table's expiry, and optionally writes a proposal that applies ONLY significant
changes. It never touches verified_at / verified_by: a human verifies each
number on the provider's own page and stamps those fields before merging.

Usage (from the repo root):
    python scripts/drift_check.py [--threshold-pct N] [--expiry-warn-days D]
                                  [--json-out PATH] [--md-out PATH]
                                  [--write-proposal PATH]

  --threshold-pct N      significant if |feed - table| / table * 100 >= N (default 5.0);
                         a table price of 0 with a non-zero feed price is significant
  --expiry-warn-days D   expiry_warning when days_to_expiry <= D (default 10)
  --json-out PATH        machine-readable report (always written when given)
  --md-out PATH          Markdown report, used as the PR body (always written when given)
  --write-proposal PATH  full prices.json candidate with only the significant changes,
                         formatted byte-for-byte like prices.json; written only when
                         there is at least one significant change

Exit codes:
  0  — no significant drift
  10 — significant drift found (proposal written if requested)
  2  — the price table is invalid, or the feed failed for every openrouter provider
  1  — unexpected error (including bad arguments)

Expiry is reported in the JSON and Markdown and never changes the exit code.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_NO_SIGNAL = 2
EXIT_DRIFT = 10


def _today() -> date:
    """Today's UTC date (a seam so tests can fix the clock)."""
    return datetime.now(timezone.utc).date()


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse's default exit code 2 is reserved here
        self.print_usage(sys.stderr)
        self.exit(EXIT_UNEXPECTED, f"{self.prog}: error: {message}\n")


def _parse_args(argv):
    p = _Parser(prog="drift_check.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--threshold-pct", type=float, default=5.0)
    p.add_argument("--expiry-warn-days", type=int, default=10)
    p.add_argument("--json-out")
    p.add_argument("--md-out")
    p.add_argument("--write-proposal")
    args = p.parse_args(argv)
    if args.threshold_pct < 0:
        p.error("--threshold-pct must be >= 0")
    return args


def _write(path: str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def _write_error(args, message: str) -> None:
    if args.json_out:
        _write(args.json_out, json.dumps({"error": message}, indent=2) + "\n")
    if args.md_out:
        _write(args.md_out, f"## Price drift check (SC7)\n\n**Check failed:** {message}\n")


def run(args) -> int:
    try:
        import pricing_table
        table = pricing_table._load_raw()          # fresh, validated read
        original_text = pricing_table._prices_file().read_text(encoding="utf-8")
        import refresh
    except Exception as exc:                       # PriceTableError or import-time failure
        msg = f"price table invalid: {exc}"
        print(f"PRICE TABLE ERROR: {exc}", file=sys.stderr)
        _write_error(args, msg)
        return EXIT_NO_SIGNAL

    report = refresh.check_drift(table)
    sla = refresh.build_sla_report(report, table, _today(),
                                   threshold_pct=args.threshold_pct,
                                   expiry_warn_days=args.expiry_warn_days)
    if args.json_out:
        _write(args.json_out, json.dumps(sla, indent=2) + "\n")
    if args.md_out:
        _write(args.md_out, refresh.render_drift_markdown(sla, table.get("pricing_urls", {})))

    print(f"significant: {len(sla['significant'])}  minor: {len(sla['minor'])}  "
          f"fetch_failed: {len(sla['fetch_failed'])}  manual: {len(sla['manual'])}")
    print(f"expires_on: {sla['expires_on']} ({sla['days_to_expiry']} days)"
          f"{'  EXPIRY WARNING' if sla['expiry_warning'] else ''}")
    print(f"fingerprint: {sla['fingerprint']}")

    if refresh.no_feed_signal(report):
        print("NO SIGNAL: the feed failed for every openrouter provider.", file=sys.stderr)
        return EXIT_NO_SIGNAL
    if not sla["significant"]:
        return EXIT_OK
    if args.write_proposal:
        proposal = refresh.apply_significant(table, sla["significant"])
        _write(args.write_proposal, refresh.dump_like(proposal, original_text))
        print(f"proposal written to {args.write_proposal}")
    return EXIT_DRIFT


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        return run(args)
    except Exception:
        traceback.print_exc()
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())
