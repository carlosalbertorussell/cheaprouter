#!/usr/bin/env python3
"""
Check provider price drift against the current table (S4b).

Fetches prices for providers with a programmatic source, compares them to
prices.json, and prints a drift report. It never writes prices.json and never
advances verified_at — it tells you what to verify by hand.

Usage:
    python scripts/refresh_prices.py            # human-readable report
    python scripts/refresh_prices.py --json     # machine-readable report

Exit codes:
  0 — no drift among fetchable providers (manual providers still need eyes)
  1 — drift detected (review and update prices.json)
  2 — the price table itself is invalid
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from refresh import check_drift, format_drift_report
except Exception as exc:
    print(f"PRICE TABLE ERROR: {exc}")
    sys.exit(2)


def main() -> int:
    report = check_drift()
    if "--json" in sys.argv:
        print(json.dumps(report, indent=2))
    else:
        print(format_drift_report(report))
    return 1 if report["any_drift"] else 0


if __name__ == "__main__":
    sys.exit(main())
