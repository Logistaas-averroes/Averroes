"""PR-ADS-171A — Meta Ads smoke test. Read-only (GET). No database.

    python -m scripts.meta_ads_smoke_test

For EVERY configured ad account — across every credential set — it makes a few
GET requests with that account's own token and prints what Meta answered:

* the account: id, name, currency, time zone, status;
* how many of its campaigns are active right now;
* its spend over the last 30 complete days (in its own time zone);
* yesterday's spend twice — the sum of the campaign rows and the account-level
  total — and whether the two agree.

So you can see at a glance which account has the ads. It writes nothing
anywhere and never prints an access token.

    python -m scripts.meta_ads_smoke_test --month 2026-03

With ``--month`` it instead compares, for every day of that month and for each
account, the sum of the campaign rows with the account-level total — the same
check the sync makes before it stores anything. Run it for a month that
contains an ARCHIVED or DELETED campaign before the first backfill: if the
campaign-level read leaves such a campaign out, that month shows a MISMATCH
here (and would fail to backfill), and nothing has been written.

Exit code: 0 every account was read and reconciles; 1 Meta refused a request
for some account, or two totals differ, or a credential set is incomplete;
2 Meta is not configured.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from decimal import Decimal

#: Meta's account_status numbers, as its documentation lists them. Printed
#: beside the raw number, which is what Meta actually returned.
ACCOUNT_STATUS = {1: "ACTIVE", 2: "DISABLED", 3: "UNSETTLED",
                  7: "PENDING_RISK_REVIEW", 8: "PENDING_SETTLEMENT",
                  9: "IN_GRACE_PERIOD", 100: "PENDING_CLOSURE", 101: "CLOSED",
                  201: "ANY_ACTIVE", 202: "ANY_CLOSED"}


def _amount(micros: int) -> str:
    return f"{(Decimal(micros) / Decimal(1_000_000)):.2f}"


def _account(account_id: str) -> bool:
    from connectors import meta_ads  # noqa: PLC0415
    from services import meta_ads_spend_service as meta  # noqa: PLC0415

    print(f"--- Ad account {account_id} ---")
    try:
        account = meta_ads.fetch_account(account_id)
        currency = account["currency_code"]
        status = account["account_status"]
        print(f"Name             : {account['account_name']}")
        print(f"Currency         : {currency}")
        print(f"Time zone        : {account['time_zone_name']}")
        print(f"Status           : {ACCOUNT_STATUS.get(status, 'UNKNOWN')} ({status})")
        day = meta.account_local_yesterday(account["time_zone_name"])
        if day is None:
            print("FAILED           : the account time zone is not a known zone")
            return False
        print(f"Active campaigns : {meta_ads.count_active_campaigns(account_id)}")
        first = day - timedelta(days=29)
        month = meta_ads.fetch_account_daily_spend(
            account_id, first.isoformat(), day.isoformat())
        print(f"Last 30 days     : {_amount(sum(r['cost_micros'] for r in month))} "
              f"{currency} on {len(month)} day(s) with spend "
              f"({first.isoformat()} to {day.isoformat()})")
        stats: dict = {}
        rows = [r for page in meta_ads.iter_campaign_daily_spend(
            account_id, day.isoformat(), day.isoformat(), stats) for r in page]
        totals = [r for r in month if r["spend_date"] == day.isoformat()]
    except meta_ads.MetaAdsError as exc:
        # The connector's own text: HTTP status and Meta's error fields only.
        print(f"FAILED           : {exc.reason}: {exc}")
        return False
    verdict = meta.reconcile_days(
        rows, totals, meta.reconciliation_tolerance_micros(currency))
    print(f"Yesterday        : {day.isoformat()} ({account['time_zone_name']})")
    print(f"Campaign rows    : {len(rows)} in {stats.get('pages_read')} page(s)")
    print(f"Campaign sum     : {_amount(sum(r['cost_micros'] for r in rows))} {currency}")
    print(f"Account total    : {_amount(sum(r['cost_micros'] for r in totals))} {currency}")
    priced = currency == "USD" or currency in meta.fx_provider_currencies()
    print("USD conversion   : "
          + ("not needed (USD account)" if currency == "USD"
             else "available" if priced
             else f"NOT available — {currency} is not priced by the FX provider"))
    if not verdict["reconciled"]:
        print("RESULT           : MISMATCH — the campaign rows do not sum to the "
              "account total")
        return False
    print("RESULT           : OK — the campaign rows sum to the account total")
    return True


def _month(account_id: str, first, last) -> bool:
    """Campaign rows against the account total for every day of a month."""
    from connectors import meta_ads  # noqa: PLC0415
    from services import meta_ads_spend_service as meta  # noqa: PLC0415

    print(f"--- Ad account {account_id} ---")
    try:
        account = meta_ads.fetch_account(account_id)
        currency = account["currency_code"]
        yesterday = meta.account_local_yesterday(account["time_zone_name"])
        if yesterday is None:
            print("FAILED           : the account time zone is not a known zone")
            return False
        end = min(last, yesterday)
        print(f"Name             : {account['account_name']}")
        print(f"Range            : {first.isoformat()} to {end.isoformat()} "
              f"({account['time_zone_name']})")
        if end < first:
            print("RESULT           : nothing to check — the month has not started "
                  "for this account")
            return True
        stats: dict = {}
        rows = [r for page in meta_ads.iter_campaign_daily_spend(
            account_id, first.isoformat(), end.isoformat(), stats) for r in page]
        totals = meta_ads.fetch_account_daily_spend(
            account_id, first.isoformat(), end.isoformat())
    except meta_ads.MetaAdsError as exc:
        print(f"FAILED           : {exc.reason}: {exc}")
        return False
    verdict = meta.reconcile_days(
        rows, totals, meta.reconciliation_tolerance_micros(currency))
    campaigns = len({r["campaign_id"] for r in rows})
    print(f"Campaign rows    : {len(rows)} rows, {campaigns} campaign(s), "
          f"{stats.get('pages_read')} page(s)")
    print(f"Campaign sum     : {_amount(sum(r['cost_micros'] for r in rows))} {currency}")
    print(f"Account total    : {_amount(sum(r['cost_micros'] for r in totals))} {currency}")
    print(f"Days compared    : {verdict['days_compared']}")
    for miss in verdict["mismatches"][:10]:
        print(f"  MISMATCH {miss['spend_date']}: campaign rows "
              f"{_amount(miss['campaign_micros'])}, account total "
              f"{_amount(miss['account_micros'])} {currency}")
    if not verdict["reconciled"]:
        print(f"RESULT           : MISMATCH on {len(verdict['mismatches'])} day(s) — "
              "the campaign rows do not sum to the account total; do not backfill "
              "this month")
        return False
    print("RESULT           : OK — every day's campaign rows sum to the account total")
    return True


def main(argv=None) -> int:
    import argparse  # noqa: PLC0415
    from datetime import date  # noqa: PLC0415
    from connectors import meta_ads  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="Meta Ads smoke test (read-only).")
    parser.add_argument("--month", default=None, metavar="YYYY-MM",
                        help="compare campaign rows with the account total for "
                             "every day of this month, per account")
    args = parser.parse_args(argv)
    month = None
    if args.month:
        try:
            first = date.fromisoformat(f"{args.month}-01")
        except ValueError:
            print(f"NOT A MONTH      : {args.month!r} (use YYYY-MM)")
            return 2
        last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        month = (first, last)

    cfg = meta_ads.config_status()
    print(f"API version      : {cfg['api_version']} (from {cfg['api_version_source']})")
    for entry in cfg["sets"]:
        print(f"Credential set {entry['set']} : token "
              f"{'set' if entry['has_access_token'] else 'MISSING'}; accounts "
              f"{', '.join(entry['account_ids']) or 'MISSING'}; app secret proof "
              f"{'sent' if entry['has_app_secret'] else 'not sent'}"
              + (f"; NOT USABLE ({entry['reason']})" if entry["reason"] else ""))
    if not cfg["configured"]:
        why = cfg["reason"] or "META_ADS_ACCESS_TOKEN and META_AD_ACCOUNT_ID are not set"
        if cfg["duplicate_account_ids"]:
            why += f": {', '.join(cfg['duplicate_account_ids'])}"
        print(f"NOT CONFIGURED   : {why}")
        return 2
    results = [(_month(account_id, *month) if month else _account(account_id))
               for account_id in cfg["account_ids"]]
    good = sum(1 for ok in results if ok)
    print(f"=== {good} of {len(results)} account(s) OK"
          + (f"; {len(cfg['misconfigured_sets'])} credential set(s) not usable"
             if cfg["misconfigured_sets"] else "") + " ===")
    return 0 if good == len(results) and not cfg["misconfigured_sets"] else 1


if __name__ == "__main__":
    sys.exit(main())
