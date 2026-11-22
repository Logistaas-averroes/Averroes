"""PR-ADS-171A — backfill Meta Ads campaign-daily spend. Read-only against Meta.

    python -m scripts.backfill_meta_ads_spend --from 2025-01-01
    python -m scripts.backfill_meta_ads_spend --from 2025-01-01 --to 2025-06-30
    python -m scripts.backfill_meta_ads_spend --from 2025-01-01 --dry-run
    python -m scripts.backfill_meta_ads_spend --from 2025-01-01 --resume
    python -m scripts.backfill_meta_ads_spend --from 2025-01-01 --account 944370805110952

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

An account stops at its first failed month; the others still run. Each month opens its own
``sync_batches`` row (``sync_type='backfill'``); no ``runs`` row is written, so
this job is not a monitoring cadence.

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
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true")
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
                              "error": ", ".join(invalid)}, indent=2))
            return 1
    result = meta.run_backfill(args.date_from, args.date_to, account_ids=accounts,
                               dry_run=args.dry_run, resume=args.resume)
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
