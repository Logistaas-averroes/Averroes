"""
Dashboard Revenue & Customers (PR-ADS-135)

The second Dashboard tab (Dashboard → Revenue). It answers, in one screen:
what real revenue and customers did marketing produce, and which deals prove it?

Composition doctrine — this service computes NO new business math. It composes
the same canonical read-only contracts the Overview tab (PR-ADS-134) uses, and
reuses that tab's pure window/bucket/delta helpers so the two tabs can never
disagree on a window or a comparison:

  - build_revenue_decision_mart    -> canonical top-line (leads / SQLs / customers
    (view="campaign")                / won revenue) and readiness (is the revenue
                                      integration connected?). The mart is the
                                      authoritative window total.
  - repo.fetch_revenue_deals       -> the closed-won deal ledger ROWS (windowed by
                                      deal_close_date), read raw so a missing deal
                                      amount stays null (never coerced to $0):
                                      the revenue trend, breakdowns, deal
                                      concentration, and top-deal proof rows.
  - build_revenue_by_source        -> the revenue-by-source breakdown (PR-ADS-133
                                      taxonomy; only Google Ads is spend-connected).
  - repo.fetch_lead_daily_series   -> per-day paid SQL counts (contact_created_at)
                                      for the customer trend.

Truth rules (unchanged, never loosened):
  - HubSpot closed-won deals are the ONLY revenue truth; the Google Ads
    conversion value is never used as revenue.
  - Reporting revenue is USD; native Google Ads currency is GBP; native GBP is
    never labelled USD.
  - No fake $0 / 0.00x / 0%. A missing amount stays null; an unavailable rate
    (missing/zero denominator) is None plus a reason, never 0%. A missing prior
    period yields "No comparison", never 0%.
  - Missing attribution keeps the revenue under "Unclassified / Needs Review" or
    "Unattributed" — never dropped, never forced into Organic or Google Ads.
  - Read-only. NO writes to Google Ads, HubSpot, budgets, bids, campaigns,
    keywords, or offline conversions.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from analysis.business_windows import resolve_window
from db import revenue_repository as repo
from services.revenue_decision_mart import build_revenue_decision_mart
from services.source_attribution_service import build_revenue_by_source

# Reuse the Overview tab's pure helpers so both dashboard tabs share ONE
# window/bucket/delta implementation (PR-ADS-134).
from services.dashboard_overview_service import (
    _bucket_start,
    _bucket_starts,
    _coerce_utc,
    _delta_metric,
    _parse_iso_date,
    _previous_window_reference,
    _rate,
    _round2,
    _WEEKLY_BUCKET_MAX_DAYS,
)

logger = logging.getLogger(__name__)

UNCLASSIFIED_LABEL = "Unclassified / Needs Review"
UNATTRIBUTED_LABEL = "Unattributed"
_BREAKDOWN_LIMIT = 5
_TOP_DEALS_LIMIT = 8


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _deal_amount(deal: dict):
    amount = deal.get("deal_amount_usd")
    if amount is None:
        return None
    try:
        return float(amount)
    except (TypeError, ValueError):
        return None


def _attribution_status(deal: dict, *, has_group: bool) -> str:
    """Coarse attribution state for a ledger row — never fabricated."""
    if not has_group:
        return "unattributed"
    match = (deal.get("match_status") or "").strip().lower()
    if match in ("", "unknown", "unmatched", "none"):
        return "ambiguous"
    return "attributed"


def _avg_deal_value(revenue_usd, customers):
    """Average deal value = revenue / customers, or None (never a fabricated 0)."""
    if revenue_usd is None or customers in (None, 0):
        return None
    return _round2(revenue_usd / customers)


def _build_kpis(summary: dict, deal_rows: list, *, revenue_connected: bool,
                amounts_complete: bool) -> dict:
    """Revenue hero KPIs — every unsafe metric is None, never a fabricated 0."""
    sqls = summary.get("sqls")

    if revenue_connected:
        revenue_usd = summary.get("won_revenue_usd")
        customers = summary.get("customers")
        # Largest Deal is Unavailable when ANY closed-won deal amount is unknown:
        # the unknown deal could be the largest, so the max of known amounts is
        # not a safe "largest deal" (kept strict — no "Largest Known Deal").
        amounts = [a for a in (_deal_amount(d) for d in deal_rows) if a is not None]
        largest_deal = _round2(max(amounts)) if (amounts_complete and amounts) else None
        # An average that would include an unknown amount is uncomputable — it is
        # Unavailable, never revenue/customers with a null deal silently as $0.
        average = _avg_deal_value(revenue_usd, customers) if amounts_complete else None
    else:
        revenue_usd = customers = largest_deal = average = None

    return {
        "closed_won_revenue_usd": revenue_usd,
        "customers": customers,
        "average_deal_value_usd": average,
        "largest_deal_usd": largest_deal,
        "sqls": sqls,
        # customers / SQLs and revenue / SQLs — None when either side is unsafe.
        "sql_to_customer_rate": _rate(customers, sqls),
        "revenue_per_sql_usd": _round2(
            revenue_usd / sqls if (revenue_usd is not None and sqls not in (None, 0)) else None
        ),
    }


def _build_period_change(window: str, now: datetime, kpis: dict) -> dict:
    """Previous-period deltas via a second canonical build at a shifted now."""
    ref = _previous_window_reference(window, now)
    if ref is None:
        return {
            "available": False,
            "label": None,
            "note": None,
            "metrics": {},
            "reason": "All Time has no previous period to compare against.",
        }

    prev_key, prev_now, label, note = ref
    try:
        prev_mart = build_revenue_decision_mart(
            view="campaign", window=prev_key, now=prev_now
        )
    except Exception as exc:  # noqa: BLE001 — comparison optional, truth is not
        logger.warning("Dashboard revenue previous-period build failed: %s", exc)
        return {
            "available": False, "label": label, "note": note,
            "metrics": {}, "reason": "Previous period unavailable.",
        }

    prev_summary = prev_mart.get("summary") or {}
    prev_connected = bool(
        (prev_mart.get("readiness") or {}).get("revenue_integration_connected")
    )
    prev_revenue = prev_summary.get("won_revenue_usd") if prev_connected else None
    prev_customers = prev_summary.get("customers") if prev_connected else None
    prev_avg = _avg_deal_value(prev_revenue, prev_customers) if prev_connected else None

    metrics = {
        "closed_won_revenue_usd": _delta_metric(kpis.get("closed_won_revenue_usd"), prev_revenue),
        "customers": _delta_metric(kpis.get("customers"), prev_customers),
        "average_deal_value_usd": _delta_metric(kpis.get("average_deal_value_usd"), prev_avg),
    }
    return {
        "available": any(m["status"] == "ok" for m in metrics.values()),
        "label": label,
        "note": note,
        "metrics": metrics,
        "reason": None,
    }


def _series_bounds(window_block: dict, deal_rows: list, lead_rows: list):
    """Resolve (start, end, bucket) for the trend, anchoring all_time to data."""
    start = _parse_iso_date(window_block.get("start_date"))
    end = _parse_iso_date(window_block.get("end_date"))
    if end is None:
        return None, None, None
    if start is None:
        candidates = [_parse_iso_date(d.get("deal_close_date")) for d in deal_rows]
        candidates += [_parse_iso_date(r.get("event_date")) for r in lead_rows]
        candidates = [c for c in candidates if c is not None]
        if not candidates:
            return None, None, None
        start = min(candidates)
    if start > end:
        return None, None, None
    bucket = "week" if (end - start).days <= _WEEKLY_BUCKET_MAX_DAYS else "month"
    return start, end, bucket


def _bucket_label(bstart, bucket: str) -> str:
    if not hasattr(bstart, "strftime"):
        return str(bstart)
    if bucket == "month":
        return bstart.strftime("%b %Y")
    # Portable day-of-month (avoid glibc-only "%-d"): strip a leading zero.
    return f"{bstart.day} {bstart.strftime('%b')}"


def _build_revenue_trend(bounds, deal_rows: list, *,
                         revenue_ready: bool, revenue_reason) -> dict:
    """Revenue / customers / deals per bucket — withheld, never fabricated.

    ``bounds`` is a shared (start, end, bucket) tuple so the revenue and
    customer trends always agree on the time grain (they anchor all_time to the
    same earliest data point).
    """
    if not revenue_ready:
        return {"bucket": None, "points": [], "status": "unavailable",
                "reason": revenue_reason or "Revenue ledger unavailable."}

    start, end, bucket = bounds
    if bucket is None:
        return {"bucket": None, "points": [], "status": "unavailable",
                "reason": "No datable closed-won revenue in this window."}

    revenue_by: dict = {}
    deals_by: dict = {}
    unknown_amount_by: dict = {}  # bucket has ≥1 deal with a null amount
    for deal in deal_rows:
        day = _parse_iso_date(deal.get("deal_close_date"))
        if day is None or day < start or day > end:
            continue
        key = max(_bucket_start(day, bucket), start)
        amount = _deal_amount(deal)
        if amount is None:
            # A missing amount makes the bucket total uncomputable — never a $0.
            unknown_amount_by[key] = True
        else:
            revenue_by[key] = revenue_by.get(key, 0.0) + amount
        deals_by[key] = deals_by.get(key, 0) + 1

    points = []
    for bstart in _bucket_starts(start, end, bucket):
        # If any deal in the bucket has an unknown amount, the bucket revenue is
        # Unavailable (null) — the known partial sum would imply completeness.
        revenue = None if unknown_amount_by.get(bstart) else _round2(revenue_by.get(bstart, 0.0))
        points.append({
            "period_start": bstart.isoformat(),
            "period_label": _bucket_label(bstart, bucket),
            # A closed-won deal == a customer in this system (deduped per deal).
            "revenue_usd": revenue,
            "customers": deals_by.get(bstart, 0),
            "deals": deals_by.get(bstart, 0),
        })
    return {"bucket": bucket, "points": points, "status": "ready", "reason": None}


def _build_customer_trend(bounds, deal_rows: list, lead_rows: list, *,
                          revenue_ready: bool, sqls_ready: bool) -> dict:
    """Customers (by close date) + SQLs (by created date) per bucket.

    The two series live on DIFFERENT event-date grains (a long B2B cycle: the
    customers closing this week came from SQLs created weeks or months earlier),
    so this strip shows the two real counts over time but never a per-bucket
    conversion rate — that cross-grain ratio would be fabricated efficiency.
    The honest window-level SQL→Customer rate lives in the KPIs / strip.

    Shares the revenue trend's ``bounds`` so both trends report on ONE grain.
    """
    start, end, bucket = bounds
    if bucket is None:
        return {"bucket": None, "points": [], "customers_status": "unavailable",
                "sqls_status": "ready" if sqls_ready else "unavailable"}

    customers_by: dict = {}
    if revenue_ready:
        for deal in deal_rows:
            day = _parse_iso_date(deal.get("deal_close_date"))
            if day is None or day < start or day > end:
                continue
            key = max(_bucket_start(day, bucket), start)
            customers_by[key] = customers_by.get(key, 0) + 1

    sqls_by: dict = {}
    for row in lead_rows:
        day = _parse_iso_date(row.get("event_date"))
        if day is None or day < start or day > end:
            continue
        key = max(_bucket_start(day, bucket), start)
        sqls_by[key] = sqls_by.get(key, 0) + int(row.get("sqls") or 0)

    points = []
    for bstart in _bucket_starts(start, end, bucket):
        points.append({
            "period_start": bstart.isoformat(),
            "period_label": _bucket_label(bstart, bucket),
            "customers": customers_by.get(bstart, 0) if revenue_ready else None,
            "sqls": sqls_by.get(bstart, 0) if sqls_ready else None,
        })
    return {
        "bucket": bucket,
        "points": points,
        "customers_status": "ready" if revenue_ready else "unavailable",
        "sqls_status": "ready" if sqls_ready else "unavailable",
    }


def _group_deals(deal_rows: list, key_field: str, *, unknown_label: str,
                 target_page: str) -> list:
    """Group ledger deals by campaign/country, preserving unattributed revenue.

    Deals and customers are always counted (never dropped). The group's revenue
    total sums the KNOWN amounts and flags ``amount_unknown`` when any deal in
    the group has a null amount — a group total is uncomputable if a part is
    unknown, so the renderer shows Unavailable rather than a partial sum that
    would imply completeness (or a fabricated $0 contribution).
    """
    groups: dict = {}
    for deal in deal_rows:
        raw = (deal.get(key_field) or "").strip()
        has_group = bool(raw) and raw.lower() not in ("unknown", "(not set)")
        label = raw if has_group else unknown_label
        g = groups.setdefault(label, {
            "label": label, "revenue_usd": 0.0, "customers": 0, "deals": 0,
            "has_group": has_group, "attributed": 0, "amount_unknown": False,
        })
        amount = _deal_amount(deal)
        if amount is None:
            g["amount_unknown"] = True
        else:
            g["revenue_usd"] += amount
        g["customers"] += 1
        g["deals"] += 1
        if _attribution_status(deal, has_group=has_group) == "attributed":
            g["attributed"] += 1
    # Order by known revenue; groups whose total is unknown sort last (they carry
    # no assertable total to rank on).
    return sorted(
        groups.values(),
        key=lambda x: (not x["amount_unknown"], x["revenue_usd"]),
        reverse=True,
    )


def _breakdown_rows(grouped: list, *, name_key: str, target_page: str) -> list:
    rows = []
    for g in grouped[:_BREAKDOWN_LIMIT]:
        if not g["has_group"]:
            status = "unattributed"
        elif g["attributed"] == g["deals"]:
            status = "attributed"
        else:
            status = "partial"
        rows.append({
            name_key: g["label"],
            # Unavailable (None) when any deal amount is unknown — never a
            # partial sum implying completeness, never a fabricated $0.
            "revenue_usd": None if g["amount_unknown"] else _round2(g["revenue_usd"]),
            "customers": g["customers"],
            "deals": g["deals"],
            "attribution_status": status,
            "target_page": target_page,
        })
    return rows


def _build_source_breakdown(window: str, now, *, revenue_connected: bool) -> list:
    """Revenue by source group (PR-ADS-133). Never any ROAS on these rows."""
    if not revenue_connected:
        return []
    source = build_revenue_by_source(window, now=now)
    groups = source.get("groups") or []
    rows = []
    for g in groups:
        revenue = _round2(g.get("won_revenue"))
        if not revenue:
            continue
        rows.append({
            "source_group": g.get("label") or g.get("group"),
            "revenue_usd": revenue,
            "customers": g.get("customers"),
            "spend_connected": bool(g.get("has_spend")),
            "status": ("Spend-connected" if g.get("has_spend")
                       else "Revenue-only"),
            "target_page": "revenue-by-source",
        })
    rows.sort(key=lambda r: r["revenue_usd"] or 0.0, reverse=True)
    return rows[:_BREAKDOWN_LIMIT]


def _build_deal_concentration(deal_rows: list, *, revenue_connected: bool,
                              amounts_complete: bool) -> dict | None:
    # Concentration shares (top-1 / top-3 of total) are uncomputable if any deal
    # amount is unknown — a missing part makes the total, and therefore every
    # share, unknowable. Return None (Unavailable) rather than shares of a
    # partial total that would misstate concentration.
    if not amounts_complete:
        return None
    amounts = sorted(
        (a for a in (_deal_amount(d) for d in deal_rows) if a is not None),
        reverse=True,
    )
    total = sum(amounts)
    if not revenue_connected or not amounts or total <= 0:
        return None
    top_1 = round(amounts[0] / total, 4)
    top_3 = round(sum(amounts[:3]) / total, 4)
    if top_1 > 0.5 or top_3 > 0.7:
        label = "Highly concentrated"
    elif top_1 > 0.35 or top_3 > 0.55:
        label = "Moderately concentrated"
    else:
        label = "Healthy spread"
    return {
        "top_1_deal_share": top_1,
        "top_3_deals_share": top_3,
        "customers": len(amounts),
        "label": label,
    }


def _build_top_deals(deal_rows: list, *, revenue_connected: bool) -> list:
    """Top closed-won deals — the proof layer. Missing fields stay null.

    company_id / main_contact / contact_id / deal name / source are not durably
    stored on the deal ledger (PR-ADS-132/133 precedent), so they are null here
    and the UI renders them as Unavailable — never fabricated, never $0.
    """
    if not revenue_connected:
        return []
    ranked = sorted(
        deal_rows,
        key=lambda d: (_deal_amount(d) if _deal_amount(d) is not None else -1.0),
        reverse=True,
    )
    out = []
    for deal in ranked[:_TOP_DEALS_LIMIT]:
        campaign = (deal.get("campaign_name") or "").strip() or None
        country = (deal.get("country") or "").strip() or None
        out.append({
            "company": deal.get("company") or None,
            "company_id": None,
            "main_contact": None,
            "contact_id": None,
            "deal": None,
            "deal_id": deal.get("deal_id") or None,
            "amount_usd": _round2(_deal_amount(deal)),  # null stays null, never $0
            "close_date": deal.get("deal_close_date"),
            "campaign": campaign,
            "source": None,  # acquisition group not carried on the deal ledger
            "country": country,
            "attribution_status": _attribution_status(deal, has_group=bool(campaign)),
            "attribution_reason": (deal.get("match_source") or None),
        })
    return out


def _build_truth_status(readiness: dict, ledger_status: str | None,
                        by_campaign: list, by_country: list,
                        source_attribution_ready: bool) -> dict:
    revenue_connected = bool(readiness.get("revenue_integration_connected"))
    if not revenue_connected:
        revenue = "blocked"
        deal_ledger = "blocked"
    elif ledger_status != "available":
        revenue = "partial"
        deal_ledger = "unavailable"
    else:
        revenue = "ready"
        deal_ledger = "ready"

    def _attr_status(rows):
        if not rows:
            return "ready"
        return "partial" if any(r["attribution_status"] != "attributed" for r in rows) else "ready"

    campaign_attribution = _attr_status(by_campaign)
    country_attribution = _attr_status(by_country)
    source_attribution = "ready" if source_attribution_ready else "partial"

    return {
        "revenue": revenue,
        "deal_ledger": deal_ledger,
        "hubspot_integration": "ready" if revenue_connected else "blocked",
        "source_attribution": source_attribution,
        "campaign_attribution": campaign_attribution,
        "country_attribution": country_attribution,
    }


def _fmt_usd_short(value) -> str:
    """Compact USD copy for decision-card text.

    An unknown amount is "Unavailable", never a fabricated $0 — a group whose
    total is uncomputable (a null deal amount) must not read as $0 in card copy.
    """
    if value is None:
        return "Unavailable"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "Unavailable"
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if abs(v) >= 1_000:
        return f"${v / 1_000:.1f}k"
    return f"${v:.0f}"


def _build_decision_cards(kpis: dict, concentration: dict | None,
                          by_campaign: list, by_source: list, by_country: list,
                          truth_status: dict, readiness: dict) -> list:
    cards = []
    revenue_connected = bool(readiness.get("revenue_integration_connected"))

    # Scale — a campaign or source that produced multiple customers.
    multi = [r for r in by_campaign if (r.get("customers") or 0) >= 2 and r["attribution_status"] != "unattributed"]
    if not revenue_connected:
        cards.append({
            "type": "scale", "title": "Scale",
            "headline": "Revenue truth unavailable",
            "body": "The HubSpot revenue integration is not connected, so customer production cannot be verified for this window.",
            "target_page": "revenue-health", "target_label": "Open Revenue Health",
        })
    elif multi:
        top = multi[0]
        if top.get("revenue_usd") is None:
            # A deal amount in this group is unknown -> the total is Unavailable,
            # never a fabricated $0. Deals/customers counts are still real.
            body = (f"{top['campaign']} closed {top['deals']} deals across "
                    f"{top['customers']} customers; its revenue total is Unavailable "
                    "(a deal amount is unknown) — verify before scaling.")
        else:
            body = (f"{top['campaign']} closed {_fmt_usd_short(top['revenue_usd'])} across "
                    f"{top['deals']} deals — the strongest repeatable revenue producer this window.")
        cards.append({
            "type": "scale", "title": "Scale",
            "headline": f"{top['campaign']} produced {top['customers']} customers",
            "body": body,
            "target_page": "roas-campaigns", "target_label": "Open ROAS by Campaign",
        })
    else:
        cards.append({
            "type": "scale", "title": "Scale",
            "headline": "No repeat customer producer yet",
            "body": "No campaign closed more than one customer in this window.",
            "target_page": "roas-campaigns", "target_label": "Open ROAS by Campaign",
        })

    # Watch — revenue concentration.
    if concentration and concentration["label"] != "Healthy spread":
        pct = round(concentration["top_3_deals_share"] * 100)
        cards.append({
            "type": "watch", "title": "Watch",
            "headline": f"Top 3 deals drove {pct}% of revenue",
            "body": (f"Revenue is {concentration['label'].lower()} across "
                     f"{concentration['customers']} customers — validate repeatability before scaling."),
            "target_page": "deals", "target_label": "Open Deals",
        })
    else:
        cards.append({
            "type": "watch", "title": "Watch",
            "headline": "Revenue is well spread" if concentration else "Concentration unavailable",
            "body": ("No single deal dominates this window's revenue." if concentration
                     else "Deal concentration cannot be computed without closed-won revenue."),
            "target_page": "deals", "target_label": "Open Deals",
        })

    # Investigate — unclassified / unattributed revenue.
    unclassified_campaign = next((r for r in by_campaign if r["attribution_status"] == "unattributed"), None)
    unclassified_country = next((r for r in by_country if r["attribution_status"] == "unattributed"), None)
    if unclassified_campaign or unclassified_country:
        blockers = []
        if unclassified_campaign:
            rev = unclassified_campaign["revenue_usd"]
            blockers.append("revenue with an Unavailable amount and no campaign"
                            if rev is None else f"{_fmt_usd_short(rev)} without a campaign")
        if unclassified_country:
            rev = unclassified_country["revenue_usd"]
            blockers.append("revenue with an Unavailable amount and no country"
                            if rev is None else f"{_fmt_usd_short(rev)} without a country")
        cards.append({
            "type": "investigate", "title": "Investigate",
            "headline": "Revenue with incomplete attribution",
            "body": ("; ".join(blockers) + ". The revenue is real but not fully attributed — "
                     "it is kept under Unclassified, never forced into a source."),
            "target_page": "revenue-by-source", "target_label": "Open Revenue by Source",
        })
    else:
        cards.append({
            "type": "investigate", "title": "Investigate",
            "headline": "Attribution looks complete" if revenue_connected else "Attribution unavailable",
            "body": ("Every closed-won deal this window carries a campaign and country." if revenue_connected
                     else "Revenue attribution cannot be verified without a connected revenue integration."),
            "target_page": "revenue-by-source", "target_label": "Open Revenue by Source",
        })

    # Unavailable — truth blockers.
    blockers = []
    if truth_status["revenue"] != "ready":
        blockers.append("revenue ledger")
    if truth_status["source_attribution"] != "ready":
        blockers.append("source attribution")
    if truth_status["campaign_attribution"] != "ready":
        blockers.append("campaign attribution")
    if truth_status["country_attribution"] != "ready":
        blockers.append("country attribution")
    if blockers:
        cards.append({
            "type": "unavailable", "title": "Unavailable",
            "headline": f"{len(blockers)} attribution blocker{'s' if len(blockers) != 1 else ''}",
            "body": "Incomplete: " + ", ".join(blockers) + ".",
            "target_page": "revenue-health", "target_label": "Open Revenue Health",
        })
    else:
        cards.append({
            "type": "unavailable", "title": "Unavailable",
            "headline": "All revenue truth verified",
            "body": "Revenue, deal ledger, and attribution are verified for this window.",
            "target_page": "revenue-health", "target_label": "Open Revenue Health",
        })
    return cards


def _build_unavailable(kpis: dict, readiness: dict, revenue_trend: dict,
                       customer_trend: dict, period_change: dict) -> list:
    out = []
    if not readiness.get("revenue_integration_connected"):
        out.append({"metric": "closed_won_revenue_usd",
                    "reason": "Revenue integration not connected for this window."})
    if kpis.get("sqls") is None:
        out.append({"metric": "sqls",
                    "reason": "Lead metrics withheld — business event dates unavailable."})
    if kpis.get("sql_to_customer_rate") is None:
        out.append({"metric": "sql_to_customer_rate",
                    "reason": "SQL→Customer rate needs a connected revenue integration and non-zero SQLs."})
    if kpis.get("average_deal_value_usd") is None:
        out.append({"metric": "average_deal_value_usd",
                    "reason": "Average deal value needs at least one closed-won customer."})
    # Contact-level proof fields are never durably stored on the deal ledger.
    out.append({"metric": "main_contact",
                "reason": "Contact name is not durably stored."})
    out.append({"metric": "company_id",
                "reason": "Company record id is not durably stored."})
    if revenue_trend.get("status") != "ready":
        out.append({"metric": "revenue_trend",
                    "reason": revenue_trend.get("reason") or "Revenue series unavailable."})
    if customer_trend.get("sqls_status") != "ready":
        out.append({"metric": "customer_trend_sqls",
                    "reason": "Per-period SQL series unavailable."})
    if not period_change.get("available"):
        out.append({"metric": "period_change",
                    "reason": period_change.get("reason") or "No comparison available."})
    return out


def build_dashboard_revenue(window: str = "current_quarter",
                            now: datetime | None = None) -> dict:
    """Build the Dashboard Revenue & Customers contract for one business window.

    Args:
        window: A business-window key (analysis.business_windows.WINDOW_KEYS).
        now: Optional reference time for deterministic resolution/testing.

    Returns:
        The compact revenue contract described in the module docstring.

    Raises:
        ValueError: If ``window`` is not a supported business window.
    """
    resolved = resolve_window(window, now=now)
    ref_now = _coerce_utc(now)

    mart = build_revenue_decision_mart(view="campaign", window=window, now=now)
    window_block = mart.get("window") or {}
    summary = mart.get("summary") or {}
    readiness = mart.get("readiness") or {}

    # Read the closed-won deal ledger ROWS directly so a missing deal amount
    # stays null (build_revenue_deals coerces it to $0 via _safe_float). The
    # mart remains the authoritative window total; these rows are the proof.
    start_date = date.fromisoformat(resolved["start_date"]) if resolved.get("start_date") else None
    end_date = date.fromisoformat(resolved["end_date"])
    ledger = repo.fetch_revenue_deals(start_date, end_date)
    ledger_status = "available" if ledger.get("available") else "database_unavailable"
    deal_rows = ledger.get("rows") or []

    revenue_connected = bool(readiness.get("revenue_integration_connected"))
    revenue_ready = revenue_connected and ledger_status == "available"
    revenue_reason = None
    if not revenue_connected:
        revenue_reason = "Revenue integration not connected — a $0 series would be fabricated."
    elif ledger_status != "available":
        revenue_reason = "Closed-won deal ledger unavailable."

    # A window is amount-complete when every closed-won deal has a known amount;
    # any null makes completeness-requiring aggregates (average, concentration,
    # group/bucket totals) Unavailable rather than embed a fabricated $0.
    amounts_complete = all(_deal_amount(d) is not None for d in deal_rows)

    # Fetch the per-day SQL series once and resolve ONE (start, end, bucket) so
    # the revenue and customer trends always share the same time grain.
    lead_series = repo.fetch_lead_daily_series(start_date, end_date)
    lead_rows = (lead_series.get("rows") or []) if lead_series.get("available") else []
    sqls_ready = bool(lead_series.get("available"))
    trend_bounds = _series_bounds(window_block, deal_rows, lead_rows)

    kpis = _build_kpis(summary, deal_rows, revenue_connected=revenue_connected,
                       amounts_complete=amounts_complete)
    period_change = _build_period_change(window, ref_now, kpis)
    revenue_trend = _build_revenue_trend(
        trend_bounds, deal_rows, revenue_ready=revenue_ready, revenue_reason=revenue_reason)
    customer_trend = _build_customer_trend(
        trend_bounds, deal_rows, lead_rows, revenue_ready=revenue_ready, sqls_ready=sqls_ready)

    ledger_for_breakdown = deal_rows if revenue_ready else []
    grouped_campaign = _group_deals(ledger_for_breakdown, "campaign_name",
                                    unknown_label=UNCLASSIFIED_LABEL,
                                    target_page="roas-campaigns")
    grouped_country = _group_deals(ledger_for_breakdown, "country",
                                   unknown_label=UNATTRIBUTED_LABEL,
                                   target_page="roas-countries")
    by_campaign = _breakdown_rows(grouped_campaign, name_key="campaign",
                                  target_page="roas-campaigns")
    by_country = _breakdown_rows(grouped_country, name_key="country",
                                 target_page="roas-countries")
    by_source = _build_source_breakdown(window, now, revenue_connected=revenue_connected)
    source_attribution_ready = not any(
        (r.get("source_group") or "").lower().startswith("unclassified") for r in by_source
    )

    concentration = _build_deal_concentration(
        deal_rows, revenue_connected=revenue_connected, amounts_complete=amounts_complete)
    top_deals = _build_top_deals(deal_rows, revenue_connected=revenue_connected)

    truth_status = _build_truth_status(
        readiness, ledger_status, by_campaign, by_country, source_attribution_ready)
    decision_cards = _build_decision_cards(
        kpis, concentration, by_campaign, by_source, by_country, truth_status, readiness)
    unavailable = _build_unavailable(
        kpis, readiness, revenue_trend, customer_trend, period_change)

    return {
        "window": window_block,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "read_only": True,
        "truth_status": truth_status,
        "kpis": kpis,
        "period_change": period_change,
        "revenue_trend": revenue_trend,
        "customer_trend": customer_trend,
        "breakdowns": {
            "by_campaign": by_campaign,
            "by_source": by_source,
            "by_country": by_country,
        },
        "deal_concentration": concentration,
        "top_deals": top_deals,
        "decision_cards": decision_cards,
        "readiness": readiness,
        "unavailable": unavailable,
        "source_truth": "revenue_decision_mart",
        "google_ads_conversion_value_used": False,
    }
