#!/usr/bin/env python3
"""
scripts/audit_crm_funnel_truth.py

PR-ADS-153B §31 — READ-ONLY production validation for canonical CRM funnel truth.

Run on Render AFTER deployment:

    python -m scripts.audit_crm_funnel_truth

Optional:
    --window current_quarter      business window for the SQL before/after block
    --json                        machine-readable output

Guarantees
----------
  * Executes SELECT statements only. No INSERT / UPDATE / DELETE / DDL.
  * Never calls HubSpot, Google Ads, or any external API.
  * Never prints an email address — contact id and company only.

Reports
-------
  1. Funnel coverage      contacts, lifecycle-stage coverage, per-stage entry-date
                          coverage, bootstrap status
  2. MQL status           counts by raw value, counts by operational mapping,
                          unmapped values, free-text pollution, missing status
  3. Legacy reconciliation legacy qualified vs lifecycle SQL: overlap, legacy-only,
                          lifecycle-only, and the date-shift vs population split
  4. Source              all-source contacts and each acquisition group
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


def collect() -> dict:
    """Gather every read-only block. Returns a plain dict (JSON-serialisable)."""
    from analysis.mql_status_taxonomy import CATEGORY_LABELS
    from analysis.source_classification import GROUP_LABELS, classify_source
    from db import crm_funnel_repository as repo
    from services import crm_funnel_reconciliation_service as recon
    from services import hubspot_contact_funnel_sync_service as sync

    coverage = sync.build_coverage()
    status_distribution = repo.fetch_coverage_summary()
    pollution = repo.fetch_polluted_mql_status_rows()

    # Acquisition-group rollup from the raw source distribution (pure mapping).
    groups: dict[str, int] = {}
    if status_distribution.get("available"):
        for row in status_distribution.get("by_source") or []:
            raw = row.get("hs_analytics_source")
            group = classify_source(None if raw == "(null)" else raw, None)
            groups[group] = groups.get(group, 0) + int(row.get("contacts") or 0)

    # Operational MQL-status rollup.
    categories: dict[str, int] = {}
    if status_distribution.get("available"):
        for row in status_distribution.get("by_mql_status") or []:
            category = row.get("mql_status_category") or "(null)"
            categories[category] = categories.get(category, 0) + int(
                row.get("contacts") or 0)

    return {
        "coverage": coverage,
        "distribution": status_distribution,
        "pollution": pollution,
        "acquisition_groups": groups,
        "group_labels": GROUP_LABELS,
        "mql_categories": categories,
        "category_labels": CATEGORY_LABELS,
        "reconciliation": recon.run,  # callable, resolved by the caller with a window
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", default="current_quarter",
                        help="business window for the SQL before/after block")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    from db.connection import init_pool
    from services import crm_funnel_reconciliation_service as recon

    init_pool()

    data = collect()
    data.pop("reconciliation", None)
    reconciliation = recon.run(business_window=args.window)

    if args.json:
        print(json.dumps({**data, "reconciliation": reconciliation},
                         indent=2, default=str))
        return 0

    coverage = data["coverage"]
    print("=" * 72)
    print("PR-ADS-153B — CANONICAL CRM FUNNEL TRUTH (read-only production audit)")
    print("=" * 72)

    # ── 1. Funnel coverage ───────────────────────────────────────────────────
    _section("1. FUNNEL COVERAGE")
    if not coverage.get("available"):
        print("  Canonical contact store UNAVAILABLE — "
              f"{coverage.get('reason', 'unknown reason')}")
        print("  (Unavailable is not zero. Nothing below can be interpreted.)")
    else:
        totals = coverage.get("totals") or {}
        stages = coverage.get("stage_entry_coverage") or {}
        print(f"  bootstrap status          : {_fmt(coverage.get('bootstrap_status'))}")
        print(f"  bootstrap completed at    : {_fmt(coverage.get('bootstrap_completed_at'))}")
        print(f"  last incremental sync     : {_fmt(coverage.get('last_incremental_at'))}")
        print(f"  modification watermark    : {_fmt(coverage.get('last_modified_watermark'))}")
        print(f"  last sync error           : {_fmt(coverage.get('last_error'))}")
        print()
        print(f"  total canonical contacts  : {_fmt(totals.get('contacts'))}")
        print(f"  with lifecycle stage      : {_fmt(totals.get('with_lifecycle_stage'))}")
        print(f"  with mql_status           : {_fmt(totals.get('with_mql_status'))}")
        print()
        print("  Stage-entry date coverage (a gap is a coverage gap, never createdate):")
        for event in ("lead", "mql", "sql", "opportunity", "customer"):
            print(f"    entered {event:<12}: {_fmt(stages.get(event))}")
        print()
        print(f"  earliest contact created  : {_fmt(coverage.get('earliest_created_at'))}")
        print(f"  latest contact created    : {_fmt(coverage.get('latest_created_at'))}")
        print(f"  latest modification held  : {_fmt(coverage.get('latest_modified_at'))}")
        print()
        print("  Contacts by lifecycle stage:")
        for row in coverage.get("by_lifecycle_stage") or []:
            print(f"    {str(row.get('lifecycle_stage')):<28} {row.get('contacts')}")

    # ── 2. MQL status ────────────────────────────────────────────────────────
    _section("2. MQL STATUS (operational dimension — NOT the funnel definition)")
    distribution = data["distribution"]
    if not distribution.get("available"):
        print("  unavailable")
    else:
        print("  By raw HubSpot value:")
        for row in distribution.get("by_mql_status") or []:
            print(f"    {str(row.get('mql_status')):<32} "
                  f"{str(row.get('mql_status_category')):<24} {row.get('contacts')}")
        print()
        print("  By operational category:")
        labels = data["category_labels"]
        for category, count in sorted(data["mql_categories"].items(),
                                      key=lambda kv: -kv[1]):
            print(f"    {category:<28} {labels.get(category, category):<32} {count}")
        print()
        print("  no_verdict = property is null. unmapped = a value Averroes does not")
        print("  recognise (a NEW production value that needs a mapping decision).")

    pollution = data["pollution"]
    print()
    print("  Legacy free-text pollution in leads.mql_status "
          "(detection only — nothing is rewritten):")
    if not pollution.get("available"):
        print("    unavailable")
    else:
        print(f"    rows with a non-HubSpot value : {_fmt(pollution.get('total'))}")
        sample = pollution.get("rows") or []
        for row in sample[:15]:
            print(f"      {str(row.get('raw_value'))[:70]}")
        if len(sample) > 15:
            print(f"      … and {len(sample) - 15} more distinct values")

    # ── 3. Legacy reconciliation ─────────────────────────────────────────────
    _section("3. LEGACY vs LIFECYCLE RECONCILIATION")
    if not reconciliation.get("available"):
        print(f"  unavailable — {reconciliation.get('reason')}")
    else:
        window = reconciliation.get("window") or {}
        comparison = reconciliation.get("sql_comparison") or {}
        print(f"  window: {window.get('window_key')} "
              f"({window.get('start_date')} → {window.get('end_date')})")
        print()
        print(f"  legacy SQL count          : {_fmt(comparison.get('legacy_sql_count'))}")
        print(f"  lifecycle SQL count       : {_fmt(comparison.get('lifecycle_sql_count'))}")
        print(f"  delta                     : {_fmt(comparison.get('delta'))}")
        print(f"  overlap contacts          : {_fmt(comparison.get('overlap_contacts'))}")
        print(f"  legacy-only contacts      : {_fmt(comparison.get('legacy_only_contacts'))}")
        print(f"  lifecycle-only contacts   : {_fmt(comparison.get('lifecycle_only_contacts'))}")
        print()
        print("  Why the number moved:")
        print(f"    date-shifted (SQL under both doctrines, different window): "
              f"{_fmt(comparison.get('date_shifted_contacts'))}")
        print(f"    population (legacy qualified, HubSpot never marked SQL)  : "
              f"{_fmt(comparison.get('missing_sql_event_date_contacts'))}")
        print()
        print("  Attribution coverage of the lifecycle SQL set:")
        for scope, count in (comparison.get("attribution_coverage") or {}).items():
            print(f"    {scope:<26} {count}")
        print()
        print("  Mismatch classes:")
        mismatches = reconciliation.get("mismatches") or {}
        labels = mismatches.get("labels") or {}
        for cls, count in sorted((mismatches.get("counts") or {}).items(),
                                 key=lambda kv: -kv[1]):
            if count:
                print(f"    {cls:<46} {count}")
                print(f"      → {labels.get(cls, '')}")

    # ── 4. Source ────────────────────────────────────────────────────────────
    _section("4. ACQUISITION SOURCE (all-source population)")
    if not distribution.get("available"):
        print("  unavailable")
    else:
        group_labels = data["group_labels"]
        for group, count in sorted(data["acquisition_groups"].items(),
                                   key=lambda kv: -kv[1]):
            print(f"    {group:<20} {str(group_labels.get(group, group)):<24} {count}")

    print()
    print("=" * 72)
    print("Read-only audit complete. No writes were performed.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
