"""
Executive Dashboard Overview (PR-ADS-134)

ONE read-only backend contract for the Dashboard → Overview command center.
It answers, in one payload: are we winning, wasting, or missing truth?

Composition doctrine — this service computes NO new business math. It composes
existing canonical read-only contracts:

  - build_revenue_decision_mart (view="campaign")  -> canonical window,
    spend_truth, summary, readiness, diagnostics, campaign rows. The mart is
    the single truth brain (PR-ADS-125); the overview never re-derives spend,
    FX, ROAS, or lead safety.
  - build_revenue_by_source                        -> source-group mix
    (google_ads / other_paid / organic / offline / unclassified, PR-ADS-133).
  - build_revenue_deals                            -> closed-won deal ledger
    (revenue trend buckets + best-country revenue grouping, PR-ADS-113).
  - repo.fetch_spend_daily_series                  -> per-day canonical spend
    (only consumed when the mart says the denominator is decision-ready).

Previous-period comparisons reuse the SAME canonical mart, resolved at a
shifted reference time (the mart's deterministic ``now`` seam) — never new SQL,
never a second implementation of window math:

  - current_quarter -> last_quarter (at the real now)
  - last_quarter    -> the quarter before last (last_quarter at a reference
                       date just inside the previous quarter)
  - last_6_months   -> the preceding 6 months
  - ytd             -> the same year-to-date span one year earlier
  - all_time        -> no comparison (there is no previous period)

Truth rules (unchanged, never loosened):
  - Google Ads is the ONLY spend-connected source; HubSpot closed-won deals are
    revenue truth; Google Ads conversion value is never used as revenue.
  - Native Google Ads currency is GBP; reporting revenue is USD; native GBP is
    never labelled USD.
  - No fake $0 / 0.00x / 0%: an unavailable metric is None plus a reason in
    ``unavailable`` — a missing previous period yields "No comparison", never 0%.
  - Read-only. NO writes to Google Ads, HubSpot, budgets, bids, campaigns,
    keywords, or offline conversions.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from dateutil.relativedelta import relativedelta

from analysis import revenue_scope
# PR-ADS-154C: THE canonical window anchor — the Google Ads account
# calendar day, not UTC. Sharing `resolve_window` while disagreeing about
# which day "today" is meant two pages could resolve `current_quarter` to
# different quarters across the account's midnight and both call it "this
# quarter". Spend is denominated in the account day, so the account day wins.
from services.canonical_contract import resolve_canonical_window
from services import canonical_contract
from db import revenue_repository as repo
from services import canonical_revenue_service as canonical_revenue
from services.revenue_attribution_service import build_revenue_deals
from services.revenue_decision_mart import build_revenue_decision_mart
from services.source_attribution_service import build_revenue_by_source

logger = logging.getLogger(__name__)

# Trend buckets: weekly for quarter-scale windows, monthly for longer spans.
_WEEKLY_BUCKET_MAX_DAYS = 200

# Metric keys exposed in period_change (and reused by the improving/declining
# signals). Values are human labels for signal copy.
PERIOD_CHANGE_METRICS = {
    "spend_usd": "Google Ads spend",
    "revenue_usd": "Closed-won revenue",
    "sqls": "SQLs",
    "customers": "Customers",
    "roas": "Google Ads ROAS",
}

# Source-group status copy for the channel mix. Only Google Ads is
# spend-connected; everything else is revenue/pipeline-only (PR-ADS-133).
_SOURCE_GROUP_STATUS = {
    "google_ads": "Spend-connected",
    "other_paid": "Revenue-only — no connected spend source",
    "organic": "Revenue-only — no connected spend source",
    "offline": "Imported CRM records — no reliable source attribution",
    "unclassified": "Needs review — source missing or unsafe",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_utc(now: datetime | None) -> datetime:
    """Coerce an optional reference time to timezone-aware UTC (mirrors
    analysis.business_windows) so quarter/month fields are read in UTC."""
    if now is None:
        return _utcnow()
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _round2(value):
    """Round to 2 dp, passing None straight through (never a fake 0)."""
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _rate(numerator, denominator):
    """numerator/denominator or None — never a fabricated 0 rate."""
    if numerator is None or denominator is None:
        return None
    try:
        denom = float(denominator)
        if denom <= 0:
            return None
        return round(float(numerator) / denom, 4)
    except (TypeError, ValueError):
        return None


def _quarter_start_month(month: int) -> int:
    return ((month - 1) // 3) * 3 + 1


def _previous_window_reference(window: str, now: datetime):
    """Map a business window to (window_key, shifted_now, label, note) or None.

    The previous period is always resolved by the SAME canonical window
    resolver, at a shifted reference time — one implementation of window math.
    ``all_time`` has no previous period.
    """
    if window == "current_quarter":
        return (
            "last_quarter",
            now,
            "vs Last Quarter",
            "Current quarter is in progress — compared against the full previous quarter.",
        )
    if window == "last_quarter":
        # A reference instant just inside the previous quarter resolves ITS
        # previous quarter (the quarter before last).
        current_q_start = date(now.year, _quarter_start_month(now.month), 1)
        ref_date = current_q_start - relativedelta(days=1)
        ref = datetime(ref_date.year, ref_date.month, ref_date.day, tzinfo=timezone.utc)
        return ("last_quarter", ref, "vs Prior Quarter", None)
    if window == "last_6_months":
        # Shift 6 months + 1 day so the two 6-month spans do not share a day.
        return (
            "last_6_months",
            now - relativedelta(months=6, days=1),
            "vs Prior 6 Months",
            None,
        )
    if window == "ytd":
        return ("ytd", now - relativedelta(years=1), "vs Prior Year (same period)", None)
    return None  # all_time — no previous period exists


def _delta_metric(current, previous):
    """One period-change metric — no comparison is stated, never faked as 0%."""
    if current is None or previous is None:
        return {
            "current": current,
            "previous": previous,
            "delta_pct": None,
            "direction": None,
            "status": "no_comparison",
            "reason": "Metric unavailable in one of the periods.",
        }
    try:
        cur = float(current)
        prev = float(previous)
    except (TypeError, ValueError):
        return {
            "current": current,
            "previous": previous,
            "delta_pct": None,
            "direction": None,
            "status": "no_comparison",
            "reason": "Metric not comparable.",
        }
    if prev == 0:
        return {
            "current": current,
            "previous": previous,
            "delta_pct": None,
            "direction": None,
            "status": "no_comparison",
            "reason": "No prior-period baseline (previous value is zero).",
        }
    pct = round((cur - prev) / abs(prev), 4)
    direction = "flat" if pct == 0 else ("up" if pct > 0 else "down")
    return {
        "current": current,
        "previous": previous,
        "delta_pct": pct,
        "direction": direction,
        "status": "ok",
        "reason": None,
    }


def _build_period_change(window: str, now: datetime, summary: dict) -> dict:
    """Previous-period deltas via a second canonical mart build (shifted now)."""
    ref = _previous_window_reference(window, now)
    if ref is None:
        return {
            "available": False,
            "label": None,
            "note": None,
            "previous_window": None,
            "metrics": {},
            "reason": "All Time has no previous period to compare against.",
        }

    prev_key, prev_now, label, note = ref
    try:
        prev_mart = build_revenue_decision_mart(
            view="campaign", window=prev_key, now=prev_now
        )
    except Exception as exc:  # noqa: BLE001 — comparison is optional, truth is not
        logger.warning("Dashboard overview previous-period mart failed: %s", exc)
        return {
            "available": False,
            "label": label,
            "note": note,
            "previous_window": None,
            "metrics": {},
            "reason": "Previous period unavailable.",
        }

    prev_summary = prev_mart.get("summary") or {}
    metrics = {
        "spend_usd": _delta_metric(summary.get("spend_usd"), prev_summary.get("spend_usd")),
        "revenue_usd": _delta_metric(
            summary.get("won_revenue_usd"), prev_summary.get("won_revenue_usd")
        ),
        "sqls": _delta_metric(summary.get("sqls"), prev_summary.get("sqls")),
        "customers": _delta_metric(summary.get("customers"), prev_summary.get("customers")),
        "roas": _delta_metric(summary.get("roas"), prev_summary.get("roas")),
    }
    prev_window_block = prev_mart.get("window") or {}
    return {
        "available": any(m["status"] == "ok" for m in metrics.values()),
        "label": label,
        "note": note,
        "previous_window": {
            "key": prev_window_block.get("key"),
            "label": prev_window_block.get("label"),
            "start_date": prev_window_block.get("start_date"),
            "end_date": prev_window_block.get("end_date"),
        },
        "metrics": metrics,
        "reason": None,
    }


def _parse_iso_date(value):
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _bucket_start(day: date, bucket: str) -> date:
    if bucket == "month":
        return date(day.year, day.month, 1)
    # ISO week — Monday
    return day - relativedelta(days=day.weekday())


def _bucket_starts(start: date, end: date, bucket: str) -> list[date]:
    """Every bucket start between start and end (inclusive), clipped to start."""
    starts: list[date] = []
    step = relativedelta(months=1) if bucket == "month" else relativedelta(days=7)
    cursor = _bucket_start(start, bucket)
    while cursor <= end:
        clipped = max(cursor, start)
        if not starts or starts[-1] != clipped:
            starts.append(clipped)
        cursor = _bucket_start(cursor + step, bucket)
    return starts


def _build_trend(window_block: dict, readiness: dict, spend_truth: dict,
                 deals_contract: dict) -> dict:
    """Spend/revenue/customers per bucket — partial and honest, never fabricated.

    - The spend series is emitted ONLY when the mart says the canonical spend
      denominator is decision-ready (coverage complete + FX verified). Inside a
      fully verified window, a day with no spend rows is a VERIFIED zero; in any
      other state, absent days are unknown and the whole series is withheld.
    - The revenue series is emitted only when the deal ledger is readable AND
      the revenue integration is connected; buckets then count real closed-won
      deals (a week with no closed deals is genuinely $0, not fabricated). A
      readable-but-unwired ledger must never render as a flat $0 series.
    """
    start = _parse_iso_date(window_block.get("start_date"))
    end = _parse_iso_date(window_block.get("end_date"))

    spend_ready = bool(readiness.get("spend_decision_ready"))
    ledger_status = (deals_contract.get("source_health") or {}).get("ledger_status")
    revenue_connected = bool(readiness.get("revenue_integration_connected"))
    revenue_ready = ledger_status == "available" and revenue_connected

    spend_reason = None
    if not spend_ready:
        if spend_truth.get("fx_status") != "verified":
            spend_reason = "FX coverage incomplete — daily USD spend is withheld."
        else:
            spend_reason = "Canonical spend coverage incomplete — missing days are unknown, not $0."

    if revenue_ready:
        revenue_reason = None
    elif ledger_status != "available":
        revenue_reason = "Closed-won deal ledger unavailable."
    else:
        revenue_reason = "Revenue integration not connected — a $0 series would be fabricated."

    deal_rows = deals_contract.get("deals") or [] if revenue_ready else []

    spend_rows = []
    if spend_ready:
        series = repo.fetch_spend_daily_series(start, end)
        if series.get("available"):
            spend_rows = series.get("rows") or []
            # Fail closed: inside a verified window every day must carry USD.
            # A day whose FX is somehow missing is withheld with the whole
            # series — never silently coerced to $0.
            if any(r.get("spend_usd") is None for r in spend_rows):
                spend_ready = False
                spend_rows = []
                spend_reason = ("Daily FX coverage is inconsistent — the spend "
                                "series is withheld rather than shown with $0 gaps.")
        else:
            spend_ready = False
            spend_reason = "Canonical spend table unavailable."

    # all_time has no lower bound — anchor to the earliest real data point.
    if start is None:
        candidates = [_parse_iso_date(r.get("spend_date")) for r in spend_rows]
        candidates += [_parse_iso_date(d.get("deal_close_date")) for d in deal_rows]
        candidates = [c for c in candidates if c is not None]
        if not candidates or end is None:
            return {
                "bucket": None,
                "points": [],
                "spend_status": "ready" if spend_ready else "unavailable",
                "spend_reason": spend_reason,
                "revenue_status": "ready" if revenue_ready else "unavailable",
                "revenue_reason": revenue_reason,
            }
        start = min(candidates)

    if end is None or start > end:
        return {
            "bucket": None,
            "points": [],
            "spend_status": "ready" if spend_ready else "unavailable",
            "spend_reason": spend_reason,
            "revenue_status": "ready" if revenue_ready else "unavailable",
            "revenue_reason": revenue_reason,
        }

    span_days = (end - start).days
    bucket = "week" if span_days <= _WEEKLY_BUCKET_MAX_DAYS else "month"

    spend_by_bucket: dict[date, float] = {}
    for row in spend_rows:
        day = _parse_iso_date(row.get("spend_date"))
        if day is None or day < start or day > end:
            continue
        key = max(_bucket_start(day, bucket), start)
        spend_by_bucket[key] = spend_by_bucket.get(key, 0.0) + float(row.get("spend_usd") or 0.0)

    revenue_by_bucket: dict[date, float] = {}
    customers_by_bucket: dict[date, int] = {}
    for deal in deal_rows:
        day = _parse_iso_date(deal.get("deal_close_date"))
        if day is None or day < start or day > end:
            continue
        key = max(_bucket_start(day, bucket), start)
        revenue_by_bucket[key] = revenue_by_bucket.get(key, 0.0) + float(
            deal.get("deal_amount_usd") or 0.0
        )
        customers_by_bucket[key] = customers_by_bucket.get(key, 0) + 1

    points = []
    for bstart in _bucket_starts(start, end, bucket):
        points.append({
            "period_start": bstart.isoformat(),
            # None (not 0) whenever the series itself is withheld.
            "spend_usd": _round2(spend_by_bucket.get(bstart, 0.0)) if spend_ready else None,
            "revenue_usd": _round2(revenue_by_bucket.get(bstart, 0.0)) if revenue_ready else None,
            "customers": customers_by_bucket.get(bstart, 0) if revenue_ready else None,
        })

    return {
        "bucket": bucket,
        "points": points,
        "spend_status": "ready" if spend_ready else "unavailable",
        "spend_reason": spend_reason,
        "revenue_status": "ready" if revenue_ready else "unavailable",
        "revenue_reason": revenue_reason,
    }


def _build_source_mix(source_groups: list, *, revenue_connected: bool) -> list:
    """Compact channel-mix rows from the canonical source groups (PR-ADS-133).

    No ROAS on any mix row — including Google Ads. The source-page group ROAS
    is a geo-spend ratio that is NOT gated by the mart's coverage/FX safety, so
    surfacing it here could contradict the hero ROAS card on the same screen.
    The hero KPI (canonical mart ROAS) is the only ROAS this page shows.

    When the revenue integration is not connected, per-source revenue/customers
    are unknown — None, never a fabricated $0.
    """
    mix = []
    for group in source_groups or []:
        key = group.get("group")
        mix.append({
            "key": key,
            "label": group.get("label"),
            "leads": group.get("leads"),
            "sqls": group.get("sqls"),
            "customers": group.get("customers") if revenue_connected else None,
            "revenue_usd": _round2(group.get("won_revenue")) if revenue_connected else None,
            # Only Google Ads is spend-connected; ROAS never appears on mix rows.
            "spend_connected": bool(group.get("has_spend")),
            "roas": None,
            "status": _SOURCE_GROUP_STATUS.get(key, "Revenue-only"),
        })
    return mix


def _best_row(rows: list, *, revenue_key: str) -> dict | None:
    scored = [r for r in rows if (r.get("customers") or 0) > 0 or (r.get(revenue_key) or 0) > 0]
    if not scored:
        return None
    return max(scored, key=lambda r: (float(r.get(revenue_key) or 0.0), r.get("customers") or 0))


def _country_revenue_rows(deal_rows: list) -> list:
    """Group ledger deals by country — trivial aggregation of canonical rows."""
    by_country: dict[str, dict] = {}
    for deal in deal_rows or []:
        country = (deal.get("country") or "").strip() or "Unattributed"
        row = by_country.setdefault(country, {"country": country, "customers": 0, "won_revenue": 0.0})
        row["customers"] += 1
        row["won_revenue"] += float(deal.get("deal_amount_usd") or 0.0)
    return list(by_country.values())


def _build_signals(campaign_rows: list, deal_rows: list, source_mix: list,
                   summary: dict, spend_truth: dict, period_change: dict) -> dict:
    """Ranked top signals, each computed from real canonical rows."""
    sqls_available = summary.get("sqls") is not None

    best_campaign = _best_row(campaign_rows or [], revenue_key="won_revenue")
    best_campaign_signal = None
    if best_campaign:
        best_campaign_signal = {
            "label": best_campaign.get("campaign_name"),
            "revenue_usd": _round2(best_campaign.get("won_revenue")),
            "customers": best_campaign.get("customers"),
            "roas": best_campaign.get("roas"),
            "target_page": "roas-campaigns",
        }

    best_country = _best_row(_country_revenue_rows(deal_rows), revenue_key="won_revenue")
    best_country_signal = None
    if best_country:
        best_country_signal = {
            "label": best_country.get("country"),
            "revenue_usd": _round2(best_country.get("won_revenue")),
            "customers": best_country.get("customers"),
            "target_page": "roas-countries",
        }

    source_candidates = [s for s in source_mix if (s.get("revenue_usd") or 0) > 0]
    best_source_signal = None
    if source_candidates:
        top = max(source_candidates, key=lambda s: float(s.get("revenue_usd") or 0.0))
        best_source_signal = {
            "label": top.get("label"),
            "revenue_usd": top.get("revenue_usd"),
            "customers": top.get("customers"),
            "status": "Spend-connected" if top.get("spend_connected") else "Revenue-only",
            "target_page": "revenue-by-source",
        }

    # Waste: campaigns with real matched spend and zero SQLs/customers. Row spend
    # is USD only when FX is verified (revenue_attribution emits native GBP
    # otherwise) — the currency is carried explicitly, never assumed.
    biggest_waste_signal = None
    if sqls_available:
        waste_rows = [
            r for r in campaign_rows or []
            if (r.get("spend") or 0) > 0
            and (r.get("sqls") or 0) == 0
            and (r.get("customers") or 0) == 0
        ]
        if waste_rows:
            waste_total = sum(float(r.get("spend") or 0.0) for r in waste_rows)
            top_waste = max(waste_rows, key=lambda r: float(r.get("spend") or 0.0))
            biggest_waste_signal = {
                "label": "Spend with no SQL or customer proof",
                "value": _round2(waste_total),
                "currency": ("USD" if spend_truth.get("fx_status") == "verified"
                             else (spend_truth.get("native_currency") or "GBP")),
                "campaign_count": len(waste_rows),
                "top_campaign": top_waste.get("campaign_name"),
                "severity": "high" if waste_total > 0 else "low",
                # Evidence lives in ROAS by Campaign (its Waste decision filter
                # shows exactly these campaigns on the same business window).
                # The #/waste page is ad-window search-term waste and cannot
                # evidence this campaign-level signal; the Investigate decision
                # card routes to the Action Queue.
                "target_page": "roas-campaigns",
            }

    fastest_improving = None
    biggest_decline = None
    # Spend is valence-neutral (more spend is not "improving" without revenue
    # proof), so it never competes for improving/declining.
    comparable = {
        key: m for key, m in (period_change.get("metrics") or {}).items()
        if key != "spend_usd" and m.get("status") == "ok" and m.get("delta_pct") is not None
    }
    if comparable:
        best_key = max(comparable, key=lambda k: comparable[k]["delta_pct"])
        worst_key = min(comparable, key=lambda k: comparable[k]["delta_pct"])
        if comparable[best_key]["delta_pct"] > 0:
            fastest_improving = {
                "metric": best_key,
                "label": PERIOD_CHANGE_METRICS.get(best_key, best_key),
                "delta_pct": comparable[best_key]["delta_pct"],
                "target_page": "revenue-by-source",
            }
        if comparable[worst_key]["delta_pct"] < 0:
            biggest_decline = {
                "metric": worst_key,
                "label": PERIOD_CHANGE_METRICS.get(worst_key, worst_key),
                "delta_pct": comparable[worst_key]["delta_pct"],
                "target_page": "revenue-by-source",
            }

    return {
        "best_campaign": best_campaign_signal,
        "best_country": best_country_signal,
        "best_source": best_source_signal,
        "biggest_waste": biggest_waste_signal,
        "fastest_improving": fastest_improving,
        "biggest_decline": biggest_decline,
    }


def _fmt_usd_short(value) -> str:
    """Compact USD copy for computed card text (e.g. $18.0k)."""
    v = float(value or 0.0)
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if abs(v) >= 1_000:
        return f"${v / 1_000:.1f}k"
    return f"${v:.0f}"


_NATIVE_SYMBOLS = {"GBP": "£", "EUR": "€", "USD": "$"}


def _fmt_native_short(value, currency: str) -> str:
    """Compact native copy with an explicit currency code (e.g. £5.7k GBP).

    Native spend is never rendered with a bare "$" — the code always rides
    along so GBP can never masquerade as USD.
    """
    code = (currency or "GBP").upper()
    sym = _NATIVE_SYMBOLS.get(code, "")
    v = float(value or 0.0)
    if abs(v) >= 1_000_000:
        body = f"{v / 1_000_000:.1f}M"
    elif abs(v) >= 1_000:
        body = f"{v / 1_000:.1f}k"
    else:
        body = f"{v:.0f}"
    return f"{sym}{body} {code}"


def _build_decision_cards(campaign_rows: list, summary: dict, readiness: dict,
                          spend_truth: dict, signals: dict, source_mix: list) -> list:
    """Four executive decision cards computed from real data — never generic."""
    cards = []
    sqls_available = summary.get("sqls") is not None

    revenue_connected = bool(readiness.get("revenue_integration_connected"))
    producing = [r for r in campaign_rows or [] if (r.get("customers") or 0) > 0]
    best = signals.get("best_campaign")
    if not revenue_connected:
        # Customer production is UNKNOWN, not zero — say so instead of
        # claiming "no campaign produced a customer".
        cards.append({
            "type": "scale",
            "title": "Scale",
            "headline": "Customer truth unavailable",
            "body": "Revenue integration is not connected, so customer production cannot be verified for this window.",
            "target_page": "revenue-health",
            "target_label": "Open Revenue Health",
        })
    elif producing and best:
        n = len(producing)
        cards.append({
            "type": "scale",
            "title": "Scale",
            "headline": f"{n} campaign{'s' if n != 1 else ''} producing customers",
            "body": (
                f"{best['label']} produced {best['customers']} customer"
                f"{'s' if (best['customers'] or 0) != 1 else ''} and "
                f"{_fmt_usd_short(best['revenue_usd'])} closed-won revenue in this window."
            ),
            "target_page": "roas-campaigns",
            "target_label": "Open ROAS by Campaign",
        })
    else:
        cards.append({
            "type": "scale",
            "title": "Scale",
            "headline": "No verified customer producers yet",
            "body": "No campaign has a closed-won customer in this window.",
            "target_page": "roas-campaigns",
            "target_label": "Open ROAS by Campaign",
        })

    if sqls_available:
        watch_rows = [
            r for r in campaign_rows or []
            if (r.get("sqls") or 0) > 0 and (r.get("customers") or 0) == 0
        ]
        if watch_rows:
            top_watch = max(watch_rows, key=lambda r: r.get("sqls") or 0)
            n = len(watch_rows)
            cards.append({
                "type": "watch",
                "title": "Watch",
                "headline": f"{n} campaign{'s' if n != 1 else ''} with SQLs but no customers yet",
                "body": (
                    f"{top_watch.get('campaign_name')} has {top_watch.get('sqls')} SQL"
                    f"{'s' if (top_watch.get('sqls') or 0) != 1 else ''} without "
                    "closed-won proof — promising, not yet verified."
                ),
                "target_page": "roas-campaigns",
                "target_label": "Open ROAS by Campaign",
            })
        else:
            cards.append({
                "type": "watch",
                "title": "Watch",
                "headline": "No unproven SQL pipelines",
                "body": "Every campaign with SQLs also has closed-won customers in this window.",
                "target_page": "revenue-by-source",
                "target_label": "Open Revenue by Source",
            })
    else:
        cards.append({
            "type": "watch",
            "title": "Watch",
            "headline": "Lead metrics withheld",
            "body": "SQL truth is unavailable for this window, so watch-list campaigns cannot be ranked.",
            "target_page": "revenue-health",
            "target_label": "Open Revenue Health",
        })

    waste = signals.get("biggest_waste")
    if waste:
        value_copy = (
            _fmt_usd_short(waste["value"]) if waste.get("currency") == "USD"
            else _fmt_native_short(waste["value"], waste.get("currency"))
        )
        cards.append({
            "type": "investigate",
            "title": "Investigate",
            "headline": "Paid spend without SQL proof",
            "body": (
                f"{value_copy} across {waste['campaign_count']} campaign"
                f"{'s' if waste['campaign_count'] != 1 else ''} has no SQL or "
                "customer evidence in this window."
            ),
            "target_page": "action-queue",
            "target_label": "Open Action Queue",
        })
    elif not sqls_available:
        cards.append({
            "type": "investigate",
            "title": "Investigate",
            "headline": "Waste check unavailable",
            "body": "Lead metrics are withheld, so spend-without-SQL waste cannot be verified.",
            "target_page": "revenue-health",
            "target_label": "Open Revenue Health",
        })
    else:
        cards.append({
            "type": "investigate",
            "title": "Investigate",
            "headline": "No unexplained paid spend",
            "body": "Every campaign with spend has SQL or customer evidence in this window.",
            "target_page": "action-queue",
            "target_label": "Open Action Queue",
        })

    blockers = []
    if spend_truth.get("fx_status") != "verified":
        blockers.append("FX coverage")
    if spend_truth.get("campaign_spend_status") != "verified":
        blockers.append("spend coverage")
    if not readiness.get("revenue_integration_connected"):
        blockers.append("revenue integration")
    if not readiness.get("lead_metrics_ready"):
        blockers.append("lead event dates")
    # Same unclassified test as truth_status.source_attribution — the footer
    # chips and this card must never disagree.
    unclassified = next((s for s in source_mix if s.get("key") == "unclassified"), None)
    if unclassified and ((unclassified.get("customers") or 0) > 0
                        or (unclassified.get("revenue_usd") or 0) > 0
                        or (unclassified.get("leads") or 0) > 0):
        blockers.append("unclassified sources need review")
    if blockers:
        cards.append({
            "type": "unavailable",
            "title": "Unavailable",
            "headline": f"{len(blockers)} truth blocker{'s' if len(blockers) != 1 else ''}",
            "body": "Blocked: " + ", ".join(blockers) + ".",
            "target_page": "revenue-health",
            "target_label": "Open Revenue Health",
        })
    else:
        cards.append({
            "type": "unavailable",
            "title": "Unavailable",
            "headline": "All truth sources verified",
            "body": "Spend, FX, revenue, and lead truth are verified for this window.",
            "target_page": "revenue-health",
            "target_label": "Open Revenue Health",
        })

    return cards


def _build_truth_status(spend_truth: dict, readiness: dict, source_mix: list,
                        ledger_status: str | None = None) -> dict:
    """Compact truth badges for the command bar / footer."""
    def _map3(status, ready="verified", partial=("incomplete",)):
        if status == ready:
            return "ready"
        if status in partial:
            return "partial"
        return "unavailable"

    spend = _map3(spend_truth.get("campaign_spend_status"))
    fx = _map3(spend_truth.get("fx_status"))
    if not readiness.get("revenue_integration_connected"):
        revenue = "blocked"
    elif ledger_status is not None and ledger_status != "available":
        # Connected, but the closed-won deal ledger could not be read — the
        # trend withholds its revenue series in exactly this state, so the
        # revenue chip must not claim "ready" (that would hide a real outage).
        revenue = "partial"
    else:
        revenue = "ready"

    geo_status = spend_truth.get("country_spend_status")
    if geo_status in ("verified", "reconciled_with_residual"):
        geo = "ready"
    elif geo_status == "mismatch":
        geo = "blocked"
    else:
        geo = "unavailable"

    unclassified = next((s for s in source_mix if s.get("key") == "unclassified"), None)
    has_unclassified = bool(unclassified and (
        (unclassified.get("customers") or 0) > 0
        or (unclassified.get("revenue_usd") or 0) > 0
        or (unclassified.get("leads") or 0) > 0
    ))
    source_attribution = "partial" if has_unclassified else "ready"

    if revenue == "blocked":
        overall = "blocked"
    elif (revenue == "ready" and spend == "ready" and fx == "ready"
          and source_attribution == "ready"):
        overall = "ready"
    else:
        overall = "partial"

    return {
        "overall": overall,
        "spend": spend,
        "revenue": revenue,
        "fx": fx,
        "source_attribution": source_attribution,
        "geo": geo,
    }


def _build_unavailable(summary: dict, spend_truth: dict, readiness: dict,
                       period_change: dict, trend: dict) -> list:
    """Explicit metric -> reason list. Nothing missing is ever rendered as 0."""
    out = []
    if summary.get("spend_usd") is None:
        if spend_truth.get("fx_status") != "verified":
            out.append({"metric": "google_ads_spend_usd",
                        "reason": "FX coverage incomplete — USD spend withheld."})
        else:
            out.append({"metric": "google_ads_spend_usd",
                        "reason": "Canonical spend coverage incomplete or unavailable."})
    if summary.get("roas") is None:
        out.append({"metric": "google_ads_roas",
                    "reason": "ROAS requires verified spend, complete FX, and closed-won revenue."})
    if summary.get("leads") is None or summary.get("sqls") is None:
        out.append({"metric": "leads_sqls",
                    "reason": "Lead metrics withheld — business event dates unavailable."})
    if not readiness.get("revenue_integration_connected"):
        out.append({"metric": "closed_won_revenue_usd",
                    "reason": "Revenue integration not connected for this window."})
    if not period_change.get("available"):
        out.append({"metric": "period_change",
                    "reason": period_change.get("reason") or "No comparison available."})
    if trend.get("spend_status") != "ready":
        out.append({"metric": "trend_spend",
                    "reason": trend.get("spend_reason") or "Spend series unavailable."})
    if trend.get("revenue_status") != "ready":
        out.append({"metric": "trend_revenue",
                    "reason": trend.get("revenue_reason") or "Revenue series unavailable."})
    return out


def build_dashboard_overview(window: str = "current_quarter",
                             now: datetime | None = None) -> dict:
    """Build the Executive Overview contract for one business window.

    Args:
        window: A business-window key (analysis.business_windows.WINDOW_KEYS).
        now: Optional reference time for deterministic resolution/testing.

    Returns:
        The compact executive contract described in the module docstring.

    Raises:
        ValueError: If ``window`` is not a supported business window.
    """
    # Validate up front so a bad window fails identically to every mart page.
    resolve_canonical_window(window, now=now)
    ref_now = _coerce_utc(now)

    mart = build_revenue_decision_mart(view="campaign", window=window, now=now)
    window_block = mart.get("window") or {}
    spend_truth = mart.get("spend_truth") or {}
    summary = mart.get("summary") or {}
    readiness = mart.get("readiness") or {}
    campaign_rows = mart.get("rows") or []

    # When the revenue integration is not connected, closed-won revenue and
    # customer counts are UNKNOWN — the mart's 0.0/0 aggregates over an unwired
    # ledger must never surface as real numbers (no fake $0).
    revenue_connected = bool(readiness.get("revenue_integration_connected"))
    won_revenue_usd = summary.get("won_revenue_usd") if revenue_connected else None
    customers = summary.get("customers") if revenue_connected else None
    roas = summary.get("roas") if revenue_connected else None
    gated_summary = {
        **summary,
        "won_revenue_usd": won_revenue_usd,
        "customers": customers,
        "roas": roas,
    }

    source_contract = build_revenue_by_source(window, now=now)
    source_mix = _build_source_mix(source_contract.get("groups") or [],
                                   revenue_connected=revenue_connected)

    deals_contract = build_revenue_deals(window, now=now)
    deal_rows = deals_contract.get("deals") or []

    period_change = _build_period_change(window, ref_now, gated_summary)
    trend = _build_trend(window_block, readiness, spend_truth, deals_contract)
    signals = _build_signals(campaign_rows, deal_rows, source_mix, gated_summary,
                             spend_truth, period_change)
    decision_cards = _build_decision_cards(campaign_rows, gated_summary, readiness,
                                           spend_truth, signals, source_mix)
    truth_status = _build_truth_status(
        spend_truth, readiness, source_mix,
        ledger_status=(deals_contract.get("source_health") or {}).get("ledger_status"),
    )

    # PR-ADS-152 §1: the Dashboard SQL headline is the canonical
    # google_ads_source_sqls (deduplicated, exclusion-filtered, staleness-aware,
    # real campaign identity) — NOT the mart's campaign-attributable ``sqls`` under
    # a bare "SQLs" label. The campaign-attributable subset is a secondary
    # disclosure. The page displays the canonical count verbatim, so consumer_count
    # is None (never summary.sqls, which is a different, narrower scope).
    from services import canonical_contact_outcome_service as _canon  # noqa: PLC0415
    sql_reconciliation = _canon.page_reconciliation(
        _canon.WINDOW_BUSINESS, window, _canon.SCOPE_GOOGLE_ADS_SOURCE, now=now)
    _recon_status = sql_reconciliation.get("reconciliation_status")
    # A mismatch is never rendered as a normal count — the headline is withheld.
    _ga_source_display = (None if _recon_status == _canon.STATUS_MISMATCH
                          else sql_reconciliation.get("google_ads_source_sqls"))

    # PR-ADS-153C §21/§22: CRM lifecycle metrics now come from the canonical
    # HubSpot funnel (stage-entry events, each dated by its OWN
    # hs_v2_date_entered_* timestamp) rather than the legacy contact-created-date
    # proxy. REVENUE metrics (customers / closed-won revenue / ROAS) deliberately
    # stay on their existing revenue contract until PR-ADS-153E — a lifecycle
    # customer is NOT a revenue customer and must never be silently substituted.
    lifecycle_funnel = _lifecycle_funnel_block(window, now=now)

    kpis = {
        # USD spend is strictly canonical: None whenever FX/coverage is unsafe.
        "google_ads_spend_usd": summary.get("spend_usd"),
        "native_spend": {
            "amount": spend_truth.get("native_spend"),
            "currency": spend_truth.get("native_currency") or "GBP",
        },
        "closed_won_revenue_usd": won_revenue_usd,
        "google_ads_roas": roas,
        "leads": summary.get("leads"),
        # PR-ADS-152 §1 — the SQL headline: canonical Google Ads-source SQLs.
        "google_ads_source_sqls": _ga_source_display,
        "google_ads_source_sqls_scope": _canon.SCOPE_GOOGLE_ADS_SOURCE,
        "google_ads_source_sqls_status": _recon_status,
        # Secondary disclosure — the campaign-attributable subset (the old mart
        # count), now under an explicitly named field, never a bare "SQLs" KPI.
        "campaign_attributable_sqls": sql_reconciliation.get("campaign_attributable_sqls"),
        # Back-compat: the mart's campaign-attributable count, explicitly scoped.
        "sqls": summary.get("sqls"),
        "sqls_scope": _canon.SCOPE_CAMPAIGN_ATTRIBUTABLE,
        "customers": customers,
        "sql_rate": _rate(summary.get("sqls"), summary.get("leads")),
        "customer_rate": _rate(customers, summary.get("leads")),

        # ── PR-ADS-153C: canonical CRM lifecycle funnel ──────────────────────
        # Each count is a stage-ENTRY event on its own event date. These are the
        # values the Dashboard funnel strip renders; `sqls` above remains the
        # explicitly-scoped campaign-attributable back-compat field.
        "lifecycle_leads": lifecycle_funnel.get("lead"),
        "lifecycle_mqls": lifecycle_funnel.get("mql"),
        "lifecycle_sqls": lifecycle_funnel.get("sql"),
        "lifecycle_opportunities": lifecycle_funnel.get("opportunity"),
        # Named "lifecycle_customers", never "customers" — the revenue customer
        # KPI above is a different fact (closed-won deals) owned by PR-ADS-153E.
        "lifecycle_customers": lifecycle_funnel.get("customer"),
        "lifecycle_scope": lifecycle_funnel.get("scope"),
        "lifecycle_available": lifecycle_funnel.get("available"),
        "lifecycle_status": lifecycle_funnel.get("status"),
    }

    # PR-ADS-154C-F1: per-metric provenance. This page publishes Google Ads
    # spend AND HubSpot revenue, so a single response-level `source_truth` is
    # wrong about one of them whichever value it takes. Each metric now states
    # its own lineage, and the cross-page audit checks those statements against
    # its registry instead of echoing the source it expected to find.
    _resolved_window = {
        "key": window_block.get("key"), "start_date": window_block.get("start_date"),
        "end_date": window_block.get("end_date"), "timezone": window_block.get("timezone"),
    }
    _spend_ready = (spend_truth.get("campaign_spend_status") == "verified")
    # PR-ADS-154C-F3: the population being readable (`revenue_available`) and its
    # TOTAL being known (`revenue_total_available`) are different facts. The count
    # is complete whatever the amounts are; the total is not, when any deal's
    # currency was never proven. Two flags, so a withheld total can never be
    # published under a `ready` contract.
    _mart_summary = mart.get("summary") or {}
    _population_ready = bool(_mart_summary.get("revenue_available"))
    _revenue_ready = bool(_mart_summary.get("revenue_total_available"))
    # The mart's lead/SQL population is withheld wholesale when the business event
    # date is unsafe, so "did it publish a count" is the readiness question.
    _lead_ready = (summary.get("leads") is not None or summary.get("sqls") is not None)
    # PR-ADS-153C: the lifecycle strip is a DIFFERENT authority answering a
    # different question, on each stage's own event date. It gets its own status.
    _lifecycle_ready = bool(lifecycle_funnel.get("available")) and \
        lifecycle_funnel.get("status") != "mismatch"
    _lifecycle_status = (canonical_contract.TRUTH_READY if _lifecycle_ready
                         else canonical_contract.TRUTH_NOT_READY)
    metric_truth = canonical_contract.metric_truth_block(_resolved_window, [
        {"metric": "google_ads_spend_usd",
         "data_source": canonical_contract.SOURCE_CANONICAL_SPEND,
         "scope": "google_ads_campaign_spend",
         "truth_status": (canonical_contract.TRUTH_READY if _spend_ready
                          else canonical_contract.TRUTH_NOT_READY),
         "customer_id": spend_truth.get("customer_id")},
        {"metric": "closed_won_revenue_usd",
         "data_source": canonical_contract.SOURCE_REVENUE_DECISION_MART,
         "scope": "all_source_business_revenue",
         "truth_status": (canonical_contract.TRUTH_READY if _revenue_ready
                          else canonical_contract.TRUTH_NOT_READY),
         "unavailable_reason": _mart_summary.get("revenue_total_unavailable_reason")},
        {"metric": "customers",
         "data_source": canonical_contract.SOURCE_REVENUE_DECISION_MART,
         "scope": "all_source_business_revenue",
         "truth_status": (canonical_contract.TRUTH_READY if _population_ready
                          else canonical_contract.TRUTH_NOT_READY)},
        {"metric": "campaign_attributable_sqls",
         "data_source": canonical_contract.SOURCE_REVENUE_DECISION_MART,
         "scope": "campaign_attributable_sqls",
         "truth_status": (canonical_contract.TRUTH_READY if _lead_ready
                          else canonical_contract.TRUTH_NOT_READY)},
        {"metric": "campaign_attributable_leads",
         "data_source": canonical_contract.SOURCE_REVENUE_DECISION_MART,
         "scope": "campaign_attributable_leads",
         "truth_status": (canonical_contract.TRUTH_READY if _lead_ready
                          else canonical_contract.TRUTH_NOT_READY)},
        *[{"metric": f"lifecycle_{_stage}",
           "data_source": canonical_contract.SOURCE_CANONICAL_FUNNEL,
           "scope": f"lifecycle_{_stage}",
           "truth_status": _lifecycle_status}
          for _stage in ("leads", "mqls", "sqls", "opportunities", "customers")],
    ])

    return {
        "window": window_block,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "read_only": True,
        "truth_status": truth_status,
        canonical_contract.METRIC_TRUTH_KEY: metric_truth,
        "kpis": kpis,
        "period_change": period_change,
        "trend": trend,
        "source_mix": source_mix,
        "top_signals": signals,
        "decision_cards": decision_cards,
        "readiness": readiness,
        "diagnostics": mart.get("diagnostics") or [],
        "unavailable": _build_unavailable(gated_summary, spend_truth, readiness,
                                          period_change, trend),
        "source_truth": "revenue_decision_mart",
        # PR-ADS-153E-B: the Overview's customers and revenue reach it through
        # the mart, which reads the canonical deal ledger at `all_source` scope.
        # The provenance is declared here so the page can never be read as
        # showing an advertising subset under a company-wide heading.
        "revenue_source": canonical_revenue.CANONICAL_SOURCE,
        "revenue_scope": revenue_scope.SCOPE_ALL_SOURCE,
        "revenue_available": bool((mart.get("summary") or {}).get("revenue_available")),
        "legacy_fallback_used": False,
        "google_ads_conversion_value_used": False,
        # PR-ADS-152: canonical SQL-scope reconciliation metadata (§7). Its
        # google_ads_source_sqls equals the Revenue by Source Google Ads SQL count
        # (both read the one canonical population) — acceptance criterion #1.
        "sql_reconciliation": sql_reconciliation,
        # PR-ADS-153C: canonical lifecycle funnel + cohort-safe conversions.
        # A conversion is published only when the canonical service proves it is
        # cohort-safe; unrelated period totals are never divided.
        "lifecycle_funnel": lifecycle_funnel,
    }


def _lifecycle_funnel_block(window: str, *, now=None) -> dict:
    """Canonical CRM lifecycle funnel counts + cohort-safe conversions.

    All-source scope: the Dashboard funnel answers a CRM question, so it is not
    restricted to Google Ads. Attribution subsets remain available under their own
    explicitly named KPIs.

    Fails closed: when the canonical store is unreadable every count is None and
    ``available`` is False — never zero.
    """
    empty = {e: None for e in ("lead", "mql", "sql", "opportunity", "customer")}
    try:
        from services import canonical_crm_funnel_service as _funnel  # noqa: PLC0415

        payload = _funnel.build(
            _funnel.WINDOW_BUSINESS, window,
            scope=_funnel.SCOPE_ALL_SOURCE, now=now)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dashboard_overview] canonical lifecycle funnel failed: %s", exc)
        return {**empty, "available": False, "status": "unavailable",
                "scope": "all_source", "conversions": None,
                "definitions": None, "sync": None}

    status = (payload.get("reconciliation") or {}).get("status")
    available = bool(payload.get("available"))
    events = payload.get("events") or {}

    counts = {}
    for event in empty:
        block = events.get(event) or {}
        # A mismatch must never render as a normal count.
        counts[event] = None if (not available or status == "mismatch") else block.get("count")

    return {
        **counts,
        "available": available,
        "status": status,
        "scope": payload.get("scope"),
        "scope_label": payload.get("scope_label"),
        "window": payload.get("window"),
        "conversions": payload.get("conversions") if available else None,
        "coverage": payload.get("coverage"),
        "definitions": {e: {
            "label": (events.get(e) or {}).get("label"),
            "event_date_property": (events.get(e) or {}).get("event_date_property"),
        } for e in empty},
        "sync": payload.get("sync"),
        "campaign_identity_available": payload.get("campaign_identity_available"),
    }
