"""
Canonical Revenue Decision Mart (PR-ADS-125)

ONE backend SQL/service contract that every Revenue & Attribution page reads
from. Before this service, each revenue page resolved its own spend truth, its
own window, its own FX coverage and its own campaign/country reconciliation, so
the pages disagreed with one another. This module ends that "page whack-a-mole":
Campaign, Country, Source and Deal views now read the SAME canonical truth.

Doctrine (unchanged, never loosened):
  - HubSpot closed-won deals are revenue truth.
  - Canonical Google Ads campaign-daily spend (google_ads_campaign_daily_spend)
    is the ONLY ROAS denominator; the geo table is diagnostic only.
  - Google Ads conversion value is NEVER used as revenue truth.
  - No fake $0. A missing mapping / unreconciled / FX-incomplete window yields
    Unavailable (None), never a fabricated zero.
  - No ROAS when the spend denominator is unsafe (incomplete coverage / FX).
  - Read-only. NO writes to Google Ads, NO writes to HubSpot.

Controlled grains (no naive mega-table that double-counts spend):
  1. Campaign-day spend grain   -> campaign view rows
  2. Country-day spend grain    -> country view rows
  3. Lead/deal event-date grain -> source + deal view rows
  4. This rollup service chooses the view: campaign | country | source | deal.

Single source contract every view shares (computed ONCE per window):
  - business window (resolver: analysis.business_windows.resolve_window)
  - spend (native + USD reporting)
  - FX coverage
  - campaign mapping status
  - country spend reconciliation status
  - leads / SQLs / customers / closed-won revenue / ROAS

The per-view ``rows`` change with the grain, but ``window``, ``spend_truth`` and
``summary`` are the SAME canonical truth regardless of the requested view. That
is the whole point of the mart: one brain, one truth, four renderings.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from analysis.business_windows import resolve_window
from db import revenue_repository as repo
from services.revenue_attribution_service import (
    build_revenue_attribution,
    build_revenue_deals,
)
from services.source_attribution_service import build_revenue_by_source

logger = logging.getLogger(__name__)

# The only views the canonical contract serves.
VALID_VIEWS = ("campaign", "country", "source", "deal")

# Reporting timezone used to render the business window. Falls back to this when
# the account time zone cannot be read from the durable spend tables.
DEFAULT_TIMEZONE = "Europe/London"


def _account_timezone() -> str:
    """Account reporting timezone, defaulting to Europe/London (read-only)."""
    try:
        tz = repo.fetch_account_time_zone()
    except Exception:  # noqa: BLE001 — diagnostics must never crash the mart
        tz = None
    return tz or DEFAULT_TIMEZONE


def _round2(value):
    """Round a numeric to 2 dp, passing None straight through (never fake 0)."""
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _window_block(core: dict) -> dict:
    """Canonical window block: resolver key/dates plus the reporting timezone."""
    resolved = core.get("window") or {}
    return {
        "key": resolved.get("key"),
        "label": resolved.get("label"),
        "start_date": resolved.get("start_date"),
        "end_date": resolved.get("end_date"),
        "timezone": _account_timezone(),
        "is_closed_window": resolved.get("is_closed_window"),
        "notes": resolved.get("notes"),
    }


def _spend_truth_block(core: dict) -> dict:
    """Canonical spend-truth block shared by every view.

    Derived from the single durable revenue-attribution contract so Campaign,
    Country, Source and Deal pages can never disagree on spend, FX coverage,
    campaign mapping status, or country reconciliation status.
    """
    health = core.get("source_health") or {}

    native_currency = health.get("spend_native_currency") or health.get(
        "canonical_currency"
    ) or "GBP"
    native_spend = _round2(health.get("spend_native_total"))
    usd_spend = _round2(health.get("spend_usd_total"))

    # FX coverage -> verified | incomplete | unavailable (never silently OK).
    fx_cov = health.get("fx_coverage_status")
    if fx_cov == "complete":
        fx_status = "verified"
    elif fx_cov == "incomplete":
        fx_status = "incomplete"
    else:  # not_evaluated | unavailable | None
        fx_status = "unavailable"

    # Campaign spend status -> verified only when the canonical denominator is a
    # trustworthy ROAS denominator (complete coverage + complete FX).
    if health.get("campaign_roas_available"):
        campaign_spend_status = "verified"
    elif health.get("spend_coverage_status") == "incomplete":
        campaign_spend_status = "incomplete"
    else:
        campaign_spend_status = "unavailable"

    # Country spend status -> geo<->canonical reconciliation outcome.
    reconciled = health.get("country_spend_reconciled")
    if reconciled is True:
        country_spend_status = "verified"
    elif reconciled is False:
        country_spend_status = "mismatch"
    else:
        country_spend_status = "unavailable"

    return {
        "native_currency": native_currency,
        "native_spend": native_spend,
        "usd_spend": usd_spend,
        "fx_status": fx_status,
        "campaign_spend_status": campaign_spend_status,
        "country_spend_status": country_spend_status,
        # Reporting currency is fixed to USD (HubSpot revenue currency).
        "reporting_currency": health.get("reporting_currency") or "USD",
        "spend_source": health.get("spend_source"),
        "spend_coverage_status": health.get("spend_coverage_status"),
    }


def _summary_block(core: dict, spend_truth: dict) -> dict:
    """Canonical top-line summary shared by every view.

    ``spend_usd`` is the canonical USD spend denominator (the same number every
    page must use). ``roas`` is exactly the revenue-attribution ROAS, which is
    already None whenever the spend denominator is unsafe — the mart never
    re-derives ROAS from an unsafe denominator.
    """
    summary = core.get("summary") or {}
    # spend_usd is STRICTLY the canonical USD spend. We never fall back to the
    # revenue-attribution summary["spend"], which is the native diagnostic figure
    # (GBP) whenever FX is incomplete — labelling native GBP as USD would be a
    # silent currency lie. When FX coverage is not verified, spend_truth.usd_spend
    # is None, so summary.spend_usd is None and ROAS (already gated upstream by
    # spend_trusted) stays None too. Native spend lives only in
    # spend_truth.native_spend.
    return {
        "spend_usd": spend_truth.get("usd_spend"),
        "leads": summary.get("leads"),
        "sqls": summary.get("sqls"),
        "customers": summary.get("customers"),
        "won_revenue_usd": _round2(summary.get("won_revenue")),
        "roas": summary.get("roas"),
    }


def _diagnostics(core: dict, view: str, spend_truth: dict) -> list:
    """Explain the canonical truth: why spend / ROAS are (un)available.

    Surfaces the shared truth-gating facts so a renderer never has to invent its
    own explanation. Warnings already produced by the revenue-attribution
    contract are carried through verbatim.
    """
    diags: list = []
    health = core.get("source_health") or {}

    for warning in (core.get("warnings") or []):
        diags.append({"code": "revenue_attribution_warning", "message": warning})

    if spend_truth["fx_status"] != "verified":
        diags.append({
            "code": "fx_coverage",
            "message": (
                "FX coverage is "
                f"'{spend_truth['fx_status']}' — USD spend / ROAS are withheld "
                "until every spend day has a daily FX rate."
            ),
        })
    if spend_truth["campaign_spend_status"] != "verified":
        diags.append({
            "code": "campaign_spend_coverage",
            "message": (
                "Canonical campaign spend coverage is "
                f"'{spend_truth['campaign_spend_status']}' — ROAS is unavailable "
                "(never a fake $0) until coverage is complete."
            ),
        })
    if spend_truth["country_spend_status"] == "mismatch":
        diags.append({
            "code": "country_spend_reconciliation",
            "message": (
                "Country (geo) spend does not reconcile with canonical campaign "
                "spend within tolerance — Country ROAS is withheld."
            ),
        })

    if view == "source":
        diags.append({
            "code": "source_spend_scope",
            "message": (
                "Only the Google Ads source carries spend/ROAS; every other "
                "source group is revenue-only (ROAS unavailable, never $0)."
            ),
        })
    return diags


def _readiness_block(core: dict, spend_truth: dict) -> dict:
    """Canonical decision-readiness, computed ONCE so the UI renders, never decides.

    PR-ADS-126: the mart owns every readiness verdict a revenue page used to
    derive on its own (lead-grain safety, revenue-integration connection, spend
    coverage, country reconciliation). The frontend reads these booleans and the
    accompanying statuses to render blocked/ready states — it does not re-derive
    them from raw source_health. The booleans mirror the existing doctrine; no new
    business math is introduced.
    """
    health = core.get("source_health") or {}

    lead_metrics_ready = (
        health.get("lead_date_grain_status") == "event_date"
        and health.get("lead_metrics_status") != "withheld"
    )
    if health.get("revenue_integration_status"):
        revenue_integration_connected = (
            health.get("revenue_integration_status") == "connected"
        )
    else:  # backward-compatible fallback for older payloads
        revenue_integration_connected = (
            health.get("revenue_attribution_status") != "not_wired_or_no_closed_won"
        )

    revenue_decision_ready = bool(lead_metrics_ready and revenue_integration_connected)
    spend_decision_ready = spend_truth["campaign_spend_status"] == "verified" and \
        spend_truth["fx_status"] == "verified"
    country_decision_ready = bool(
        spend_decision_ready and spend_truth["country_spend_status"] == "verified"
    )

    return {
        "revenue_decision_ready": revenue_decision_ready,
        "spend_decision_ready": bool(spend_decision_ready),
        "country_decision_ready": country_decision_ready,
        "roas_ready": (core.get("summary") or {}).get("roas") is not None,
        "lead_metrics_ready": bool(lead_metrics_ready),
        "revenue_integration_connected": bool(revenue_integration_connected),
        # Statuses the blocked-state renderer needs (presentation only).
        "lead_date_grain_status": health.get("lead_date_grain_status"),
        "lead_metrics_status": health.get("lead_metrics_status"),
        "revenue_integration_status": health.get("revenue_integration_status"),
        "revenue_attribution_status": health.get("revenue_attribution_status"),
        "missing_contact_created_at_count": health.get(
            "missing_contact_created_at_count", 0
        ),
        "country_spend_available": bool(core.get("country_spend_available")),
        "geo_country_mapping_status": core.get("geo_country_mapping_status"),
        "geo_country_source": health.get("geo_country_source"),
    }


def _canonical_core(window: str, now: datetime | None):
    """The single durable revenue-attribution contract for ``window``."""
    return build_revenue_attribution(window, now=now)


def _rows_for_view(view: str, window: str, core: dict, now: datetime | None) -> list:
    """Pick the rows for the requested view at its controlled grain."""
    if view == "campaign":
        return core.get("campaigns") or []
    if view == "country":
        return core.get("countries") or []
    if view == "source":
        return build_revenue_by_source(window, now=now).get("groups") or []
    if view == "deal":
        return build_revenue_deals(window, now=now).get("deals") or []
    return []


def build_revenue_decision_mart(
    view: str = "campaign",
    window: str = "current_quarter",
    now: datetime | None = None,
) -> dict:
    """Build the canonical Revenue Decision Mart contract for a view + window.

    Args:
        view: One of VALID_VIEWS (campaign | country | source | deal).
        window: A business-window key (analysis.business_windows.WINDOW_KEYS).
        now: Optional reference time for deterministic resolution/testing.

    Returns:
        The canonical contract: window, spend_truth, summary, rows, diagnostics.
        ``window``, ``spend_truth`` and ``summary`` are IDENTICAL across views;
        only ``rows`` changes with the requested grain.

    Raises:
        ValueError: If ``view`` is not allowed or ``window`` is unsupported.
    """
    if view not in VALID_VIEWS:
        raise ValueError(
            f"Invalid view '{view}'. Valid views: {', '.join(VALID_VIEWS)}"
        )
    # Validate / resolve the window up front so every view fails identically on a
    # bad window (shared window resolver — one truth for the business window).
    resolve_window(window, now=now)

    core = _canonical_core(window, now)

    spend_truth = _spend_truth_block(core)
    summary = _summary_block(core, spend_truth)
    readiness = _readiness_block(core, spend_truth)
    rows = _rows_for_view(view, window, core, now)
    diagnostics = _diagnostics(core, view, spend_truth)

    return {
        "view": view,
        "window": _window_block(core),
        "spend_truth": spend_truth,
        "summary": summary,
        "readiness": readiness,
        "rows": rows,
        "diagnostics": diagnostics,
        "source_truth": "revenue_decision_mart",
        "google_ads_conversion_value_used": False,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }


# ── Audit: do all Revenue & Attribution pages agree with the mart? ───────────


def _abs_and_pct(current, mart):
    """Return (abs_diff, pct_diff) or (None, None) when not comparable."""
    if current is None or mart is None:
        return None, None
    try:
        diff = round(float(current) - float(mart), 2)
    except (TypeError, ValueError):
        return None, None
    base = abs(float(mart)) if mart else 0.0
    pct = round(abs(diff) / base, 4) if base else (0.0 if diff == 0 else None)
    return diff, pct


def _audit_status(current, mart, *, tolerance: float):
    """pass | fail | unavailable for one page-vs-mart comparison."""
    if current is None or mart is None:
        return "unavailable"
    diff, pct = _abs_and_pct(current, mart)
    if diff is None:
        return "unavailable"
    if abs(diff) <= 0.01:
        return "pass"
    if pct is not None and pct <= tolerance:
        return "pass"
    return "fail"


def _difference_reasons(page_key: str, spend_truth: dict, *, status: str) -> list:
    """Explain exactly WHY a page can differ from the mart (the audit's job)."""
    if status == "pass":
        return []
    reasons: list = []
    if page_key == "roas_by_country" and spend_truth["country_spend_status"] != "verified":
        reasons.append("different_geo_reconciliation")
    if page_key == "revenue_by_source":
        # Source page Google Ads spend is the geo total; canonical campaign spend
        # is the ROAS denominator — a classic different-source-table mismatch.
        reasons.append("different_source_table")
        if spend_truth["fx_status"] != "verified":
            reasons.append("different_fx_coverage")
    if spend_truth["campaign_spend_status"] != "verified":
        reasons.append("different_campaign_mapping")
    if spend_truth["fx_status"] != "verified" and "different_fx_coverage" not in reasons:
        reasons.append("different_fx_coverage")
    if page_key == "deals":
        reasons.append("different_date_grain")
    return reasons


def build_revenue_performance_audit(
    window: str, now: datetime | None = None
) -> dict:
    """Compare every current revenue page's output against the canonical mart.

    Answers the acceptance-criteria question: do all Revenue & Attribution pages
    agree on spend, revenue and ROAS — and if not, exactly where do they differ?

    Read-only. No writes to Google Ads or HubSpot.

    Raises:
        ValueError: If ``window`` is unsupported.
    """
    resolve_window(window, now=now)

    core = _canonical_core(window, now)
    spend_truth = _spend_truth_block(core)
    summary = _summary_block(core, spend_truth)

    # Canonical mart spend (USD denominator) — the one number pages must match.
    mart_spend = summary.get("spend_usd")
    mart_revenue = summary.get("won_revenue_usd")

    # Current per-page truth, each at its own grain.
    campaigns = core.get("campaigns") or []
    countries = core.get("countries") or []
    source = build_revenue_by_source(window, now=now)
    deals = build_revenue_deals(window, now=now)

    def _sum_spend(rows):
        total = 0.0
        seen_value = False
        for r in rows:
            spend = r.get("spend")
            if spend is None:
                continue
            seen_value = True
            try:
                total += float(spend)
            except (TypeError, ValueError):
                continue
        return round(total, 2) if seen_value else None

    campaign_page_spend = _sum_spend(campaigns)
    country_page_spend = _sum_spend(countries)
    google_group = next(
        (g for g in (source.get("groups") or []) if g.get("group") == "google_ads"),
        {},
    )
    source_google_spend = _round2(google_group.get("spend"))
    deals_revenue = _round2((deals.get("summary") or {}).get("won_revenue"))

    tolerance = 0.02  # mirrors SPEND_VARIANCE_TOLERANCE for reconciliation
    pages_spec = [
        ("roas_by_campaign", "ROAS by Campaign", "spend", campaign_page_spend, mart_spend),
        ("roas_by_country", "ROAS by Country", "spend", country_page_spend, mart_spend),
        ("revenue_by_source", "Revenue by Source", "spend", source_google_spend, mart_spend),
        ("deals", "Deals", "revenue", deals_revenue, mart_revenue),
    ]

    pages = []
    all_pass = True
    for key, label, metric, current, mart_value in pages_spec:
        status = _audit_status(current, mart_value, tolerance=tolerance)
        diff, pct = _abs_and_pct(current, mart_value)
        if status != "pass":
            all_pass = False
        pages.append({
            "page": label,
            "page_key": key,
            "metric": metric,
            "current_value": current,
            "mart_value": mart_value,
            "difference": diff,
            "difference_pct": pct,
            "status": status,
            "reasons": _difference_reasons(key, spend_truth, status=status),
        })

    return {
        "window": _window_block(core),
        "spend_truth": spend_truth,
        "summary": summary,
        "all_pages_agree": all_pass,
        "tolerance": tolerance,
        "pages": pages,
        "diff_reason_codes": [
            "different_source_table",
            "different_date_grain",
            "different_campaign_mapping",
            "different_geo_reconciliation",
            "different_fx_coverage",
            "different_source_classification",
        ],
        "source_truth": "revenue_decision_mart",
        "google_ads_conversion_value_used": False,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }
