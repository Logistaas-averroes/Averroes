#!/usr/bin/env python3
"""Operator audit of durable vs legacy keyword data (PR-ADS-146 §4). Strictly
READ-ONLY — SELECT only; never deletes, relabels or assigns a currency to any
row, and never touches the legacy ``keywords`` snapshot table.

Reports durable-fact health: durable rows + distinct criteria, earliest/latest
durable source dates, rows with missing immutable IDs, rows with unverified
currency lineage, duplicate-key candidates (should be 0 under the unique index),
rows with a latest quality observation, and the untouched legacy snapshot count.

Usage: python scripts/audit_keyword_currency.py [--json]
Requires DATABASE_URL. Exits 0 always (a read-only report).
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from db.connection import init_pool
    from db.keyword_repository import fetch_keyword_legacy_audit

    init_pool()
    audit = fetch_keyword_legacy_audit()

    if "--json" in sys.argv:
        print(json.dumps(audit, indent=2, default=str))
        return 0

    if not audit.get("available"):
        print("Keyword audit unavailable (database not reachable).")
        return 0

    s = audit.get("summary") or {}
    print("── Keyword Evidence durable/legacy audit (read-only) ──")
    print(f"Durable source table:         keyword_daily_facts")
    print(f"Legacy snapshot table:        keywords (untouched)")
    print(f"Durable rows:                 {s.get('durable_rows')}")
    print(f"Durable unique criteria:      {s.get('durable_criteria')}")
    print(f"Durable source-date range:    {s.get('durable_source_date_min')} → {s.get('durable_source_date_max')}")
    print(f"Distinct source dates:        {s.get('distinct_source_dates')}")
    print(f"Rows missing immutable IDs:   {s.get('rows_missing_ids')}")
    print(f"Rows unverified currency:     {s.get('rows_unverified_currency')}")
    print(f"Rows with quality observed:   {s.get('rows_with_quality')}")
    print(f"Duplicate-key candidates:     {s.get('duplicate_key_groups')} (expected 0)")
    print(f"Legacy snapshot rows:         {s.get('legacy_snapshot_rows')}")
    print(f"Legacy run_date range:        {s.get('legacy_run_date_min')} → {s.get('legacy_run_date_max')}")
    print(f"\nNatural key: {s.get('natural_key')}")
    print("(Durable facts upsert on this key — overlapping scheduler snapshots "
          "never multiply. Unverified-currency rows are quarantined from USD, "
          "never assigned GBP/USD. No row is deleted, relabelled or converted.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
