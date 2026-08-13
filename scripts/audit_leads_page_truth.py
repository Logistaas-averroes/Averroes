#!/usr/bin/env python3
"""
scripts/audit_leads_page_truth.py

PR-ADS-153C §41 — READ-ONLY production validation for the canonical Leads page.

Run on Render AFTER deployment:

    python -m scripts.audit_leads_page_truth

Optional:
    --window current_quarter      business window (default: current_quarter)
    --json                        machine-readable output

Guarantees
----------
  * SELECT statements only. No INSERT / UPDATE / DELETE / DDL.
  * No HubSpot, Google Ads or any other external API call.
  * Never prints an email address — company and contact id only.

Reports
-------
  1. Canonical sync      bootstrap status, total contacts, modification watermark
  2. Current window      Leads / MQLs / SQLs / Opportunities / Lifecycle Customers,
                         each on its OWN stage-entry date
  3. SQL reconciliation  canonical lifecycle SQL vs legacy qualified, split into
                         date-shift and population causes
  4. Source scopes       all-source / Google Ads-source / campaign-attributable /
                         keyword-attributable SQLs
  5. Operational status  counts per working-status category
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _fmt(value) -> str:
    return "unavailable" if value is None else str(value)


def _section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def collect(window: str) -> dict:
    """Gather every read-only block for the given business window."""
    from services import canonical_crm_funnel_service as funnel
    from services import crm_funnel_reconciliation_service as recon
    from services import hubspot_contact_funnel_sync_service as sync

    scopes = {}
    for scope in funnel.ORDERED_SCOPES:
        payload = funnel.build(funnel.WINDOW_BUSINESS, window, scope=scope)
        scopes[scope] = payload

    return {
        "window": window,
        "sync": sync.build_coverage(),
        "funnel": scopes[funnel.SCOPE_ALL_SOURCE],
        "scopes": scopes,
        "operational": funnel.operational_status_breakdown(
            funnel.WINDOW_BUSINESS, window, event=funnel.EVENT_LEAD),
        "reconciliation": recon.run(business_window=window),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", default="current_quarter")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    from db.connection import init_pool
    from services import canonical_crm_funnel_service as funnel

    init_pool()
    data = collect(args.window)

    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0

    print("=" * 72)
    print("PR-ADS-153C — CANONICAL LEADS PAGE (read-only production audit)")
    print("=" * 72)

    # ── 1. Canonical sync ────────────────────────────────────────────────────
    _section("1. CANONICAL CRM SYNC")
    sync = data["sync"]
    if not sync.get("available"):
        print(f"  UNAVAILABLE — {sync.get('reason', 'unknown')}")
        print("  (Unavailable is not zero. Nothing below can be interpreted.)")
    else:
        totals = sync.get("totals") or {}
        print(f"  bootstrap status        : {_fmt(sync.get('bootstrap_status'))}")
        print(f"  bootstrap completed at  : {_fmt(sync.get('bootstrap_completed_at'))}")
        print(f"  modification watermark  : {_fmt(sync.get('last_modified_watermark'))}")
        print(f"  last incremental sync   : {_fmt(sync.get('last_incremental_at'))}")
        print(f"  total canonical contacts: {_fmt(totals.get('contacts'))}")
        if (sync.get("bootstrap_status") or "") != "complete":
            print()
            print("  ⚠ Historical bootstrap is INCOMPLETE — All Time does not yet")
            print("    represent full history. The Leads page surfaces this too.")

    # ── 2. Current window funnel ─────────────────────────────────────────────
    _section(f"2. FUNNEL — {data['window'].upper()} (each stage on its own event date)")
    payload = data["funnel"]
    if not payload.get("available"):
        print("  unavailable — canonical contact store unreadable")
    else:
        w = payload.get("window") or {}
        print(f"  window: {w.get('start_date') or '(all time)'} → {w.get('end_date')}")
        print(f"  scope : {payload.get('scope_label')}")
        print(f"  status: {(payload.get('reconciliation') or {}).get('status')}")
        print()
        for event, block in (payload.get("events") or {}).items():
            print(f"    {block.get('label', event):<22} {_fmt(block.get('count')):>10}"
                  f"   {block.get('event_date_property')}")
        print()
        print("  Cohort-safe conversions (blank = not cohort-safe for this window):")
        for conv in (payload.get("conversions") or []):
            rate = "unavailable" if not conv.get("available") else f"{conv.get('rate_pct')}%"
            print(f"    {conv['from_event']:>12} → {conv['to_event']:<14} {rate:>12}"
                  f"   (cohort {conv.get('cohort_size')})")

    # ── 3. SQL reconciliation ────────────────────────────────────────────────
    _section("3. SQL RECONCILIATION — canonical lifecycle vs legacy qualified")
    reconciliation = data["reconciliation"]
    if not reconciliation.get("available"):
        print(f"  unavailable — {reconciliation.get('reason')}")
    else:
        comparison = reconciliation.get("sql_comparison") or {}
        print(f"  canonical lifecycle SQLs : {_fmt(comparison.get('lifecycle_sql_count'))}")
        print(f"  legacy qualified count   : {_fmt(comparison.get('legacy_sql_count'))}")
        print(f"  delta                    : {_fmt(comparison.get('delta'))}")
        print(f"  overlap                  : {_fmt(comparison.get('overlap_contacts'))}")
        print()
        print("  Why the number differs:")
        print(f"    date-shifted (SQL under both doctrines, different window): "
              f"{_fmt(comparison.get('date_shifted_contacts'))}")
        print(f"    population (legacy qualified, HubSpot never marked SQL)  : "
              f"{_fmt(comparison.get('missing_sql_event_date_contacts'))}")

    # ── 4. Source scopes ─────────────────────────────────────────────────────
    _section("4. SOURCE SCOPES — SQLs by named attribution scope")
    for scope in funnel.ORDERED_SCOPES:
        scoped = data["scopes"].get(scope) or {}
        block = (scoped.get("events") or {}).get("sql") or {}
        note = ""
        if scoped.get("campaign_identity_available") is False and scope in (
                funnel.SCOPE_CAMPAIGN_ATTRIBUTABLE, funnel.SCOPE_KEYWORD_ATTRIBUTABLE):
            note = "  (campaign identity unavailable — withheld, not zero)"
        print(f"    {scope:<24} {_fmt(block.get('count')):>10}{note}")
    print()
    print("  Invariant: keyword ≤ campaign ≤ google_ads_source ≤ all_source")

    # ── 5. Operational status ────────────────────────────────────────────────
    _section("5. OPERATIONAL STATUS (working dimension — NOT a funnel stage)")
    operational = data["operational"]
    if not operational.get("available"):
        print("  unavailable")
    else:
        counts = operational.get("counts") or {}
        for category in operational.get("categories") or []:
            print(f"    {category:<26} {counts.get(category, 0)}")

    print()
    print("=" * 72)
    print("Read-only audit complete. No writes were performed.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
