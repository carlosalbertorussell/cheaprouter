#!/usr/bin/env python3
"""
Print the age of the price table and exit non-zero if it is stale (S4a).

Intended for CI: a red build is a standing reminder that prices need
re-verification. Run from the repo root:

    python scripts/check_prices.py

Exit codes:
  0 — prices are within max_age_days
  1 — prices are stale (older than max_age_days)
  2 — the price table is missing or malformed
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from pricing_table import VERIFIED_AT, MAX_AGE_DAYS, age_days, is_stale, PriceTableError
except PriceTableError as exc:
    print(f"PRICE TABLE INVALID: {exc}")
    sys.exit(2)
except Exception as exc:  # import-time validation failure
    print(f"PRICE TABLE ERROR: {exc}")
    sys.exit(2)


def main() -> int:
    age = age_days()
    print(f"Price table verified_at : {VERIFIED_AT.isoformat()}")
    print(f"Age                     : {age} days (limit {MAX_AGE_DAYS})")
    if is_stale():
        print(f"STATUS                  : STALE — re-verify prices in prices.json and update 'verified_at'.")
        return 1
    print("STATUS                  : OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
