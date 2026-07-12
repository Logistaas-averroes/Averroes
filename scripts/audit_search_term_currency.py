#!/usr/bin/env python3
"""Operator audit of legacy search_terms rows with unverified currency lineage
(PR-ADS-145 §3). Strictly READ-ONLY — SELECT only; never deletes, relabels or
assigns a currency to any row.

Reports the legacy unverified row count, date range, campaigns and terms
represented, and whether each legacy row later gained an EXACT verified Google
Ads replacement (same durable natural key, with proven lineage).

Usage: python scripts/audit_search_term_currency.py [--json]
Requires DATABASE_URL. Exits 0 always (a read-only report).
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from db.connection import init_pool
    from db.search_term_repository import fetch_legacy_currency_audit

    init_pool()
    audit = fetch_legacy_currency_audit()

    if "--json" in sys.argv:
        print(json.dumps(audit, indent=2, default=str))
        return 0

    if not audit.get("available"):
        print("Legacy currency audit unavailable (database not reachable).")
        return 0

    s = audit.get("summary") or {}
    print("── Search-term legacy currency audit (read-only) ──")
    print(f"Marker:                       legacy_currency_unverified")
    print(f"Legacy unverified rows:       {s.get('legacy_unverified_row_count')}")
    print(f"Terms represented:            {s.get('terms_represented')}")
    print(f"Campaigns represented:        {s.get('campaigns_represented')}")
    print(f"Source-date range:            {s.get('min_source_date')} → {s.get('max_source_date')}")
    print(f"Rows with verified replacement: {s.get('rows_with_verified_replacement')}")
    campaigns = s.get("campaigns") or []
    if campaigns:
        print("Campaigns:")
        for c in campaigns:
            print(f"  - {c}")
    print(f"\n(These rows are preserved. Never assigned GBP/USD from account "
          f"currency; future verified Google Ads imports use the durable "
          f"currency contract.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
