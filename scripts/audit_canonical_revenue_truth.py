#!/usr/bin/env python3
"""
scripts/audit_canonical_revenue_truth.py

PR-ADS-153E-A §8 / PR-ADS-153E-A2 §5 — READ-ONLY reconciliation and COVERAGE
gate for the canonical deal ledger.

    python -m scripts.audit_canonical_revenue_truth --window current_quarter
    python -m scripts.audit_canonical_revenue_truth --all-windows --json
    echo $?      # 0 = reconciled, 1 = validation failed, 2 = usage error

This is a MERGE and PRODUCTION gate, not a report. It exits non-zero whenever an
invariant fails, and `--json` returns the same status — a machine-readable
failure is still a failure.

`--all-windows` is the production cutover gate: ONE failing or unavailable
window fails the whole command. A green `current_quarter` is not permission to
migrate a page that renders "all time".

Two things must hold, and the second is what PR-ADS-153E-A2 added:

  1. every legacy-versus-canonical difference is itemized BY DEAL ID with a
     reason — "totals differ" is not an acceptable output;
  2. the ledger's HISTORICAL COVERAGE is proven — a complete bootstrap with
     ordered timestamps, and a successful incremental sync after it. Reconciling
     what the ledger holds says nothing about what it is missing.

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
    stages = report.get("stage_breakdown")
    if stages is None or not report.get("stage_breakdown_available", True):
        # "Could not read" is not "there is nothing there".
        print("    UNAVAILABLE — stage coverage could not be read.")
        stages = []
    elif not stages:
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
        if not diff.get("available"):
            # No comparison was performed, so nothing below would mean anything.
            # Printing zeros here would read as "reconciled".
            print("    Could not be read"
                  + (f": {diff.get('unavailable_detail')}"
                     if diff.get("unavailable_detail") else "")
                  + " — no comparison performed.")
            continue
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
    _section("4. SYNC COVERAGE (the PR-ADS-153E-A2 cutover interlock)")
    state = report.get("sync_state")
    if not state:
        print("    No deal sync has run yet — coverage cannot be verified.")
    else:
        for key in ("bootstrap_status", "bootstrap_started_at",
                    "bootstrap_completed_at", "last_incremental_at",
                    "last_sync_mode", "last_status",
                    "last_modified_watermark", "deals_seen",
                    "pages_fetched", "association_failures", "last_error"):
            print(f"    {key:<44} {_fmt(state.get(key)):>26}")
        print()
        print("    A complete bootstrap AND a later successful incremental are")
        print("    both required. Reconciling what the ledger holds says")
        print("    nothing about what it is missing.")
        print()
        print("    `last_sync_mode` says which mode wrote `last_status`. The")
        print("    two are shared, so without it a bootstrap rerun's success")
        print("    could validate an incremental timestamp it never wrote.")

    # ── verdict ─────────────────────────────────────────────────────────────
    _section("VERDICT")
    details = report.get("violation_details") or [
        {"code": "", "message": m} for m in (report.get("violations") or [])]
    if not details:
        print("  PASS — history is proven complete, an incremental has run on")
        print("  top of it, the ledger is internally consistent, and every")
        print("  legacy difference is itemized with a deal-level reason.")
    else:
        print(f"  FAIL — {len(details)} invariant violation(s):")
        for v in details:
            code = f"[{v['code']}] " if v.get("code") else ""
            print(f"    ✗ {code}{v['message']}")

    print()
    print("=" * 74)
    print("Shadow mode: no production page reads this ledger yet (PR-ADS-153E-B).")
    print("Read-only audit complete. No writes and no external API calls.")
    print("=" * 74)


# The windows a production cutover must ALL pass. Named here rather than taken
# from WINDOW_KEYS at random so a window added later is a deliberate decision
# about what the gate covers, not a silent widening of it.
GATE_WINDOWS = ("current_quarter", "last_quarter", "last_6_months", "ytd",
                "all_time")


def _audit_window(window: str) -> dict:
    """Run one window. An audit that cannot RUN is a failed audit, never a pass.

    The import is INSIDE the try on purpose: a broken dependency raises at
    import time, and letting that escape would crash the process instead of
    reporting a failed window — turning a gate failure into an absence of a
    result, and taking the whole --all-windows aggregation with it.
    """
    try:
        from services.revenue_reconciliation_service import (  # noqa: PLC0415
            build_revenue_reconciliation,
        )

        report = build_revenue_reconciliation(window)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "available": False, "window": window,
                "violations": [f"audit could not run: {exc}"],
                "violation_codes": ["audit_could_not_run"]}

    if not report.get("available"):
        reason = report.get("reason", "unknown")
        return {"ok": False, "available": False, "window": window,
                "violations": [f"reconciliation unavailable: {reason}"],
                "violation_codes": ["reconciliation_unavailable"]}
    return report


def main() -> int:
    from analysis.business_windows import WINDOW_KEYS

    parser = argparse.ArgumentParser(
        description="Read-only canonical revenue reconciliation and coverage "
                    "gate (PR-ADS-153E-A / PR-ADS-153E-A2)")
    parser.add_argument("--window",
                        help=f"One business window ({'|'.join(WINDOW_KEYS)}). "
                             "Defaults to current_quarter.")
    parser.add_argument("--all-windows", action="store_true",
                        help="Run every gate window ("
                             f"{', '.join(GATE_WINDOWS)}). ONE failing or "
                             "unavailable window fails the whole command — "
                             "this is the production cutover gate.")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable output (still exits non-zero on "
                             "failure)")
    args = parser.parse_args()

    if args.window and args.all_windows:
        print("--window and --all-windows are mutually exclusive.",
              file=sys.stderr)
        return EXIT_USAGE

    windows = list(GATE_WINDOWS) if args.all_windows else [
        args.window or "current_quarter"]

    unknown = [w for w in windows if w not in WINDOW_KEYS]
    if unknown:
        print(f"Unknown window {unknown[0]!r}. Valid: {', '.join(WINDOW_KEYS)}",
              file=sys.stderr)
        return EXIT_USAGE

    try:
        from db.connection import init_pool

        init_pool()
    except Exception as exc:  # noqa: BLE001
        failure = {"ok": False, "violations": [f"audit could not run: {exc}"]}
        if args.json:
            print(json.dumps(failure, indent=2))
        else:
            print(f"AUDIT FAILED — could not run: {exc}")
        return EXIT_VALIDATION_FAILED

    reports = [_audit_window(w) for w in windows]
    # Aggregate: EVERY window must pass. A single green window is not permission
    # to migrate a page that renders "all time".
    overall_ok = all(bool(r.get("ok")) for r in reports)
    exit_code = EXIT_OK if overall_ok else EXIT_VALIDATION_FAILED

    if args.json:
        if args.all_windows:
            print(json.dumps({
                "ok": overall_ok,
                "windows": windows,
                "failing_windows": [r["window"] for r in reports
                                    if not r.get("ok")],
                "results": {r["window"]: r for r in reports},
            }, indent=2, default=str))
        else:
            print(json.dumps(reports[0], indent=2, default=str))
        return exit_code

    for report in reports:
        if not report.get("available"):
            print(f"AUDIT FAILED [{report.get('window')}] — "
                  + "; ".join(report.get("violations") or ["unavailable"]))
            continue
        _render(report)

    if args.all_windows:
        _section("ALL-WINDOW GATE")
        for report in reports:
            mark = "PASS" if report.get("ok") else "FAIL"
            print(f"    {report.get('window'):<20} {mark}")
        failing = [r["window"] for r in reports if not r.get("ok")]
        print()
        if failing:
            print("  FAILING WINDOWS: " + ", ".join(failing))
        print(f"  aggregate ok = {overall_ok}")

    print(f"exit={exit_code}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
