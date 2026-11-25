"""PR-ADS-177B — the "Meta Ads — ad sets" table on the Campaigns page.

Owner decision 2026-11-24 (4): Meta ad sets go in a SEPARATE table beside the
Google Ads campaigns. Each ad set is a row treated as its own campaign. The
table has its own totals, is never summed with Google, and its outcome cells
(SQLs, deals, revenue) are marked not linked yet.

This module is a thin, read-only page wrapper over
``meta_ads_adset_truth_service.build_meta_adset_breakdown_for_days`` (PR-ADS-171C)
for one business window's SETTLED days — the days the Campaigns page reads
(``canonical_contract.resolve_settled_window``). It decides nothing about
coverage: which accounts the range needs, through which day in EACH account's
own time zone, whether every account's ad-set ledger is verified and settled,
and whether the ad-set rows still add up to the campaign rows are the truth
service's verdicts, republished as they are. Withheld means absent: ``rows``
and ``totals`` are empty / None unless ``status`` is ``published``.

What the table carries, per ad set (joined on ids, never on names):

* spend in the account's own currency and in USD (the truth service's FX
  rule: a day with no rate leaves USD unavailable, never converted at another
  day's rate);
* impressions and clicks;
* ``meta_reported_leads`` — Meta's own attributed count, labelled as such. It
  is not a contact, a CRM lead or an SQL and is never compared with, added to
  or divided by a HubSpot count;
* ``hubspot_outcomes`` — SQLs, closed-won deals and revenue — each
  ``not_linked`` with ``meta_adset_not_linked_to_hubspot``: no Meta ad set is
  joined to a HubSpot contact or deal yet.

Totals are the table's own (Σ ad-set rows), published only when every row's
figure is. They are never added to a Google Ads figure, KPI or ratio, and no
ratio over Meta spend is computed here.

Read-only: SELECTs on the local database. No call to Meta, Google Ads or
HubSpot.
"""
from __future__ import annotations

from datetime import date, datetime

from services import canonical_contract

STATUS_PUBLISHED = "published"
STATUS_UNAVAILABLE = "unavailable"
STATUS_NOT_LINKED = "not_linked"

SCOPE = "meta_ads_adsets"
#: Never summed with this: the Google Ads campaign table and every Google KPI.
NEVER_SUMMED_WITH = "google_ads_campaigns"
REASON_NOT_LINKED = "meta_adset_not_linked_to_hubspot"
NOT_LINKED_DETAIL = (
    "No Meta ad set is joined to a HubSpot contact or deal yet, so its SQLs, "
    "closed-won deals and revenue are not linked.")
REASON_MIXED_CURRENCIES = "meta_accounts_use_different_currencies"
REASON_ROW_FIGURE_UNAVAILABLE = "meta_adset_row_figure_unavailable"
REASON_ATTRIBUTION_DIFFERS = "meta_attribution_settings_differ"
#: The page's HubSpot outcome columns (SQLs, closed-won deals, won revenue),
#: each not linked: this module publishes no HubSpot figure at all.
OUTCOME_KEYS = ("hubspot_sql_contacts", "hubspot_closed_won_deals", "hubspot_won_revenue")


def _metric(value, reason=None, **extra) -> dict:
    published = value is not None
    return {"value": value, "status": STATUS_PUBLISHED if published else STATUS_UNAVAILABLE,
            "reason": None if published else reason, **extra}


def not_linked_outcomes() -> dict:
    """The outcome cells of one ad set (or the totals): not linked yet."""
    return {k: {"value": None, "status": STATUS_NOT_LINKED, "reason": REASON_NOT_LINKED}
            for k in OUTCOME_KEYS}


def _sum(rows: list, key: str, *, decimals: int | None = None) -> dict:
    """Σ of one figure over the rows, or unavailable naming the first row
    that does not publish it — never a partial sum."""
    total = 0
    for r in rows:
        m = r.get(key) or {}
        if m.get("status") != STATUS_PUBLISHED or m.get("value") is None:
            return _metric(None, m.get("reason") or REASON_ROW_FIGURE_UNAVAILABLE,
                           blocked_by_adset=r.get("adset_id"))
        total += m["value"]
    if decimals is not None:
        total = round(total, decimals)
    return _metric(total)


def table_totals(rows: list) -> dict:
    """Pure. The Meta table's own totals over its published rows."""
    currencies = sorted({(r.get("spend") or {}).get("currency") or "" for r in rows})
    if len(currencies) > 1:
        native = _metric(None, REASON_MIXED_CURRENCIES, currency=None)
    else:
        native = {**_sum(rows, "spend", decimals=2),
                  "currency": (currencies[0] or None) if currencies else None}
    settings = sorted({s for r in rows for s in r.get("attribution_settings") or []})
    leads = _sum(rows, "meta_reported_leads")
    if leads["status"] == STATUS_PUBLISHED and len(settings) > 1:
        # Counts under different attribution settings are not one count.
        leads = _metric(None, REASON_ATTRIBUTION_DIFFERS)
    return {
        "adsets": len(rows),
        "spend": native,
        "spend_usd": {**_sum(rows, "spend_usd", decimals=2), "currency": "USD"},
        "impressions": _sum(rows, "impressions"),
        "clicks": _sum(rows, "clicks"),
        "meta_reported_leads": leads,
        "hubspot_outcomes": not_linked_outcomes(),
        "never_summed_with": NEVER_SUMMED_WITH,
    }


def _row(r: dict) -> dict:
    return {
        "account_id": r.get("account_id"),
        "account_name": r.get("account_name"),
        "time_zone": r.get("time_zone"),
        "campaign_id": r.get("campaign_id"),
        "campaign_name": r.get("campaign_name"),
        "adset_id": r.get("adset_id"),
        "adset_name": r.get("adset_name"),
        "first_day": r.get("first_day"),
        "last_day": r.get("last_day"),
        "spend": r.get("spend"),
        "spend_usd": r.get("spend_usd"),
        "impressions": r.get("impressions"),
        "clicks": r.get("clicks"),
        "meta_reported_leads": r.get("meta_reported_leads"),
        "attribution_settings": r.get("attribution_settings") or [],
        "hubspot_outcomes": not_linked_outcomes(),
    }


def day_basis_disclosure(accounts) -> str:
    """PR-ADS-177C (M7) — what a "day" is in this table, ALWAYS stated.

    The truth reads each Meta ad account on its own calendar days, in its own
    time zone (``docs/55`` §1). The Google Ads table beside it counts the
    Google Ads account's days (``analysis.account_time.ACCOUNT_TZ``,
    Europe/London). The table says so on every response — published, withheld
    or with no settled day — naming each account's zone when it is known."""
    from analysis.account_time import ACCOUNT_TZ  # noqa: PLC0415
    zones = [f"{a.get('account_id')} on {a.get('time_zone')}" if a.get("time_zone")
             else f"{a.get('account_id')} on an unknown time zone"
             for a in (accounts or []) if isinstance(a, dict)]
    head = ("Each Meta ad account is read on its own calendar days"
            + (": " + "; ".join(zones) + "." if zones else ", in its own time zone."))
    if len({a.get("time_zone") for a in (accounts or []) if isinstance(a, dict)}) > 1:
        head += " The same date is a different 24 hours in each account."
    return (head + f" These are not the {ACCOUNT_TZ} days the Google Ads table "
            "counts on; nothing on this page is divided by Meta spend.")


def build_meta_adset_table(window: str, now: datetime | None = None) -> dict:
    """The Meta ad-set table for business window ``window`` over its settled
    days (an open window ends yesterday). Raises ``ValueError`` for an
    unsupported window."""
    from services import meta_ads_adset_truth_service as truth  # noqa: PLC0415

    settled = canonical_contract.resolve_settled_window(window, now=now)
    rng = settled.get("window_range") or {}
    window_block = {
        "key": settled.get("key"), "label": settled.get("label"),
        "start_date": settled.get("start_date"), "end_date": settled.get("end_date"),
        "timezone": settled.get("timezone"),
        "through_reason": settled.get("through_reason"),
        "window_range": rng,
    }
    base = {
        "read_only": True,
        "scope": SCOPE,
        "never_summed_with": NEVER_SUMMED_WITH,
        "window": window_block,
        "outcome_link_status": STATUS_NOT_LINKED,
        "outcome_link_reason": REASON_NOT_LINKED,
        "outcome_link_detail": NOT_LINKED_DETAIL,
        "meta_reported_leads_disclosure": truth.LEADS_DISCLOSURE,
        "metric_family": truth.METRIC_FAMILY,
        "window_basis": truth.WINDOW_BASIS,
        "names_are_labels": True,
    }
    if rng.get("empty"):
        return {**base, "status": "withheld",
                "reason": canonical_contract.NO_SETTLED_DAY_IN_WINDOW,
                "detail": rng.get("disclosure"), "rows": [], "totals": None,
                "accounts": [], "time_zones": [], "mixed_time_zones": False,
                "day_basis_disclosure": day_basis_disclosure([]),
                "owner_declared_retired": [],
                "owner_declared_retired_disclosure": None}
    start = (date.fromisoformat(str(settled["start_date"]))
             if settled.get("start_date") else None)
    through = date.fromisoformat(str(settled["end_date"]))
    out = truth.build_meta_adset_breakdown_for_days(start, through, settled=True, now=now)
    published = out.get("status") == truth.STATUS_PUBLISHED
    rows = [_row(r) for r in (out.get("rows") or [])] if published else []
    rows.sort(key=lambda r: (-(((r.get("spend_usd") or {}).get("value")) or 0.0),
                             str(r.get("account_id")), str(r.get("adset_id"))))
    return {
        **base,
        "status": out.get("status"),
        "reason": out.get("reason"),
        "detail": out.get("detail"),
        "rows": rows,
        "totals": table_totals(rows) if published else None,
        "accounts": out.get("accounts") or [],
        "time_zones": out.get("time_zones") or [],
        "mixed_time_zones": bool(out.get("mixed_time_zones")),
        # PR-ADS-177C (M7): every response says whose days these are.
        "day_basis_disclosure": day_basis_disclosure(out.get("accounts") or []),
        "mixed_currencies": bool(out.get("mixed_currencies")),
        "settled_rule_applied": bool(out.get("settled_rule_applied")),
        "native_lead_action_type": out.get("native_lead_action_type"),
        "meta_reported_leads_attribution": out.get("meta_reported_leads_attribution"),
        "config_problems": out.get("config_problems") or [],
        # A figure that rests on the owner's retirement declaration says so
        # (PR-ADS-171B): carried beside the figures, never dropped.
        "owner_declared_retired": out.get("owner_declared_retired") or [],
        "owner_declared_retired_disclosure": out.get("owner_declared_retired_disclosure"),
    }
