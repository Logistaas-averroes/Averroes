"""PR-ADS-171A — backfill Meta Ads campaign-daily spend. Read-only against Meta.

    python -m scripts.backfill_meta_ads_spend --from 2025-01-01
    python -m scripts.backfill_meta_ads_spend --from 2025-01-01 --to 2025-06-30
    python -m scripts.backfill_meta_ads_spend --from 2025-01-01 --dry-run
    python -m scripts.backfill_meta_ads_spend --from 2025-01-01 --resume
    python -m scripts.backfill_meta_ads_spend --from 2025-01-01 --account 944370805110952
    python -m scripts.backfill_meta_ads_spend --from 2025-01-01 --level adset

Every configured ad account is backfilled, one after another (``--account``
names one, and may be repeated). For each, the range is cut at calendar-month
ends and each month is one chunk: fetched (GET only), reconciled against the
account-level total per day, then replaced in one transaction and recorded
``verified``. ``--to`` defaults to, and is capped at, yesterday in THAT
account's own time zone.

Backfill every account from the SAME first day: a period is published only
when every configured account is covered for all of it, and an account with
no ads in a month is recorded as a verified zero in seconds.

``--dry-run``  reads the account from Meta, prints the plan, writes nothing.
``--resume``   skips a month already recorded ``verified`` with these bounds.

``--accept-zero-restatement``  A re-read that would take a day with verified
               spend to zero is refused (``meta_restatement_implausible``) and
               changes nothing. Pass this ONLY after checking in Meta that the
               day really has no spend. The daily sync has no such switch.

``--level adset``  (PR-ADS-171C) Backfill the AD-SET detail instead: one row
               per ad set per day — spend, impressions, clicks and Meta's own
               lead-related action counts — reconciled per day and campaign to
               the campaign rows ALREADY stored. Run the campaign level (the
               default) for the same range first: a month whose campaign level
               is not verified fails ``meta_adset_campaign_base_unverified``
               and stores nothing. It writes only the two ad-set tables and
               cannot change the campaign rows, their ledger or an account's
               sync state. ``--dry-run``, ``--resume``, ``--account`` and
               ``--to`` mean the same. Without ``--level`` this command does
               exactly what it did before.

An account stops at its first failed month; the others still run.

The backfill writes no ``sync_batches`` row and no ``runs`` row (like the
Google Ads spend and geo backfills): the dataset's freshness and its
proven-through date belong to the daily sync alone, so a backfill can neither
make a failed daily feed look fresh nor move its date. What the backfill did
is in the coverage ledger, month by month. It is not a monitoring cadence.

BEFORE the first backfill, check a month that contains an archived or deleted
campaign with ``python -m scripts.meta_ads_smoke_test --month YYYY-MM``.

Exit code: 0 every month of every account verified and convertible to USD (or
nothing to do); 1 every account failed, or Meta is misconfigured; 2 the
database is not ready or Meta is not configured; 3 ``partial`` — some account
failed, or some USD rates are missing.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date


def _parse(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill Meta Ads campaign-daily spend (read-only).")
    parser.add_argument("--from", dest="date_from", required=True,
                        type=date.fromisoformat, help="first day, YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", default=None,
                        type=date.fromisoformat,
                        help="last day, YYYY-MM-DD (default: account yesterday)")
    parser.add_argument("--account", dest="accounts", action="append",
                        default=None, metavar="ID",
                        help="one configured ad account id (repeatable; "
                             "default: every configured account)")
    parser.add_argument("--level", choices=("campaign", "adset"),
                        default="campaign",
                        help="campaign (default) or adset: the ad-set detail "
                             "of months whose campaign level is verified")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--accept-zero-restatement", action="store_true",
                        help="allow a re-read to take days with verified spend "
                             "to zero (check in Meta first)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse(argv)
    from db.connection import ensure_database_ready  # noqa: PLC0415
    from services import meta_ads_spend_service as meta  # noqa: PLC0415

    if not args.dry_run:
        ready, detail = ensure_database_ready()
        if not ready:
            print(json.dumps({"status": "failed", "reason": "database_unavailable",
                              "error": detail}, indent=2))
            return 2
        from db.schema import init_db  # noqa: PLC0415
        init_db()
    from connectors.meta_ads import parse_account_ids  # noqa: PLC0415
    accounts = None
    if args.accounts:
        accounts, invalid = parse_account_ids(",".join(args.accounts))
        if invalid:
            print(json.dumps({"status": "failed", "reason": "ad_account_id_invalid",
                              "error": f"{len(invalid)} --account value(s) "
                                       "are not an ad account id"}, indent=2))
            return 1
    extra = {"level": "adset"} if args.level == "adset" else {}
    result = meta.run_backfill(args.date_from, args.date_to, account_ids=accounts,
                               dry_run=args.dry_run, resume=args.resume,
                               accept_zero_restatement=args.accept_zero_restatement,
                               **extra)
    print(json.dumps(result, indent=2, default=str))
    status = result.get("status")
    if status == "success":
        return 0
    if status == "partial":
        return 3
    if status == "skipped":
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
