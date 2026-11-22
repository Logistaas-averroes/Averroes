"""PR-ADS-171C — the Meta Ads AD-SET sync: fetch, reconcile to campaigns, replace.

Each Meta ad set has its own budget, targeting and countries, so the owner
judges ad sets separately. This module stores one row per account, ad set and
account-local day — spend, impressions, clicks and Meta's own attributed
lead-related action counts — as a DETAIL of the campaign-level rows that
PR-ADS-171A stores.

The campaign level stays the published base
--------------------------------------------
* An ad-set chunk is ``verified`` only when, for every day and every campaign
  of the range, the ad-set rows sum to the STORED campaign-level row within
  one minor unit (:func:`reconcile_adsets_to_campaigns`), and every day of the
  range is inside a ``verified`` campaign chunk. Otherwise the chunk fails and
  stores nothing.
* Nothing here writes a campaign table, the campaign ledger
  (``meta_ads_spend_coverage``) or ``meta_ads_account_sync_state``. A failed
  ad-set read leaves campaign truth exactly as it was and records a ``failed``
  row in the ad-set ledger (``meta_ads_adset_coverage``) — which never
  replaces a ``verified`` ad-set row.

Leads
-----
``meta_reported_lead_actions`` are Meta's own counts, under each ad set's attribution
setting. They are not contacts, not CRM leads, not MQLs and not SQLs, and are
never compared arithmetically with a HubSpot count. The action types overlap,
so they are stored one per type and never added together. A row without an
``actions`` key is unknown, not zero. See docs/53 §4.

Read-only against Meta (GET). Writes only the two local ad-set tables.
"""
from __future__ import annotations

import logging
from datetime import date

log = logging.getLogger(__name__)

#: The campaign-level rows this range must be reconciled against are not
#: verified for every day of it. The ad-set read has no base.
REASON_BASE_UNVERIFIED = "meta_adset_campaign_base_unverified"
#: The ad-set rows of some day and campaign do not sum to the campaign row.
REASON_ADSET_RECONCILIATION = "meta_adset_reconciliation_mismatch"


def reconcile_adsets_to_campaigns(rows: list, campaign_rows: list,
                                  tolerance_micros: int) -> dict:
    """Per day AND campaign: do the ad-set rows sum to the campaign-level row?

    Pure. Keyed by ``(spend_date, campaign_id)`` — ids only. A key present on
    one side only is compared with zero on the other, so an ad set Meta left
    out of the ad-set read (its spend is in the campaign row) and an ad-set
    row under a campaign that has no campaign-level row both fail.

    Equal per campaign per day implies equal per day, and so equal to the
    account-level total the campaign rows were reconciled to.

    ``{reconciled, mismatches: [{spend_date, campaign_id, adset_micros,
    campaign_micros, delta_micros}], keys_compared}``.
    """
    by_adset: dict = {}
    for r in rows or []:
        key = (str(r["spend_date"]), str(r["campaign_id"]))
        by_adset[key] = by_adset.get(key, 0) + int(r["cost_micros"])
    by_campaign: dict = {}
    for r in campaign_rows or []:
        key = (str(r["spend_date"]), str(r["campaign_id"]))
        by_campaign[key] = by_campaign.get(key, 0) + int(r["cost_micros"])
    mismatches = []
    for key in sorted(set(by_adset) | set(by_campaign)):
        adset, campaign = by_adset.get(key, 0), by_campaign.get(key, 0)
        if abs(adset - campaign) > int(tolerance_micros):
            mismatches.append({"spend_date": key[0], "campaign_id": key[1],
                               "adset_micros": adset,
                               "campaign_micros": campaign,
                               "delta_micros": adset - campaign})
    return {"reconciled": not mismatches, "mismatches": mismatches,
            "keys_compared": len(set(by_adset) | set(by_campaign))}


def lead_action_totals(rows: list) -> dict:
    """``{action_type: count}`` over the rows that carried ``actions``, one
    total PER TYPE. The types are never added to each other."""
    totals: dict = {}
    for r in rows or []:
        for kind, count in (r.get("meta_reported_lead_actions") or {}).items():
            totals[kind] = totals.get(kind, 0) + int(count)
    return dict(sorted(totals.items()))


def sync_adset_chunk(account: dict, start: date, end: date, *,
                     sync_run_id: str | None = None,
                     accept_zero_restatement: bool = False) -> dict:
    """Fetch, reconcile and replace the ad-set rows of ``[start, end]``.

    Never raises. ``status`` is ``success`` (verified) or ``failed`` (nothing
    replaced; a ``failed`` row recorded in the AD-SET ledger only). There is
    no ``partial``: USD conversion is the campaign level's question — the
    ad-set rows are amounts on the same days in the same currency.

    ``account`` needs ``account_id``, ``currency_code``, ``time_zone_name``.
    """
    from connectors import meta_ads  # noqa: PLC0415
    from db import meta_ads_repository as repo  # noqa: PLC0415
    import db.writers as db_writers  # noqa: PLC0415
    from services import meta_ads_spend_service as meta  # noqa: PLC0415
    from services.google_ads_spend_service import analyze_coverage  # noqa: PLC0415

    account_id = account["account_id"]
    currency = account.get("currency_code")
    zone = account.get("time_zone_name")
    stats: dict = {"pages_read": 0}
    base = {"chunk_start": start.isoformat(), "chunk_end": end.isoformat(),
            "account_id": account_id, "currency": currency,
            "account_time_zone": zone, "level": "adset",
            "external_writes_performed": False}
    try:
        rows: list = []
        for page in meta_ads.iter_adset_daily(
                account_id, start.isoformat(), end.isoformat(), stats):
            rows.extend(page)
            if len(rows) > meta.MAX_ROWS_PER_CHUNK:
                raise meta.MetaChunkFailed(
                    meta.REASON_ROW_LIMIT,
                    f"more than {meta.MAX_ROWS_PER_CHUNK} ad-set-day rows in "
                    "one chunk; use a shorter range")
        for r in rows:
            if r.get("currency_code") and currency and r["currency_code"] != currency:
                raise meta.MetaChunkFailed(
                    meta.REASON_MALFORMED,
                    f"an ad-set row is in {r['currency_code']}; the account "
                    f"on record is in {currency}")

        # The base: the campaign-level rows on record, and proof that every
        # day of this range is inside a verified campaign chunk.
        stored = repo.fetch_meta_campaign_day_totals(account_id, start, end)
        if not stored.get("available"):
            raise meta.MetaChunkFailed(
                meta.REASON_PERSISTENCE,
                "the stored campaign-level rows could not be read")
        if analyze_coverage(start, end, stored.get("chunks") or []).get(
                "complete") is not True:
            raise meta.MetaChunkFailed(
                REASON_BASE_UNVERIFIED,
                f"campaign-level spend is not verified for every day of "
                f"{start.isoformat()}..{end.isoformat()}; sync or backfill the "
                "campaign level first")
        verdict = reconcile_adsets_to_campaigns(
            rows, stored["rows"], meta.reconciliation_tolerance_micros(currency))
        if not verdict["reconciled"]:
            first = verdict["mismatches"][0]
            raise meta.MetaChunkFailed(
                REASON_ADSET_RECONCILIATION,
                f"{len(verdict['mismatches'])} campaign-day(s) do not "
                f"reconcile; first {first['spend_date']} campaign "
                f"{first['campaign_id']}: ad-set rows {first['adset_micros']} "
                f"micros, campaign row {first['campaign_micros']} micros")

        written = db_writers.replace_meta_adset_chunk(
            account_id, start, end, rows,
            currency_code=currency, account_time_zone=zone,
            pages_read=stats["pages_read"], http_status=stats.get("http_status"),
            source_query_version=meta_ads.ADSET_SOURCE_QUERY_VERSION,
            sync_run_id=sync_run_id,
            allow_zero_restatement=bool(accept_zero_restatement))
    except Exception as exc:  # noqa: BLE001
        failure = meta.describe_failure(exc)
        # The AD-SET ledger only. Campaign coverage is not touched.
        recorded = db_writers.record_meta_adset_coverage_failure(
            account_id, start, end, error_code=failure["reason"],
            error_message=failure["error"],
            http_status=failure["http_status"] or stats.get("http_status"),
            pages_read=stats.get("pages_read"), account_time_zone=zone,
            sync_run_id=sync_run_id)
        log.warning("[meta_ads] ad-set chunk %s..%s failed: %s", start, end,
                    failure["reason"])
        return {**base, "status": "failed", "coverage_status": "failed",
                "failed_coverage_recorded": bool(recorded),
                "rows_written": 0, "pages_read": stats.get("pages_read"),
                **failure}

    return {**base, "status": "success", "reason": None, "error": None,
            "coverage_status": "verified",
            "rows_written": written["written"],
            "rows_deleted": written["deleted"],
            "cost_micros": written["cost_micros_total"],
            "verified_zero": written["written"] == 0,
            "adsets": len({r["adset_id"] for r in rows}),
            "pages_read": stats["pages_read"],
            "campaign_days_reconciled": verdict["keys_compared"],
            "days_restated": written["days_restated"],
            "names_preserved": written["names_preserved"],
            "rows_without_actions": written["rows_without_actions"],
            # One total per action type. Overlapping; never summed.
            "meta_reported_lead_action_counts": lead_action_totals(rows)}


def _account_text(entry: dict) -> str:
    return (f"account {entry['account_id']} ad sets: "
            f"{entry.get('error') or entry.get('reason')}")


def aggregate(accounts: list) -> dict:
    """The ad-set step's verdict over per-account results. Pure.

    ``success`` when every account's ad sets were verified; ``failed`` when
    none were; ``partial`` otherwise — never folded into either neighbour.
    """
    statuses = [a["status"] for a in accounts]
    if statuses and all(s == "success" for s in statuses):
        status = "success"
    elif not statuses or all(s == "failed" for s in statuses):
        status = "failed"
    else:
        status = "partial"
    bad = [a for a in accounts if a["status"] != "success"]
    verified_ends = [a["chunk_end"] for a in accounts
                     if a.get("coverage_status") == "verified"]
    return {
        "status": status,
        "reason": bad[0].get("reason") if bad else None,
        "error": "; ".join(_account_text(a) for a in bad) or None,
        "errors": [_account_text(a) for a in bad],
        "accounts": accounts,
        "accounts_synced": len(accounts),
        "rows_written": sum(int(a.get("rows_written") or 0) for a in accounts),
        "last_source_date": (min(verified_ends)
                             if verified_ends and len(verified_ends) == len(accounts)
                             else None),
        "external_writes_performed": False,
    }


def sync_after_campaigns(campaign_accounts: list, *,
                         sync_run_id: str | None = None) -> dict:
    """The daily ad-set pass, over the SAME window each account's campaign
    level was just synced for. Never raises.

    ``campaign_accounts`` is the campaign step's per-account results. An
    account whose campaign chunk was not verified in this run has no base:
    its ad sets are not requested, and the range is recorded ``failed`` in
    the ad-set ledger (when the range is known). Its campaign rows, campaign
    ledger and sync state are whatever the campaign step left.
    """
    import db.writers as db_writers  # noqa: PLC0415

    entries = []
    for c in campaign_accounts or []:
        account_id = c["account_id"]
        start, end = c.get("chunk_start"), c.get("chunk_end")
        if c.get("coverage_status") != "verified" or not start or not end:
            text = ("campaign-level spend was not verified in this run"
                    + (f" ({c['reason']})" if c.get("reason") else "")
                    + "; ad sets were not read")
            recorded = bool(start and end) and \
                db_writers.record_meta_adset_coverage_failure(
                    account_id, start, end, error_code=REASON_BASE_UNVERIFIED,
                    error_message=text,
                    account_time_zone=c.get("account_time_zone"),
                    sync_run_id=sync_run_id)
            entries.append({
                "account_id": account_id, "status": "failed", "level": "adset",
                "coverage_status": "failed" if start and end else None,
                "failed_coverage_recorded": bool(recorded),
                "chunk_start": start, "chunk_end": end,
                "account_time_zone": c.get("account_time_zone"),
                "rows_written": 0, "reason": REASON_BASE_UNVERIFIED,
                "error": text, "http_status": None, "error_code": None,
                "external_writes_performed": False})
            continue
        entries.append(sync_adset_chunk(
            {"account_id": account_id, "currency_code": c.get("currency"),
             "time_zone_name": c.get("account_time_zone")},
            date.fromisoformat(start), date.fromisoformat(end),
            sync_run_id=sync_run_id))
    return aggregate(entries)


def window_span(campaign_accounts: list) -> tuple:
    """``(earliest start, latest end)`` over the accounts' windows."""
    starts = [date.fromisoformat(c["chunk_start"]) for c in campaign_accounts or []
              if c.get("chunk_start")]
    ends = [date.fromisoformat(c["chunk_end"]) for c in campaign_accounts or []
            if c.get("chunk_end")]
    return (min(starts) if starts else None, max(ends) if ends else None)


def native_lead_action_type() -> str | None:
    """``meta_ads.native_lead_action_type`` (config/thresholds.yaml), or None.

    The ONE Insights ``action_type`` that counts native lead-form submissions
    for these accounts. It is null: Meta's reference does not name it, so no
    type is published as "leads" until the owner's
    ``meta_ads_smoke_test --adsets`` output shows which types these accounts
    emit and the choice is confirmed (docs/53 §4). No default is assumed.
    """
    from services import meta_ads_spend_service as meta  # noqa: PLC0415
    value = (meta._config().get("meta_ads") or {}).get("native_lead_action_type")
    value = str(value).strip() if value is not None else ""
    return value or None
