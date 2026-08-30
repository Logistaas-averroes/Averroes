#!/usr/bin/env python3
"""
scripts/report_missing_deal_amounts.py

PR-ADS-155 §6 — which closed-won deals block the revenue total.

Why this exists
---------------
All-Time closed-won revenue is UNAVAILABLE, and it is correctly unavailable: of
the 181 closed-won deals in the canonical ledger, 14 carry no amount in HubSpot,
so the window total is genuinely unknown. The product refuses to publish a total
it cannot compute, and refuses equally to publish the $878,324.80 it CAN compute
under a "total revenue" label, because that figure is the value of 167 deals and
not of 181.

That refusal is correct and it is also, on its own, a dead end for an operator:
a blocked number with nothing to do about it. This command closes that loop. It
names the exact records whose absence blocks the total, with a link to each, so
the fix is a few minutes of data entry in HubSpot rather than an investigation.

    python -m scripts.report_missing_deal_amounts --window all_time
    python -m scripts.report_missing_deal_amounts --window all_time --json

Exit codes:
    0  the total is publishable — no deal in the window is missing an amount
    1  deals are missing amounts (the list is the output)
    2  the canonical ledger could not be read, so nothing is known either way
    3  usage error

Read-only and non-inferential
-----------------------------
No HubSpot call. No write to HubSpot, Google Ads, Mailchimp or the database. No
amount is guessed from the company, the campaign, the account's previous deals,
the associated contacts, or by dividing the known revenue across the unpriced
deals. A missing amount is reported as missing.

No contact names or email addresses are read or printed.
"""

from __future__ import annotations

import argparse
import json
import sys

# Exit codes, named so the CI/runbook meaning is not a bare integer.
EXIT_CLEAN = 0
EXIT_MISSING_AMOUNTS = 1
EXIT_UNAVAILABLE = 2
EXIT_USAGE = 3


def _render(report: dict, disclosure: dict) -> None:
    print("=" * 78)
    print("  CLOSED-WON DEALS WITH NO PROVEN AMOUNT")
    print(f"  window: {report.get('window')}  "
          f"[{report.get('window_start')} → {report.get('window_end')}]")
    print(f"  canonical source: {report.get('source')}   "
          f"ledger as of: {report.get('as_of')}")
    print("=" * 78)

    if not report.get("available"):
        print(f"\n  UNAVAILABLE: {report.get('reason')}")
        if report.get("detail"):
            print(f"  {report['detail']}")
        print("\n  The ledger could not be read, so the number of unpriced deals")
        print("  is UNKNOWN. It is not zero: a refused read carries no population,")
        print("  and reporting 0 here would be counting an empty list.")
        return

    print("\n  REVENUE DISCLOSURE (PR-ADS-155 §5)")
    print(f"    closed-won deals            {disclosure.get('closed_won_deals')}")
    print(f"    with a proven amount        {disclosure.get('revenue_proven_deals')}")
    print(f"    with NO proven amount       {disclosure.get('revenue_unavailable_deals')}")
    known = disclosure.get("known_revenue_usd")
    known_label = disclosure.get("known_revenue_label") or "known revenue"
    print(f"    {known_label}: "
          + (f"${known:,.2f}" if known is not None else "unavailable"))
    total = disclosure.get("total_revenue_usd")
    print("    TOTAL revenue:              "
          + (f"${total:,.2f}" if total is not None else "UNAVAILABLE"))
    if not disclosure.get("total_revenue_publishable"):
        print(f"    reason                      {disclosure.get('unavailable_reason')}")
        for code in disclosure.get("violation_codes") or []:
            print(f"    violation                   {code}")

    if not report["deals"]:
        print("\n  Every closed-won deal in this window has a proven amount.")
        return

    print(f"\n  {report['deal_count']} DEAL(S) TO PRICE IN HUBSPOT")
    print("  " + "-" * 74)
    for deal in report["deals"]:
        print(f"    {deal['deal_id']}  {deal.get('deal_name') or '(unnamed)'}")
        print(f"      closed:   {deal.get('deal_close_date')}   "
              f"stage: {deal.get('deal_stage_label')}")
        print(f"      amount:   {deal.get('amount_status')}   "
              f"currency code: {deal.get('currency_code_status')}"
              f"{'' if not deal.get('currency_code') else ' (' + str(deal['currency_code']) + ')'}")
        print(f"      reason:   {deal.get('reason')}   "
              f"(currency_status={deal.get('currency_status')})")
        if deal.get("hubspot_record_url"):
            print(f"      open:     {deal['hubspot_record_url']}")
        print()

    print("  " + "-" * 74)
    print("  Every amount above is reported as MISSING. None was inferred from the")
    print("  company, the campaign, previous deals, associated contacts, or by")
    print("  dividing the known revenue. Nothing was written to HubSpot.")


def main() -> int:
    from analysis.business_windows import WINDOW_KEYS

    parser = argparse.ArgumentParser(
        description="List closed-won deals with no proven amount (read-only).")
    parser.add_argument("--window", default="all_time",
                        help=f"business window ({'|'.join(WINDOW_KEYS)})")
    parser.add_argument("--json", action="store_true",
                        help="emit the raw canonical report as JSON")
    args = parser.parse_args()

    if args.window not in WINDOW_KEYS:
        print(f"Unknown window '{args.window}'. Valid: {', '.join(WINDOW_KEYS)}",
              file=sys.stderr)
        return EXIT_USAGE

    from services import canonical_revenue_service as canonical_revenue

    base = canonical_revenue.load_won_deals(args.window)
    report = canonical_revenue.missing_amount_deals(base)
    disclosure = canonical_revenue.revenue_disclosure(base)

    if args.json:
        print(json.dumps({"report": report, "revenue_disclosure": disclosure},
                         indent=2, default=str))
    else:
        _render(report, disclosure)

    if not report.get("available"):
        return EXIT_UNAVAILABLE
    return EXIT_CLEAN if report["deal_count"] == 0 else EXIT_MISSING_AMOUNTS


if __name__ == "__main__":
    sys.exit(main())
