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


def main() -> int:
    from connectors import meta_ads  # noqa: PLC0415

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
    results = [_account(account_id) for account_id in cfg["account_ids"]]
    good = sum(1 for ok in results if ok)
    print(f"=== {good} of {len(results)} account(s) OK"
          + (f"; {len(cfg['misconfigured_sets'])} credential set(s) not usable"
             if cfg["misconfigured_sets"] else "") + " ===")
    return 0 if good == len(results) and not cfg["misconfigured_sets"] else 1


if __name__ == "__main__":
    sys.exit(main())
