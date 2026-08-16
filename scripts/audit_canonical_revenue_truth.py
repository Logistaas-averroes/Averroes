#!/usr/bin/env python3
"""
scripts/audit_canonical_revenue_truth.py

PR-ADS-153E-A §8 — READ-ONLY shadow reconciliation gate for the canonical deal
ledger.

    python -m scripts.audit_canonical_revenue_truth --window current_quarter
    python -m scripts.audit_canonical_revenue_truth --window current_quarter --json
    echo $?      # 0 = reconciled, 1 = validation failed, 2 = usage error

This is a MERGE and PRODUCTION gate, not a report. It exits non-zero whenever an
invariant fails, and `--json` returns the same status — a machine-readable
failure is still a failure.

Every legacy-versus-canonical difference is itemized BY DEAL ID with a reason.
"Totals differ" is not an acceptable output: the whole point of shadow mode is
that PR-ADS-153E-B can only migrate consumers once each moving deal is explained.

Guarantees
----------
  * NO writes of any kind;
  * NO external API calls — every number comes from the local database;
  * NO PII in the output. Contact names and email addresses are never read, and
    a GCLID is reported only as present/absent, because this output goes into
    CI logs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

EXIT_OK = 0
EXIT_VALIDATION_FAILED = 1
EXIT_USAGE = 2

# How many itemized differences to print per category in human-readable mode.
# The `--json` output always carries the complete list — the cap is a terminal
# convenience, never a truncation of the evidence itself.
_PRINT_CAP = 25


def _fmt(value) -> str:
    """Render a value without ever turning an unknown into a zero."""
    if value is None:
        return "Unavailable"
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _section(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def _print_items(label: str, items: list, fields: tuple) -> None:
    if not items:
        print(f"    {label:<44} {'0':>10}")
        return
    unexplained = sum(1 for i in items if i.get("expected") is False)
    suffix = f"   ({unexplained} UNEXPLAINED)" if unexplained else ""
    print(f"    {label:<44} {len(items):>10}{suffix}")
    for item in items[:_PRINT_CAP]:
        detail = "  ".join(f"{f}={_fmt(item.get(f))}" for f in fields)
        mark = " " if item.get("expected") is not False else "!"
        print(f"      {mark}· deal {item.get('deal_id')}  {detail}")
    if len(items) > _PRINT_CAP:
        print(f"      … {len(items) - _PRINT_CAP} more (complete list in --json)")


def _render(report: dict) -> None:
    canonical = report.get("canonical") or {}
    print("=" * 74)
    print("PR-ADS-153E-A — CANONICAL REVENUE TRUTH (READ-ONLY SHADOW GATE)")
    print(f"Business window: {report.get('window')}  "
          f"({report.get('window_start') or 'no lower bound'} → "
          f"{report.get('window_end')})")
    print("=" * 74)

    # ── 1 ───────────────────────────────────────────────────────────────────
    _section("1. CANONICAL DEAL LEDGER (hubspot_deal_ledger)")
    print(f"    {'distinct deals':<44} {_fmt(canonical.get('distinct_deals')):>10}")
    print(f"    {'current closed-won deals':<44} {_fmt(canonical.get('won_deals')):>10}")
    print(f"    {'  ...with a GCLID':<44} {_fmt(canonical.get('won_with_gclid')):>10}")
    print(f"    {'  ...WITHOUT a GCLID':<44} {_fmt(canonical.get('won_without_gclid')):>10}"
          "   ← invisible to the legacy GCLID ledger")
    print(f"    {'won deals with unknown won-state':<44} "
          f"{_fmt(canonical.get('unknown_won_deals')):>10}")
    print(f"    {'won deals with no close date':<44} "
          f"{_fmt(canonical.get('won_without_close_date')):>10}")
    print(f"    {'unknown stage deals':<44} {_fmt(canonical.get('unknown_stage_deals')):>10}")
    print()
    print(f"    {'revenue USD (currency PROVEN only)':<44} "
          f"{_fmt(canonical.get('revenue_usd')):>10}")
    print(f"    {'won deals with proven currency':<44} "
          f"{_fmt(canonical.get('won_currency_proven')):>10}")
    print(f"    {'won deals with currency UNAVAILABLE':<44} "
          f"{_fmt(canonical.get('won_currency_unavailable')):>10}"
          "   ← withheld, never zeroed")
    print(f"    {'raw amount total (mixed currencies)':<44} "
          f"{_fmt(canonical.get('amount_raw_total')):>10}   ← NOT a USD figure")
    print()
    print(f"    {'ambiguous associations':<44} "
          f"{_fmt(canonical.get('ambiguous_associations')):>10}")
    print(f"    {'failed association lookups':<44} "
          f"{_fmt(canonical.get('failed_associations')):>10}")

    # ── 2 ───────────────────────────────────────────────────────────────────
    _section("2. STAGE COVERAGE (open / lost / downgrade / churn are stored)")
    stages = report.get("stage_breakdown") or []
    if not stages:
        print("    No deals in the ledger yet.")
    for row in stages[:20]:
        won = row.get("hs_is_closed_won")
        marker = "WON " if won is True else ("    " if won is False else "?   ")
        print(f"    {marker}{str(row.get('deal_stage_label'))[:44]:<44} "
              f"{_fmt(row.get('deals')):>10}")

    # ── 3 ───────────────────────────────────────────────────────────────────
    _section("3. DEAL-GRAIN RECONCILIATION VS LEGACY LEDGERS")
    for diff in (report.get("legacy_diffs") or []):
        print()
        print(f"  ── {diff.get('ledger')} "
              f"({'available' if diff.get('available') else 'UNAVAILABLE'}) ──")
        print(f"    {'legacy deals in window':<44} "
              f"{_fmt(diff.get('legacy_deal_count')):>10}")
        _print_items("canonical-only (not in legacy)",
                     diff.get("canonical_only") or [],
                     ("has_gclid", "revenue_usd", "reason"))
        _print_items("legacy-only (MISSING from canonical entirely)",
                     diff.get("legacy_only") or [],
                     ("legacy_usd", "legacy_stage_label", "reason"))
        _print_items("won disagreement (held, classified differently)",
                     diff.get("won_disagreement") or [],
                     ("canonical_is_closed_won", "legacy_usd", "reason"))
        _print_items("amount disagreement",
                     diff.get("amount_disagreement") or [],
                     ("canonical_usd", "legacy_usd", "reason"))
        _print_items("duplicate legacy rows for one deal",
                     diff.get("duplicate_legacy_rows") or [], ("rows_held",))

    # ── 4 ───────────────────────────────────────────────────────────────────
    _section("4. SYNC COVERAGE")
    state = report.get("sync_state")
    if not state:
        print("    No deal sync has run yet.")
    else:
        for key in ("bootstrap_status", "last_status", "last_incremental_at",
                    "last_modified_watermark", "deals_seen", "pages_fetched",
                    "association_failures", "last_error"):
            print(f"    {key:<44} {_fmt(state.get(key)):>10}")

    # ── verdict ─────────────────────────────────────────────────────────────
    _section("VERDICT")
    violations = report.get("violations") or []
    if not violations:
        print("  PASS — the canonical ledger is internally consistent and every")
        print("  legacy difference is itemized above with a deal-level reason.")
    else:
        print(f"  FAIL — {len(violations)} invariant violation(s):")
        for v in violations:
            print(f"    ✗ {v}")

    print()
    print("=" * 74)
    print("Shadow mode: no production page reads this ledger yet (PR-ADS-153E-B).")
    print("Read-only audit complete. No writes and no external API calls.")
    print("=" * 74)


def main() -> int:
    from analysis.business_windows import WINDOW_KEYS

    parser = argparse.ArgumentParser(
        description="Read-only PR-ADS-153E-A canonical revenue reconciliation gate")
    parser.add_argument("--window", default="current_quarter",
                        help=f"Business window ({'|'.join(WINDOW_KEYS)})")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable output (still exits non-zero on "
                             "failure)")
    args = parser.parse_args()

    if args.window not in WINDOW_KEYS:
        print(f"Unknown window {args.window!r}. Valid: {', '.join(WINDOW_KEYS)}",
              file=sys.stderr)
        return EXIT_USAGE

    try:
        from db.connection import init_pool
        from services.revenue_reconciliation_service import (
            build_revenue_reconciliation,
        )

        init_pool()
        report = build_revenue_reconciliation(args.window)
    except Exception as exc:  # noqa: BLE001
        # An audit that cannot run is a FAILED audit, never a pass.
        failure = {"ok": False, "window": args.window,
                   "violations": [f"audit could not run: {exc}"]}
        if args.json:
            print(json.dumps(failure, indent=2))
        else:
            print(f"AUDIT FAILED — could not run: {exc}")
        return EXIT_VALIDATION_FAILED

    if not report.get("available"):
        reason = report.get("reason", "unknown")
        failure = {"ok": False, "window": args.window,
                   "violations": [f"reconciliation unavailable: {reason}"]}
        if args.json:
            print(json.dumps(failure, indent=2, default=str))
        else:
            print(f"AUDIT FAILED — reconciliation unavailable: {reason}")
        return EXIT_VALIDATION_FAILED

    exit_code = EXIT_OK if report.get("ok") else EXIT_VALIDATION_FAILED

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return exit_code

    _render(report)
    print(f"exit={exit_code}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
