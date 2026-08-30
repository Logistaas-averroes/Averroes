"""
Dashboard Channels & Platforms (PR-ADS-136)

The third Dashboard tab (Dashboard → Channels). It answers, in one screen:
which channels and platforms are producing SQLs, customers, and closed-won
revenue — and which are revenue-only because spend is not connected?

Composition doctrine — this service computes NO new business math and adds NO
new taxonomy. It re-presents the PR-ADS-133 source classification in executive
channel/platform language and composes canonical read-only contracts:

  - build_revenue_by_source          -> the durable group → channel → platform
                                        taxonomy (leads / SQLs / customers / won
                                        revenue per level; Google Ads is the only
                                        spend-connected path). The shared
                                        classifier (analysis.source_classification)
                                        is called, never re-implemented.
  - build_revenue_decision_mart      -> canonical top-line (SQLs / customers /
    (view="campaign")                  won revenue) + the CANONICAL Google Ads
                                        spend truth (FX-gated USD + native GBP) and
                                        readiness. The source-page geo spend is NOT
                                        used for ROAS (it is ungated); the mart's
                                        FX-verified spend is.
  - repo.fetch_source_*_daily        -> per-day classified source rows for the
                                        channel trend (read-only).

Truth rules (unchanged, never loosened):
  - Only Google Ads / Paid Search is spend-connected and ROAS-eligible. Paid
    Social, Organic, Email, Events, SalesNash, Referrals, Direct, Offline are
    revenue/SQL/customer attribution only — spend and ROAS stay null with a
    "no connected spend source" status. Never fake Meta/LinkedIn/Organic spend
    or ROAS.
  - HubSpot closed-won deals are the only revenue truth; the Google Ads
    conversion value is never used. Reporting revenue is USD; native spend is
    GBP; native GBP is never labelled USD.
  - No fake $0 / 0.00x / 0%. A missing amount stays null; a total that needs
    completeness becomes Unavailable when any part is unknown; a missing prior
    period yields "No comparison".
  - Missing attribution keeps the revenue under "Unclassified / Needs Review" —
    never dropped, never forced into Organic or Google Ads.
  - Read-only. NO writes to Google Ads, HubSpot, budgets, bids, campaigns,
    keywords, or offline conversions.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

# PR-ADS-154C: THE canonical window anchor — the Google Ads account
# calendar day, not UTC. Sharing `resolve_window` while disagreeing about
# which day "today" is meant two pages could resolve `current_quarter` to
# different quarters across the account's midnight and both call it "this
# quarter". Spend is denominated in the account day, so the account day wins.
from services.canonical_contract import resolve_canonical_window
from services import canonical_contract
from analysis.source_classification import (
    CH_DIRECT_EMAIL,
    CH_EMAIL_MARKETING,
    CH_EVENTS,
    CH_IMPORTED_CRM,
    CH_NEEDS_REVIEW,
    CH_ORGANIC_SEARCH_DIRECT,
    CH_ORGANIC_SOCIAL,
    CH_OTHER_CAMPAIGNS,
    CH_PAID_SEARCH,
    CH_PAID_SOCIAL,
    CH_REFERRALS_PARTNERS,
    CH_SALESNASH,
    CH_UNSPECIFIED,
    GROUP_UNCLASSIFIED,
)
from analysis import revenue_scope
from db import revenue_repository as repo
from services import canonical_revenue_service as canonical_revenue
from services.revenue_decision_mart import build_revenue_decision_mart
from services.source_attribution_service import (
    build_revenue_by_source,
    _nullable_float,
    _section_bucket,
    _taxonomy_for_section,
)

# Reuse the Overview tab's pure window/bucket/delta helpers (PR-ADS-134/135).
from services.dashboard_overview_service import (
    _bucket_start,
    _bucket_starts,
    _coerce_utc,
    _delta_metric,
    _parse_iso_date,
    _previous_window_reference,
    _round2,
    _WEEKLY_BUCKET_MAX_DAYS,
)

logger = logging.getLogger(__name__)

# ── Executive channel buckets ────────────────────────────────────────────────
# A PRESENTATION mapping from the PR-ADS-133 classifier channels to the eight
# executive channel buckets this tab shows. This never changes the classifier;
# it groups its channels for display. Order = display order.

CX_GOOGLE_ADS = "google_ads"
CX_PAID_SOCIAL = "paid_social"
CX_OTHER_PAID = "other_paid"
CX_ORGANIC_SEARCH_DIRECT = "organic_search_direct"
CX_ORGANIC_SOCIAL = "organic_social"
CX_REFERRALS_PARTNER = "referrals_partner"
CX_OFFLINE = "offline"
CX_UNCLASSIFIED = "unclassified"

EXEC_CHANNEL_ORDER = [
    CX_GOOGLE_ADS, CX_PAID_SOCIAL, CX_OTHER_PAID, CX_ORGANIC_SEARCH_DIRECT,
    CX_ORGANIC_SOCIAL, CX_REFERRALS_PARTNER, CX_OFFLINE, CX_UNCLASSIFIED,
]

EXEC_CHANNEL_LABELS = {
    CX_GOOGLE_ADS: "Google Ads",
    CX_PAID_SOCIAL: "Paid Social",
    CX_OTHER_PAID: "Other Paid",
    CX_ORGANIC_SEARCH_DIRECT: "Organic Search + Direct",
    CX_ORGANIC_SOCIAL: "Organic Social",
    CX_REFERRALS_PARTNER: "Referrals / Partner",
    CX_OFFLINE: "Offline / CRM imported",
    CX_UNCLASSIFIED: "Unclassified / Needs Review",
}

# classifier channel id -> executive bucket.
_CHANNEL_TO_EXEC = {
    CH_PAID_SEARCH: CX_GOOGLE_ADS,
    CH_PAID_SOCIAL: CX_PAID_SOCIAL,
    CH_EMAIL_MARKETING: CX_OTHER_PAID,
    CH_OTHER_CAMPAIGNS: CX_OTHER_PAID,
    CH_EVENTS: CX_OTHER_PAID,
    CH_SALESNASH: CX_OTHER_PAID,
    CH_ORGANIC_SEARCH_DIRECT: CX_ORGANIC_SEARCH_DIRECT,
    CH_ORGANIC_SOCIAL: CX_ORGANIC_SOCIAL,
    CH_REFERRALS_PARTNERS: CX_REFERRALS_PARTNER,
    CH_DIRECT_EMAIL: CX_REFERRALS_PARTNER,
    CH_IMPORTED_CRM: CX_OFFLINE,
    CH_NEEDS_REVIEW: CX_UNCLASSIFIED,
    CH_UNSPECIFIED: CX_UNCLASSIFIED,
}

# Status copy (business-facing, never engineering terms).
STATUS_SPEND_CONNECTED = "Spend-connected"
STATUS_REVENUE_ONLY = "Revenue-only — no connected spend source"
STATUS_OFFLINE = "Offline / CRM imported — attribution not reliable"
STATUS_NEEDS_REVIEW = "Attribution needs review"


def exec_channel_for(classifier_channel: str) -> str:
    """Map a PR-ADS-133 classifier channel id to its executive bucket."""
    return _CHANNEL_TO_EXEC.get(classifier_channel, CX_UNCLASSIFIED)


def _round2n(v):
    return _round2(v)


def _channel_status(exec_channel: str, spend_connected: bool) -> str:
    if exec_channel == CX_GOOGLE_ADS and spend_connected:
        return STATUS_SPEND_CONNECTED
    if exec_channel == CX_GOOGLE_ADS:
        # Google Ads bucket but the canonical spend source is unavailable.
        return STATUS_REVENUE_ONLY
    if exec_channel == CX_OFFLINE:
        return STATUS_OFFLINE
    if exec_channel == CX_UNCLASSIFIED:
        return STATUS_NEEDS_REVIEW
    return STATUS_REVENUE_ONLY


def _canonical_google_spend(mart: dict) -> dict:
    """Canonical Google Ads spend truth from the mart (FX-gated), never geo spend."""
    spend_truth = mart.get("spend_truth") or {}
    summary = mart.get("summary") or {}
    return {
        # USD is None unless FX + coverage are verified (mart summary.spend_usd).
        "usd": summary.get("spend_usd"),
        "native_amount": spend_truth.get("native_spend"),
        "native_currency": spend_truth.get("native_currency") or "GBP",
        "fx_status": spend_truth.get("fx_status"),
        "campaign_spend_status": spend_truth.get("campaign_spend_status"),
    }


def _google_roas(google_won_revenue, usd_spend):
    """Google Ads ROAS = Google-attributed revenue / canonical USD spend.

    Only when the canonical USD spend is safe (FX + coverage verified, so
    non-null and positive). Never a fabricated 0.00x, never from geo/native spend.
    """
    if google_won_revenue is None or usd_spend is None or usd_spend <= 0:
        return None
    return round(float(google_won_revenue) / float(usd_spend), 2)


def _accumulate(dst: dict, leads, sqls, customers, won, *, amount_known: bool):
    dst["leads"] += leads or 0
    dst["sqls"] += sqls or 0
    dst["customers"] += customers or 0
    if amount_known:
        dst["won_revenue"] += won or 0.0
    else:
        dst["amount_unknown"] = True


def _new_bucket():
    return {"leads": 0, "sqls": 0, "customers": 0, "won_revenue": 0.0, "amount_unknown": False}


def _unknown_amount_maps(revenue_rows: list) -> tuple[set, set]:
    """Which executive channels / platform buckets contain an unknown deal amount.

    build_revenue_by_source aggregates by GROUP → channel → platform, which is a
    different fold from the executive-channel rows this page shows, so its own
    per-bucket ``revenue_unavailable_deals`` counts cannot be reused directly.
    We re-read the same canonical closed-won rows (PR-ADS-153E-B) and, using the
    source service's OWN section/taxonomy helpers (no new taxonomy), record which
    executive channel and which (group, channel, platform) bucket holds a deal
    whose amount is unknown. Those buckets' revenue (and any ROAS) must render
    Unavailable — never a lowered $0.
    """
    exec_unknown: set = set()
    platform_unknown: set = set()
    for r in revenue_rows or []:
        if _nullable_float(r.get("deal_amount_usd")) is not None:
            continue
        section = _section_bucket(r.get("acquisition_group") or GROUP_UNCLASSIFIED)
        ch, _cl, pf, _pl = _taxonomy_for_section(
            section, r.get("source_primary_raw"), r.get("source_detail_raw"))
        exec_unknown.add(exec_channel_for(ch))
        platform_unknown.add((section, ch, pf))
    return exec_unknown, platform_unknown


def _build_channels_and_platforms(source_groups: list, google_spend: dict,
                                  exec_unknown: set, platform_unknown: set,
                                  amounts_verifiable: bool = True) -> tuple[list, list]:
    """Fold the nested source taxonomy into executive channels + a platform matrix.

    Every level's counts and revenue come straight from build_revenue_by_source
    (which already summed them from the durable classified rows). Spend/ROAS
    attach ONLY to the Google Ads channel, and only from the canonical mart spend.
    A channel/platform whose raw rows include an unknown deal amount reports its
    revenue (and ROAS) as null — never a silently lowered $0.

    ``amounts_verifiable`` is False when the raw closed-won source rows could not
    be re-read to detect unknown amounts; then NO revenue total can be trusted as
    complete, so every channel/platform revenue (and ROAS) is reported as
    Unavailable — fail closed, never a silently lowered total.
    """
    channels: dict = {}
    platforms: list = []

    for group in source_groups or []:
        for ch in group.get("channels") or []:
            exec_ch = exec_channel_for(ch.get("channel"))
            agg = channels.setdefault(exec_ch, _new_bucket())
            ch_won = ch.get("won_revenue")
            # An unknown amount anywhere in this executive channel makes its total
            # Unavailable (the source service's sum silently absorbed it as $0). If
            # the raw rows were unverifiable, no total can be trusted → all unknown.
            ch_amount_known = (amounts_verifiable and ch_won is not None
                               and exec_ch not in exec_unknown)
            _accumulate(agg, ch.get("leads"), ch.get("sqls"), ch.get("customers"),
                        ch_won, amount_known=ch_amount_known)

            for pf in ch.get("platforms") or []:
                is_google = (exec_ch == CX_GOOGLE_ADS
                             and pf.get("platform") == "google_ads")
                pf_key = (group.get("group"), ch.get("channel"), pf.get("platform"))
                pf_amount_known = (amounts_verifiable and pf.get("won_revenue") is not None
                                   and pf_key not in platform_unknown)
                pf_won = pf.get("won_revenue") if pf_amount_known else None
                pf_roas = _google_roas(pf_won, google_spend["usd"]) if is_google else None
                # spend_connected reflects the Google Ads spend SOURCE being connected —
                # i.e. native (GBP) spend exists. It does NOT require USD/FX: when FX
                # is missing the source stays connected, native GBP is shown, and USD
                # spend + ROAS are Unavailable. roas_available is the separate ROAS gate.
                spend_connected = bool(is_google and google_spend["native_amount"] is not None)
                platforms.append({
                    "platform": pf.get("platform"),
                    "platform_label": pf.get("label"),
                    "channel": exec_ch,
                    "channel_label": EXEC_CHANNEL_LABELS[exec_ch],
                    "source_group": group.get("group"),
                    "source_channel": ch.get("channel"),
                    "leads": pf.get("leads"),
                    "sqls": pf.get("sqls"),
                    "customers": pf.get("customers"),
                    "won_revenue_usd": _round2(pf_won) if pf_won is not None else None,
                    "spend_usd": google_spend["usd"] if is_google else None,
                    "native_spend": ({"amount": google_spend["native_amount"],
                                      "currency": google_spend["native_currency"]}
                                     if is_google else None),
                    "roas": pf_roas,
                    "spend_connected": spend_connected,
                    "roas_available": pf_roas is not None,
                    "status": _channel_status(exec_ch, spend_connected),
                })

    # Shape executive channel rows (display order), Google Ads first.
    channel_rows = []
    for exec_ch in EXEC_CHANNEL_ORDER:
        agg = channels.get(exec_ch)
        if agg is None:
            continue
        is_google = exec_ch == CX_GOOGLE_ADS
        won = None if agg["amount_unknown"] else _round2(agg["won_revenue"])
        roas = _google_roas(won, google_spend["usd"]) if is_google else None
        # spend_connected reflects the Google Ads spend SOURCE being connected
        # (native GBP spend exists), independent of USD/FX and of revenue. ROAS is
        # gated separately: roas_available iff roas is a real, USD-based number.
        spend_connected = bool(is_google and google_spend["native_amount"] is not None)
        channel_rows.append({
            "channel": exec_ch,
            "channel_label": EXEC_CHANNEL_LABELS[exec_ch],
            "leads": agg["leads"],
            "sqls": agg["sqls"],
            "customers": agg["customers"],
            "won_revenue_usd": won,
            "spend_usd": google_spend["usd"] if is_google else None,
            "native_spend": ({"amount": google_spend["native_amount"],
                              "currency": google_spend["native_currency"]}
                             if is_google else None),
            "roas": roas,
            "spend_connected": spend_connected,
            "roas_available": roas is not None,
            "status": _channel_status(exec_ch, spend_connected),
        })

    # Platform matrix: Google Ads first, then by customers, then won revenue.
    platforms.sort(
        key=lambda p: (p["channel"] == CX_GOOGLE_ADS, p.get("customers") or 0,
                       p.get("won_revenue_usd") or 0.0),
        reverse=True,
    )
    return channel_rows, platforms


def _build_kpis(channel_rows: list, *, revenue_available: bool = True,
                revenue_total_available: bool = True) -> dict:
    """Hero KPIs derived FROM the channel rows so they reconcile exactly with the
    channel mix, platform matrix and trend (all built from the Revenue-by-Source
    taxonomy) — never a mart total on a different denominator.

    SQLs come from contacts and are always available (they survive a revenue
    outage). Customers/revenue come from closed-won deals: customers is Unavailable
    if any channel's is (a disconnected revenue integration blanks them), and
    revenue is Unavailable unless EVERY channel's revenue is known — never a
    partial sum implying completeness.
    """
    total_sqls = sum((c.get("sqls") or 0) for c in channel_rows)

    # PR-ADS-153E-B: an UNAVAILABLE canonical ledger produces no channel rows at
    # all, and summing an empty list yields a confident 0.00 / 0 customers — a
    # verified-looking zero standing in for an outage. Availability is therefore
    # an explicit input, not something inferred from the rows that survived.
    if not revenue_available:
        return {
            "total_sqls": total_sqls,
            "total_customers": None,
            "closed_won_revenue_usd": None,
            "spend_connected_revenue_usd": None,
            "revenue_only_revenue_usd": None,
        }

    customers_incomplete = any(c.get("customers") is None for c in channel_rows)
    total_customers = (None if customers_incomplete
                       else sum((c.get("customers") or 0) for c in channel_rows))

    # PR-ADS-154C-F3-F1 §1: the canonical verdict gates the REVENUE TOTAL and
    # nothing else. `won_deals` is complete whatever the amounts are, so the
    # customer count above is untouched — withholding a number we did measure
    # would be its own fabrication.
    revenue_incomplete = (not revenue_total_available) or any(
        c.get("won_revenue_usd") is None for c in channel_rows)
    total_revenue = 0.0
    spend_connected_revenue = 0.0
    revenue_only_revenue = 0.0
    for c in channel_rows:
        won = c.get("won_revenue_usd")
        if won is None:
            continue
        total_revenue += won
        if c["spend_connected"]:
            spend_connected_revenue += won
        else:
            revenue_only_revenue += won

    return {
        "total_sqls": total_sqls,
        "total_customers": total_customers,
        "closed_won_revenue_usd": None if revenue_incomplete else _round2(total_revenue),
        "spend_connected_revenue_usd": (None if revenue_incomplete
                                        else _round2(spend_connected_revenue)),
        "revenue_only_revenue_usd": (None if revenue_incomplete
                                     else _round2(revenue_only_revenue)),
    }


def _top_channel_and_platform(channel_rows: list, platforms: list) -> tuple:
    def _pick(rows, name_key):
        scored = [r for r in rows if (r.get("customers") or 0) > 0
                  or (r.get("won_revenue_usd") or 0) > 0]
        if not scored:
            return None
        best = max(scored, key=lambda r: (r.get("customers") or 0,
                                          r.get("won_revenue_usd") or 0.0))
        return best.get(name_key)
    return _pick(channel_rows, "channel_label"), _pick(platforms, "platform_label")


# ── Channel trend (per-bucket SQLs / customers / revenue by channel) ─────────


def _series_bounds(window_block, lead_rows, rev_rows):
    start = _parse_iso_date(window_block.get("start_date"))
    end = _parse_iso_date(window_block.get("end_date"))
    if end is None:
        return None, None, None
    if start is None:
        cands = [_parse_iso_date(r.get("event_date")) for r in lead_rows]
        cands += [_parse_iso_date(r.get("close_date")) for r in rev_rows]
        cands = [c for c in cands if c is not None]
        if not cands:
            return None, None, None
        start = min(cands)
    if start > end:
        return None, None, None
    bucket = "week" if (end - start).days <= _WEEKLY_BUCKET_MAX_DAYS else "month"
    return start, end, bucket


def _exec_channel_from_raw(primary_raw, detail_raw, acquisition_group) -> str:
    """Executive channel for a raw source row — IDENTICAL to the channel-mix path.

    Uses the source service's OWN section/taxonomy helpers (the same ones
    build_revenue_by_source uses), so the trend classifies a deal exactly as the
    channel-mix / platform-matrix does. A deal whose stored group disagrees with
    its raw source (e.g. missing primary on a Google-grouped deal) folds into the
    section's Unspecified channel → Unclassified — never force-credited to Google
    Ads, and never divergent between the two panels of the same tab.
    """
    section = _section_bucket(acquisition_group or GROUP_UNCLASSIFIED)
    ch, _cl, _pf, _pl = _taxonomy_for_section(section, primary_raw, detail_raw)
    return exec_channel_for(ch)


def _bucket_label(bstart, bucket: str) -> str:
    if not hasattr(bstart, "strftime"):
        return str(bstart)
    if bucket == "month":
        return bstart.strftime("%b %Y")
    return f"{bstart.day} {bstart.strftime('%b')}"


def _utc_midnight(day):
    """A plain ``date`` as an explicit UTC midnight datetime, or ``None``.

    The ledger read casts its bounds to ``timestamptz``, and PostgreSQL resolves
    a DATE → timestamptz cast using the SESSION time zone. On a server whose
    session is not UTC that silently shifts the window by hours and mis-buckets
    deals that closed near a boundary — the trend and the KPI above it would
    then disagree about the same quarter. Passing an explicit UTC datetime makes
    the bound mean the same thing regardless of session settings, matching
    ``analysis.business_windows.get_window_bounds``.
    """
    from datetime import datetime, time, timezone  # noqa: PLC0415

    if day is None:
        return None
    if isinstance(day, datetime):
        return day if day.tzinfo else day.replace(tzinfo=timezone.utc)
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def _canonical_daily_revenue(start_key, end_key) -> dict:
    """Canonical won deals for a date range, carrying a per-day ``close_date``.

    The bounds arrive as inclusive dates from the resolved window block, so the
    upper bound is converted to the EXCLUSIVE bound the ledger read expects —
    the same convention ``analysis.business_windows.get_window_bounds`` uses —
    and both are expressed as explicit UTC datetimes (see ``_utc_midnight``).
    """
    from datetime import timedelta  # noqa: PLC0415

    if end_key is None:
        return {"available": False, "rows": []}
    base = canonical_revenue.load_won_deals(
        start=_utc_midnight(start_key),
        end=_utc_midnight(end_key + timedelta(days=1)))
    if not base.get("available"):
        return {"available": False, "rows": [], "reason": base.get("reason")}
    rows = []
    for row in canonical_revenue.canonical_deal_rows(
            base, revenue_scope.SCOPE_ALL_SOURCE):
        close = row.get("deal_close_date")
        rows.append({**row, "close_date": str(close)[:10] if close else None})
    return {"available": True, "rows": rows}


def _build_trend(window_block: dict) -> dict:
    """Per-bucket SQLs (by created date) + customers/revenue (by close date) per
    executive channel. Each metric uses its OWN business event date. A bucket
    whose revenue includes an unknown amount reports that channel's revenue as
    null (never a fabricated $0). Withheld entirely if the durable series are
    unavailable."""
    start_key = _parse_iso_date(window_block.get("start_date"))
    end_key = _parse_iso_date(window_block.get("end_date"))

    leads = repo.fetch_source_leads_daily(start_key, end_key)
    # PR-ADS-153E-B: the per-day revenue series is the canonical ledger, bucketed
    # by the canonical deal close date. `deal_source_attribution` supplied the
    # same shape but a different population and no currency contract, so the
    # trend and the KPI above it could disagree about the same quarter.
    revenue = _canonical_daily_revenue(start_key, end_key)
    if not leads.get("available") or not revenue.get("available"):
        return {"bucket": None, "points": [], "status": "unavailable",
                "reason": "Per-day channel source series is unavailable."}

    lead_rows = leads.get("rows") or []
    rev_rows = revenue.get("rows") or []
    start, end, bucket = _series_bounds(window_block, lead_rows, rev_rows)
    if bucket is None:
        return {"bucket": None, "points": [], "status": "unavailable",
                "reason": "No datable channel activity in this window."}

    # per (bucket_start, exec_channel) -> {sqls, customers, revenue, amount_unknown}
    grid: dict = {}

    def cell(bstart, ch):
        return grid.setdefault((bstart, ch),
                               {"sqls": 0, "customers": 0, "revenue": 0.0,
                                "amount_unknown": False})

    for r in lead_rows:
        day = _parse_iso_date(r.get("event_date"))
        if day is None or day < start or day > end:
            continue
        if r.get("status_category") != "qualified":
            continue
        ch = _exec_channel_from_raw(r.get("source_primary_raw"), r.get("source_detail_raw"),
                                    r.get("acquisition_group"))
        bstart = max(_bucket_start(day, bucket), start)
        cell(bstart, ch)["sqls"] += 1

    for r in rev_rows:
        day = _parse_iso_date(r.get("close_date"))
        if day is None or day < start or day > end:
            continue
        ch = _exec_channel_from_raw(r.get("source_primary_raw"), r.get("source_detail_raw"),
                                    r.get("acquisition_group"))
        bstart = max(_bucket_start(day, bucket), start)
        c = cell(bstart, ch)
        c["customers"] += 1
        amount = r.get("deal_amount_usd")
        if amount is None:
            c["amount_unknown"] = True
        else:
            c["revenue"] += float(amount)

    points = []
    for bstart in _bucket_starts(start, end, bucket):
        chans = []
        for exec_ch in EXEC_CHANNEL_ORDER:
            c = grid.get((bstart, exec_ch))
            if not c or (c["sqls"] == 0 and c["customers"] == 0):
                continue
            chans.append({
                "channel": exec_ch,
                "channel_label": EXEC_CHANNEL_LABELS[exec_ch],
                "sqls": c["sqls"],
                "customers": c["customers"],
                "revenue_usd": None if c["amount_unknown"] else _round2(c["revenue"]),
            })
        points.append({
            "period_start": bstart.isoformat(),
            "period_label": _bucket_label(bstart, bucket),
            "channels": chans,
        })
    return {"bucket": bucket, "points": points, "status": "ready", "reason": None}


def _build_quality_matrix(channel_rows: list) -> list:
    """Quality-vs-revenue bubble points: x=SQLs, y=customers, size=revenue."""
    out = []
    for c in channel_rows:
        if c.get("sqls") is None:
            continue
        if (c["sqls"] or 0) == 0 and (c["customers"] or 0) == 0:
            continue
        out.append({
            "channel": c["channel"],
            "channel_label": c["channel_label"],
            "sqls": c["sqls"],
            "customers": c["customers"],
            "revenue_usd": c["won_revenue_usd"],  # None => bubble neutral/min
            "spend_connected": c["spend_connected"],
        })
    return out


def _fmt_usd_short(value) -> str:
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


def _build_decision_cards(channel_rows: list, kpis: dict, google_spend: dict,
                          readiness: dict) -> list:
    cards = []
    by_channel = {c["channel"]: c for c in channel_rows}
    google = by_channel.get(CX_GOOGLE_ADS)
    revenue_connected = bool(readiness.get("revenue_integration_connected"))

    # Scale — the spend-connected customer source (Google Ads) if it produced.
    if google and google["spend_connected"] and (google.get("customers") or 0) > 0:
        cards.append({
            "type": "scale", "title": "Scale",
            "headline": "Google Ads is the only spend-connected customer source",
            "body": (f"It produced {google['customers']} customer"
                     f"{'s' if google['customers'] != 1 else ''} and "
                     f"{_fmt_usd_short(google['won_revenue_usd'])} closed-won revenue "
                     "with verified spend."),
            "target_page": "roas-campaigns", "target_label": "Open ROAS by Campaign",
        })
    else:
        # Highlight the top revenue-only customer producer instead.
        producers = [c for c in channel_rows if not c["spend_connected"]
                     and (c.get("customers") or 0) > 0]
        top = max(producers, key=lambda c: (c["customers"], c.get("won_revenue_usd") or 0.0),
                  default=None)
        if top:
            cards.append({
                "type": "scale", "title": "Scale",
                "headline": f"{top['channel_label']} produced customers without spend visibility",
                "body": (f"Treat this as revenue attribution, not ROAS — "
                         f"{top['channel_label']} closed {top['customers']} customer"
                         f"{'s' if top['customers'] != 1 else ''}. Spend is unavailable "
                         "because it is not a connected spend source."),
                "target_page": "revenue-by-source", "target_label": "Open Revenue by Source",
            })
        else:
            cards.append({
                "type": "scale", "title": "Scale",
                "headline": "No spend-connected customer source yet",
                "body": "No channel has both verified spend and closed-won customers in this window.",
                "target_page": "roas-campaigns", "target_label": "Open ROAS by Campaign",
            })

    # Watch — a revenue-only channel with SQLs but no connected spend.
    watch = [c for c in channel_rows if not c["spend_connected"]
             and (c.get("sqls") or 0) > 0]
    if watch:
        top = max(watch, key=lambda c: c["sqls"])
        cards.append({
            "type": "watch", "title": "Watch",
            "headline": f"{top['channel_label']} has SQLs but no connected spend",
            "body": (f"It produced {top['sqls']} SQL{'s' if top['sqls'] != 1 else ''} and "
                     f"{top.get('customers') or 0} customer"
                     f"{'s' if (top.get('customers') or 0) != 1 else ''}, but ROAS is "
                     "unavailable until a real spend source is connected."),
            "target_page": "revenue-by-source", "target_label": "Open Revenue by Source",
        })
    else:
        cards.append({
            "type": "watch", "title": "Watch",
            "headline": "No unmeasured demand channels",
            "body": "Every channel producing SQLs either has connected spend or no customers yet.",
            "target_page": "revenue-by-source", "target_label": "Open Revenue by Source",
        })

    # Investigate — unclassified revenue.
    unclassified = by_channel.get(CX_UNCLASSIFIED)
    if unclassified and ((unclassified.get("customers") or 0) > 0
                         or (unclassified.get("won_revenue_usd") or 0) > 0):
        cards.append({
            "type": "investigate", "title": "Investigate",
            "headline": "Unclassified source revenue needs review",
            "body": (f"{_fmt_usd_short(unclassified.get('won_revenue_usd'))} closed-won revenue "
                     "is real but not safely mapped to a channel or platform."),
            "target_page": "revenue-health", "target_label": "Open Revenue Health",
        })
    else:
        cards.append({
            "type": "investigate", "title": "Investigate",
            "headline": "No unclassified revenue" if revenue_connected else "Attribution unavailable",
            "body": ("Every closed-won deal this window maps to a channel." if revenue_connected
                     else "Channel attribution cannot be verified without a connected revenue integration."),
            "target_page": "revenue-health", "target_label": "Open Revenue Health",
        })

    # Unavailable — non-Google spend is not connected (always true today).
    cards.append({
        "type": "unavailable", "title": "Unavailable",
        "headline": "Non-Google platform spend unavailable",
        "body": ("Only Google Ads spend is connected. Paid Social and Organic channels "
                 "are revenue-only in this dashboard — ROAS is intentionally withheld."),
        "target_page": "revenue-health", "target_label": "Open Revenue Health",
    })
    return cards


def _build_truth_status(mart: dict, google_spend: dict, channel_rows: list,
                        readiness: dict) -> dict:
    revenue = "ready" if readiness.get("revenue_integration_connected") else "blocked"

    fx = google_spend["fx_status"]
    cov = google_spend["campaign_spend_status"]
    if fx == "verified" and cov == "verified":
        google_ads_spend = "ready"
    elif cov == "incomplete" or fx == "incomplete":
        google_ads_spend = "partial"
    else:
        google_ads_spend = "unavailable"

    unclassified = next((c for c in channel_rows if c["channel"] == CX_UNCLASSIFIED), None)
    has_unclassified = bool(unclassified and ((unclassified.get("customers") or 0) > 0
                                              or (unclassified.get("won_revenue_usd") or 0) > 0
                                              or (unclassified.get("sqls") or 0) > 0))

    return {
        "source_attribution": "partial" if has_unclassified else "ready",
        "revenue": revenue,
        "google_ads_spend": google_ads_spend,
        # Non-Google spend is structurally not connected today — honest constant.
        "non_google_spend": "unavailable",
        "platform_classification": "partial" if has_unclassified else "ready",
    }


def _build_unavailable(kpis: dict, google_spend: dict, trend: dict,
                       readiness: dict, period_change: dict) -> list:
    out = []
    if not readiness.get("revenue_integration_connected"):
        out.append({"metric": "closed_won_revenue_usd",
                    "reason": "Revenue integration not connected for this window."})
    if kpis.get("total_sqls") is None:
        out.append({"metric": "total_sqls",
                    "reason": "Lead metrics withheld — business event dates unavailable."})
    if google_spend["usd"] is None:
        out.append({"metric": "google_ads_spend_usd",
                    "reason": "Google Ads USD spend requires verified spend coverage and FX."})
    # Non-Google spend/ROAS is structurally unavailable.
    out.append({"metric": "paid_social_roas",
                "reason": "Paid Social spend source is not connected."})
    out.append({"metric": "non_google_spend",
                "reason": "Only Google Ads spend is connected; other channels are revenue-only."})
    if trend.get("status") != "ready":
        out.append({"metric": "channel_trend",
                    "reason": trend.get("reason") or "Channel trend unavailable."})
    if not period_change.get("available"):
        out.append({"metric": "period_change",
                    "reason": period_change.get("reason") or "No comparison available."})
    return out


def _build_period_change(window: str, now: datetime, kpis: dict) -> dict:
    """Previous-period deltas for customers / revenue via a shifted canonical build."""
    ref = _previous_window_reference(window, now)
    if ref is None:
        return {"available": False, "label": None, "note": None, "metrics": {},
                "reason": "All Time has no previous period to compare against."}
    prev_key, prev_now, label, note = ref
    try:
        prev_mart = build_revenue_decision_mart(view="campaign", window=prev_key, now=prev_now)
        prev_source = build_revenue_by_source(prev_key, now=prev_now)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Dashboard channels previous-period build failed: %s", exc)
        return {"available": False, "label": label, "note": note, "metrics": {},
                "reason": "Previous period unavailable."}
    ps = prev_mart.get("summary") or {}
    connected = bool((prev_mart.get("readiness") or {}).get("revenue_integration_connected"))
    prev_rev = ps.get("won_revenue_usd") if connected else None
    prev_cust = ps.get("customers") if connected else None
    # SQLs on this tab are source-based (from the taxonomy), so the previous-period
    # baseline must be too — a campaign-mart SQL count is a different denominator.
    prev_sqls = sum((g.get("sqls") or 0) for g in (prev_source.get("groups") or []))
    metrics = {
        "closed_won_revenue_usd": _delta_metric(kpis.get("closed_won_revenue_usd"), prev_rev),
        "total_customers": _delta_metric(kpis.get("total_customers"), prev_cust),
        "total_sqls": _delta_metric(kpis.get("total_sqls"), prev_sqls),
    }
    return {
        "available": any(m["status"] == "ok" for m in metrics.values()),
        "label": label, "note": note, "metrics": metrics, "reason": None,
    }


def build_dashboard_channels(window: str = "current_quarter",
                             now: datetime | None = None) -> dict:
    """Build the Dashboard Channels & Platforms contract for one business window.

    Raises:
        ValueError: If ``window`` is not a supported business window.
    """
    resolve_canonical_window(window, now=now)
    ref_now = _coerce_utc(now)

    mart = build_revenue_decision_mart(view="campaign", window=window, now=now)
    window_block = mart.get("window") or {}
    readiness = mart.get("readiness") or {}
    revenue_connected = bool(readiness.get("revenue_integration_connected"))

    source = build_revenue_by_source(window, now=now)
    source_groups = source.get("groups") or []

    google_spend = _canonical_google_spend(mart)

    # Re-read the raw closed-won source rows to detect unknown deal amounts the
    # source service silently summed as $0 (same window bounds it resolved). If
    # this fetch is unavailable we cannot verify amounts are known, so revenue is
    # reported Unavailable rather than trusting a possibly-understated total.
    start_key = _parse_iso_date(window_block.get("start_date"))
    end_key = _parse_iso_date(window_block.get("end_date"))
    raw_revenue = _canonical_daily_revenue(start_key, end_key) if end_key else {
        "available": True, "rows": []}
    amounts_verifiable = bool(raw_revenue.get("available"))
    exec_unknown, platform_unknown = _unknown_amount_maps(raw_revenue.get("rows") or [])

    channel_rows, platforms = _build_channels_and_platforms(
        source_groups, google_spend, exec_unknown, platform_unknown,
        amounts_verifiable=amounts_verifiable)

    if not revenue_connected:
        # Closed-won customers AND revenue both come from the revenue integration,
        # so both are unknown when it is disconnected — blank them (Leads / SQLs
        # come from contacts and survive). Never imply a verified $0 / customer
        # count from an unwired integration. The hero KPIs derive from these rows,
        # so they become Unavailable in lockstep — no separate mart gate needed.
        # spend_connected/status stay as-is: the Google Ads SPEND source remains
        # connected when only the REVENUE integration is down. But ROAS needs
        # revenue, so roas and roas_available are cleared (kept consistent).
        for c in channel_rows:
            c["customers"] = None
            c["won_revenue_usd"] = None
            c["roas"] = None
            c["roas_available"] = False
        for p in platforms:
            p["customers"] = None
            p["won_revenue_usd"] = None
            p["roas"] = None
            p["roas_available"] = False

    # Hero KPIs derive from the channel rows so they reconcile with the mix /
    # matrix / trend (all Revenue-by-Source), never a mart total on a different
    # denominator (mart SQLs are campaign-based; these are source-based).
    # PR-ADS-154C-F3-F1 §1/§4. This page builds its totals from the source-group
    # taxonomy rather than the mart, so it had its own idea of when a revenue
    # total was publishable. It now also consults the ONE canonical verdict, and
    # republishes that verdict's reason and codes — so a reader gets the same
    # answer, and the same explanation, whichever page they are looking at.
    _canonical_base = canonical_revenue.load_won_deals(window, now=now)
    _revenue_verdict = canonical_revenue.revenue_total_publishable(
        _canonical_base, revenue_scope.SCOPE_ALL_SOURCE)
    kpis = _build_kpis(
        channel_rows, revenue_available=amounts_verifiable,
        revenue_total_available=bool(_revenue_verdict["publishable"]))
    top_channel, top_platform = _top_channel_and_platform(channel_rows, platforms)
    kpis["top_channel"] = top_channel
    kpis["top_platform"] = top_platform
    total = kpis.get("closed_won_revenue_usd")
    sc = kpis.get("spend_connected_revenue_usd")
    kpis["spend_connected_share"] = (
        round(sc / total, 4) if (sc is not None and total not in (None, 0)) else None
    )

    period_change = _build_period_change(window, ref_now, kpis)
    trend = _build_trend(window_block) if revenue_connected else {
        "bucket": None, "points": [], "status": "unavailable",
        "reason": "Revenue integration not connected — a channel series cannot be shown without fabricating zeros."}
    # The quality matrix plots customers (y) and revenue (bubble) — both unknown
    # without a connected revenue integration, so it is withheld rather than
    # plotting channels at a fabricated zero.
    quality_matrix = _build_quality_matrix(channel_rows) if revenue_connected else []
    decision_cards = _build_decision_cards(channel_rows, kpis, google_spend, readiness)
    truth_status = _build_truth_status(mart, google_spend, channel_rows, readiness)
    unavailable = _build_unavailable(kpis, google_spend, trend, readiness, period_change)

    # PR-ADS-154C-F2 §3: per-metric provenance. Until now this page was built by
    # the cross-page audit, had its window checked, and certified nothing — so it
    # could have published anything under an executive heading and passed.
    #
    # The declarations name the authority this page ACTUALLY reads. Revenue,
    # customers and SQLs here come from the source-group taxonomy
    # (`build_revenue_by_source`, itself the canonical deal ledger and lead
    # population grouped by CRM acquisition source), never from the mart totals —
    # which is why `total_sqls` is a source-group count and is registered as its
    # own identity rather than compared with the campaign-attributable one.
    _resolved_window = {
        "key": window_block.get("key"), "start_date": window_block.get("start_date"),
        "end_date": window_block.get("end_date"), "timezone": window_block.get("timezone"),
    }
    _mt = canonical_contract.metric_truth_block(_resolved_window, [
        {"metric": "closed_won_revenue_usd",
         "data_source": canonical_contract.SOURCE_REVENUE_BY_SOURCE,
         "scope": "all_source_business_revenue",
         "truth_status": (canonical_contract.TRUTH_READY
                          if kpis.get("closed_won_revenue_usd") is not None
                          else canonical_contract.TRUTH_NOT_READY),
         "unavailable_reason": _revenue_verdict["reason"],
         "violation_codes": _revenue_verdict["violation_codes"]},
        {"metric": "customers",
         "data_source": canonical_contract.SOURCE_REVENUE_BY_SOURCE,
         "scope": "all_source_business_revenue",
         "truth_status": (canonical_contract.TRUTH_READY
                          if kpis.get("total_customers") is not None
                          else canonical_contract.TRUTH_NOT_READY)},
        {"metric": "source_group_sqls",
         "data_source": canonical_contract.SOURCE_REVENUE_BY_SOURCE,
         "scope": "source_group_sqls",
         "truth_status": (canonical_contract.TRUTH_READY
                          if kpis.get("total_sqls") is not None
                          else canonical_contract.TRUTH_NOT_READY)},
    ])

    return {
        "window": window_block,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "read_only": True,
        "source_truth": "revenue_by_source_taxonomy",
        # This page reads canonical authorities only; declared so the audit's
        # fallback guard has something to check rather than an absent field it
        # would otherwise have to read as innocence.
        "legacy_fallback_used": False,
        "google_ads_conversion_value_used": False,
        canonical_contract.METRIC_TRUTH_KEY: _mt,
        "kpis": kpis,
        "period_change": period_change,
        "channel_mix": channel_rows,
        "platform_matrix": platforms,
        "trend": trend,
        "quality_matrix": quality_matrix,
        "decision_cards": decision_cards,
        "truth_status": truth_status,
        "readiness": readiness,
        "unavailable": unavailable,
    }
