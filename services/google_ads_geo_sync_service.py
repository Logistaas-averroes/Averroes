"""
Google Ads Geo Sync + country reconciliation (PR-ADS-124).

Country ROAS needs geo-level (per-country) spend to reconcile with the canonical
campaign-level Google Ads spend before a country ROAS denominator can be trusted.
This service:

  - reads canonical Google Ads geo spend DIRECTLY from the API (read-only,
    geographic_view) and writes ONLY the local canonical geo table, and
  - reconciles the canonical geo total against the canonical campaign-level total
    for a business window, exposing the exact mismatch.

Doctrine: reads Google Ads read-only; writes ONLY local canonical tables. NEVER
writes to Google Ads and NEVER writes to HubSpot. Country ROAS stays unavailable
until geo spend reconciles — no fake ROAS, no partial denominator, no $0
replacement.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from analysis import country_identity
from db import revenue_repository as repo
# PR-ADS-153F: THE canonical machine key for this dataset. Every surface that
# names canonical geo — scheduler batch, freshness config, system status,
# Revenue Health, reconciliation and API responses — imports these constants
# rather than spelling the strings itself, so a rename can never leave one
# surface reading a key nothing writes (the defect that left canonical_spend
# with no freshness signal at all).
from services.dataset_keys import (
    CANONICAL_GEO_DATASET as GEO_SYNC_DATASET,
    CANONICAL_GEO_SCOPE as GEO_SYNC_SCOPE,
    CANONICAL_GEO_SOURCE as GEO_SYNC_SOURCE,
)
from services.google_ads_spend_service import (
    SPEND_VARIANCE_TOLERANCE,
    _window_bounds,
    configured_customer_id,
)

log = logging.getLogger(__name__)

# Earliest geo history to sync by default (matches the spend backfill floor).
DEFAULT_GEO_SYNC_START = date(2024, 1, 1)

# Daily rolling re-fetch window, matching the canonical campaign-spend lookback
# so the two denominators are refreshed over the SAME recent range. Google Ads
# restates recent spend, so the newest days must be re-fetched rather than
# treated as settled — that is why a daily chunk is never "already verified".
DAILY_GEO_LOOKBACK_DAYS = 7


class GeoPersistenceError(RuntimeError):
    """A geo read succeeded but its durable write did not.

    Raised so the chunk is recorded as ``failed`` rather than ``verified``: a
    range whose rows never landed must never be reported as covered.
    """


def fetch_geo_daily(start_date: str, end_date: str) -> dict:
    """Thin seam over the canonical Google Ads geo connector (late import).

    Read-only. Patched directly by tests so the google-ads SDK is never imported.
    """
    from connectors.google_ads_direct import fetch_geo_daily_spend as _f  # noqa: PLC0415
    return _f(start_date, end_date)


def fetch_geo_country_codes(criterion_ids) -> dict:
    """Seam over the geo_target_constant resolver (late import). Read-only.

    Maps Google Ads country criterion ids -> {country_code, name}. Patched by
    tests so the SDK is never imported. Best-effort: an empty result simply
    leaves rows with an unresolved country (still stored, just not country-named).
    """
    from connectors.google_ads_direct import fetch_geo_target_country_codes as _f  # noqa: PLC0415
    return _f(criterion_ids)


def _resolve_country_metadata(rows: list) -> None:
    """Attach country_code + country_name to canonical geo rows in place.

    Resolves the distinct criterion ids via geo_target_constant so the canonical
    geo table itself carries the country identity that feeds named ROAS by
    Country rows. Resolution failure is non-fatal: rows keep an unresolved
    country (never blocks the spend write).
    """
    ids = {r.get("country_criterion_id") for r in rows if r.get("country_criterion_id")}
    if not ids:
        return
    try:
        resolved = fetch_geo_country_codes(sorted(ids)) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("[geo_sync] country-code resolution failed: %s", exc)
        resolved = {}
    for r in rows:
        meta = resolved.get(str(r.get("country_criterion_id"))) or {}
        r["country_code"] = meta.get("country_code")
        r["country_name"] = meta.get("name")


def _micros_to_native(micros) -> float:
    return round(int(micros or 0) / 1_000_000, 6)


def build_geo_reconciliation(window: str, now: datetime | None = None) -> dict:
    """Reconcile canonical geo spend against the canonical campaign-level total.

    The diagnostics behind the Revenue Health Geo Sync panel and the ROAS by
    Country blocked card. Compares, for the SAME window (native currency):
    canonical campaign-level spend vs canonical geo (country) spend, plus FX
    coverage. Read-only. Never fabricates a reconciliation when geo data is
    absent — that is reported as ``no_geo_data`` (not £0).

    Returns the panel contract including canonical/geo totals, variance, status
    (reconciled | mismatch | no_geo_data | unavailable), latest sync metadata,
    and whether Country ROAS is unblockable (campaign coverage + FX + geo all OK).
    """
    account_time_zone = repo.fetch_account_time_zone()
    resolved, start, end = _window_bounds(window, now, account_time_zone)
    date_from = resolved.get("start_date")
    date_to = resolved.get("end_date")

    canonical = repo.fetch_canonical_campaign_spend(start, end)
    canonical_available = bool(canonical.get("available"))
    canonical_total = (round(float(canonical.get("total_spend") or 0.0), 6)
                       if canonical_available else None)
    currency_code = (canonical.get("currency_code") if canonical_available else None) or "GBP"
    customer_id = (canonical.get("customer_id") if canonical_available else None) \
        or configured_customer_id()

    geo = repo.fetch_geo_daily_spend_total(start, end)
    geo_available = bool(geo.get("available"))
    geo_has_rows = bool(geo.get("has_rows"))
    geo_total = round(float(geo.get("total_spend") or 0.0), 6) if geo_has_rows else None

    # Campaign-coverage completeness (canonical denominator) + FX coverage — the
    # other two gates Country ROAS needs alongside geo reconciliation.
    coverage_status = "unavailable"
    coverage_complete = False
    if canonical_available:
        try:
            from services.google_ads_spend_service import analyze_coverage  # noqa: PLC0415
            cov = analyze_coverage(start, end, repo.fetch_spend_coverage(start, end).get("chunks", []))
            coverage_complete = bool(cov.get("complete"))
            coverage_status = "complete" if coverage_complete else "incomplete"
        except Exception as exc:  # noqa: BLE001
            log.warning("[geo-reconcile] coverage check failed: %s", exc)

    fx_complete = False
    try:
        fx = repo.fetch_fx_coverage(start, end, currency_code)
        fx_complete = bool(fx.get("complete"))
    except Exception as exc:  # noqa: BLE001
        log.warning("[geo-reconcile] fx coverage failed: %s", exc)

    # Variance is computed in native currency and never hidden.
    if canonical_total is not None and geo_total is not None:
        variance = round(geo_total - canonical_total, 6)
        if canonical_total > 0:
            variance_pct = round((abs(variance) / canonical_total) * 100, 4)
            reconciled = abs(variance) / canonical_total <= SPEND_VARIANCE_TOLERANCE
        else:
            variance_pct = 0.0 if variance == 0 else None
            reconciled = (geo_total == 0)
    else:
        variance = None
        variance_pct = None
        reconciled = False

    if not canonical_available:
        status = "unavailable"
    elif not geo_has_rows:
        status = "no_geo_data"
    elif reconciled:
        status = "reconciled"
    else:
        status = "mismatch"

    # Country ROAS is unblockable only when ALL three gates pass. This mirrors the
    # revenue-attribution rule and is reported so the UI never loosens it.
    country_roas_unblockable = bool(
        canonical_available and coverage_complete and fx_complete and status == "reconciled"
    )

    # PR-ADS-129: explain WHY the totals differ, from per-day + per-campaign data.
    breakdown = _geo_reconciliation_detail(
        start, end, canonical_total, geo_total, status)

    # PR-ADS-131: an unattributed-residual shortfall (geographic_view omits
    # location-less spend by design) is a SAFE unblock — Country ROAS can show real
    # country rows plus an explicit residual bucket instead of staying trapped in a
    # blind "run geo sync" loop. This never loosens tolerance, never distributes the
    # residual across countries, and keeps missing geo dates / missing campaigns /
    # incomplete FX BLOCKED. `status` and `country_roas_unblockable` (the strict
    # "reconciles perfectly" flag) are intentionally unchanged.
    residual = evaluate_country_residual(
        canonical_total, geo_total, breakdown,
        coverage_complete=coverage_complete, fx_complete=fx_complete,
        geo_has_rows=geo_has_rows, reconciled=(status == "reconciled"))
    # PR-ADS-153F: derived by THE shared gate, not by a local expression. The
    # ordering it encodes is unchanged (a safe residual outranks a plain
    # mismatch; an unreadable source is unavailable, never a mismatch).
    country_spend_status = resolve_country_spend_status(
        reconciled=(True if status == "reconciled"
                    else None if status in ("unavailable", "no_geo_data") else False),
        residual_eligible=residual["eligible"],
    )

    # PR-ADS-153F: durable coverage evidence from the geo ledger. This is
    # EVIDENCE, not a new blocking condition — the gate above is unchanged. It
    # answers "which ranges were never fetched, and which failed", which the
    # spend-comparison heuristics (`missing_geo_dates`) cannot distinguish from
    # "fetched and genuinely zero".
    geo_ledger = analyze_geo_coverage(start, end)

    return {
        "window": window,
        "date_from": date_from,
        "date_to": date_to,
        "account_time_zone": account_time_zone,
        "currency_code": currency_code,
        "customer_id": customer_id,
        "canonical_campaign_total": canonical_total,
        "geo_total": geo_total,
        "variance": variance,
        "variance_pct": variance_pct,
        "status": status,
        "reconciled": status == "reconciled",
        "coverage_status": coverage_status,
        "fx_coverage_status": "complete" if fx_complete else "incomplete",
        "country_roas_unblockable": country_roas_unblockable,
        # PR-ADS-131: safe-unblock verdict + explicit residual bucket for the
        # Country page and Revenue Health. `country_spend_status` mirrors the mart's
        # country_spend_status (verified | reconciled_with_residual | mismatch |
        # unavailable). The residual is the campaign↔geo shortfall Google Ads could
        # not assign to a country — surfaced, never spread across real countries.
        "country_spend_status": country_spend_status,
        "country_residual_native": residual["residual_native"],
        "country_residual_pct": residual["residual_pct"],
        "country_residual_label": (country_identity.RESIDUAL_LABEL if residual["eligible"] else None),
        "country_residual_reason": (
            "Google Ads geographic view does not assign this spend to a country."
            if residual["eligible"] else None),
        "geo_rows_counted": int(geo.get("rows_counted") or 0) if geo_available else 0,
        "geo_country_count": int(geo.get("country_count") or 0) if geo_available else 0,
        "campaign_rows_counted": breakdown.get("campaign_rows_counted"),
        "last_geo_sync_at": geo.get("last_synced_at") if geo_available else None,
        "tolerance": SPEND_VARIANCE_TOLERANCE,
        # PR-ADS-129 diagnostics: exactly where the gap is + the concrete next action.
        "reason": breakdown.get("reason"),
        "next_action": breakdown.get("next_action"),
        "missing_geo_dates": breakdown.get("missing_geo_dates", []),
        # PR-ADS-153F: the geo sync now HAS a per-chunk failure ledger. These
        # three fields answer "which ranges were never fetched and which failed",
        # which a spend comparison alone cannot separate from "fetched, and the
        # country genuinely spent nothing".
        "geo_coverage_available": bool(geo_ledger.get("available")),
        "geo_coverage_complete": bool(geo_ledger.get("complete")),
        "geo_coverage_missing_ranges": geo_ledger.get("missing_ranges", []),
        "failed_geo_dates": geo_ledger.get("failed_chunks", []),
        "geo_ready": country_geo_ready(country_spend_status),
        "geo_accepted_states": sorted(GEO_ACCEPTED_STATES),
        "unmapped_location_spend_native": breakdown.get("unmapped_geo_native"),
        "unknown_location_spend_native": breakdown.get("unknown_country_spend_native"),
        "unknown_country_spend_native": breakdown.get("unknown_country_spend_native"),
        "geo_rows_with_null_country": breakdown.get("geo_rows_with_null_country"),
        "excluded_geo_spend_native": None,
        "campaign_spend_without_geo_native": breakdown.get("campaign_spend_without_geo_native"),
        "campaigns_missing_geo": breakdown.get("campaigns_missing_geo", []),
        "network_or_segment_gap_native": breakdown.get("network_or_segment_gap_native"),
        # PR-ADS-130: date-level and campaign-level gap breakdowns (largest first).
        "largest_daily_gaps": breakdown.get("largest_daily_variances", []),
        "largest_daily_variances": breakdown.get("largest_daily_variances", []),
        "largest_campaign_gaps": breakdown.get("campaign_geo_gaps", []),
        "campaign_geo_gaps": breakdown.get("campaign_geo_gaps", []),
        "campaign_ids_in_geo": breakdown.get("campaign_ids_in_geo"),
        # PR-ADS-130: document the query dimensions each side uses so a by-design
        # non-reconciliation is auditable rather than a mystery.
        "campaign_query_dimensions": ["customer_id", "campaign_id", "spend_date", "cost_micros"],
        "geo_query_dimensions": ["customer_id", "campaign_id", "country_criterion_id", "spend_date", "cost_micros"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _geo_reconciliation_detail(start, end, canonical_total, geo_total, status: str) -> dict:
    """Per-day + per-campaign explanation of the geo↔campaign variance (PR-ADS-129).

    Best-effort and read-only: computes the largest daily and per-campaign gaps,
    the geo spend with no resolvable country (unmapped), the overall gap, and a
    concrete next action. Never blocks or fabricates — on any error it returns the
    top-line reason only.
    """
    net_gap = (round(canonical_total - geo_total, 6)
               if (canonical_total is not None and geo_total is not None) else None)

    detail = repo.fetch_geo_reconciliation_breakdown(start, end)
    if not detail.get("available"):
        reason = ("geo_total_below_campaign_total"
                  if (net_gap is not None and net_gap > 0) else "totals_differ")
        return {
            "reason": reason,
            "next_action": "inspect_geo_gap",
            "network_or_segment_gap_native": net_gap,
            "missing_geo_dates": [],
            "largest_daily_variances": [],
            "campaign_geo_gaps": [],
            "campaign_ids_in_geo": None,
            "campaign_rows_counted": None,
            "unmapped_geo_native": None,
        }

    daily = detail.get("daily", [])
    # Dates where campaign spent but geo has NO spend at all → a genuine geo gap
    # (the geo sync did not cover that day), distinct from an unattributed residual.
    missing_geo_dates = [
        d["spend_date"] for d in daily
        if (d.get("campaign_spend") or 0) > 0 and (d.get("geo_spend") or 0) == 0
    ]
    largest_daily = sorted(
        (
            {
                "date": d.get("spend_date"),
                "campaign_spend_native": d.get("campaign_spend"),
                "geo_spend_native": d.get("geo_spend"),
                "variance_native": round((d.get("geo_spend") or 0) - (d.get("campaign_spend") or 0), 6),
            }
            for d in daily
        ),
        key=lambda x: abs(x["variance_native"]),
        reverse=True,
    )[:10]

    by_campaign = detail.get("by_campaign", [])
    campaign_ids_in_geo = any((c.get("geo_spend") or 0) > 0 for c in by_campaign)
    campaign_gaps = sorted(
        (
            {
                "campaign_id": c.get("campaign_id"),
                "campaign_name": c.get("campaign_name"),
                "campaign_spend_native": c.get("campaign_spend"),
                "geo_spend_native": c.get("geo_spend"),
                "variance_native": round((c.get("geo_spend") or 0) - (c.get("campaign_spend") or 0), 6),
            }
            for c in by_campaign
        ),
        key=lambda x: abs(x["variance_native"]),
        reverse=True,
    )[:10]

    # PR-ADS-130: campaigns that spent but have NO geo rows at all — a genuine
    # per-campaign geo gap (distinct from a total-only residual).
    campaigns_missing_geo = [
        {
            "campaign_id": c.get("campaign_id"),
            "campaign_name": c.get("campaign_name"),
            "campaign_spend_native": c.get("campaign_spend"),
        }
        for c in by_campaign
        if (c.get("campaign_spend") or 0) > 0 and (c.get("geo_spend") or 0) == 0
    ]
    campaign_spend_without_geo = round(
        sum(c["campaign_spend_native"] or 0 for c in campaigns_missing_geo), 6)
    unknown_country_spend = detail.get("unmapped_geo_native")

    if status == "reconciled":
        reason = "reconciled"
        next_action = "none"
    elif missing_geo_dates:
        reason = "missing_geo_dates"
        next_action = "run_geo_sync_for_missing_dates"
    elif campaigns_missing_geo:
        reason = "campaign_spend_without_geo"
        next_action = "inspect_geo_query_parity"
    elif net_gap is not None and net_gap > 0:
        # Geo rows exist for every campaign-spend day AND every campaign, but the
        # totals still differ. The residual is spend Google Ads did not attribute
        # to any country — the geographic_view report omits it by design, so this
        # denominator cannot reconcile to campaign spend under the current query.
        reason = "geo_report_does_not_reconcile_by_design"
        next_action = "inspect_geo_query_parity"
    else:
        reason = "totals_differ"
        next_action = "inspect_geo_gap"

    return {
        "reason": reason,
        "next_action": next_action,
        "network_or_segment_gap_native": net_gap,
        "unmapped_geo_native": unknown_country_spend,
        "unknown_country_spend_native": unknown_country_spend,
        "geo_rows_with_null_country": detail.get("geo_rows_with_null_country"),
        "campaign_spend_without_geo_native": campaign_spend_without_geo,
        "campaigns_missing_geo": campaigns_missing_geo[:10],
        "missing_geo_dates": missing_geo_dates,
        "largest_daily_variances": largest_daily,
        "campaign_geo_gaps": campaign_gaps if campaign_ids_in_geo else [],
        "campaign_ids_in_geo": campaign_ids_in_geo,
        "campaign_rows_counted": detail.get("campaign_rows_counted"),
    }


def evaluate_country_residual(canonical_total, geo_total, detail, *,
                              coverage_complete, fx_complete, geo_has_rows, reconciled):
    """PR-ADS-131: decide whether a geo↔campaign shortfall is a SAFE unattributed
    residual rather than a blocking mismatch.

    Returns {eligible, residual_native, residual_pct, reason}. ``detail`` is the
    ``_geo_reconciliation_detail`` breakdown (reused, not re-fetched).

    Eligible ONLY when — campaign-spend coverage AND FX are complete, geo rows
    exist, there are NO missing geo dates and NO campaigns missing geo, the
    shortfall is positive (geo total below campaign total), and the reason is the
    by-design residual (``geo_report_does_not_reconcile_by_design``). This never
    loosens tolerance and never distributes the residual across countries. Missing
    geo dates, campaigns missing geo, or incomplete FX/coverage keep it BLOCKED.

        residual_native = canonical_campaign_total - geo_total   (only when > 0)
    """
    out = {"eligible": False, "residual_native": None, "residual_pct": None,
           "reason": (detail or {}).get("reason")}
    if reconciled or not (coverage_complete and fx_complete and geo_has_rows):
        return out
    if canonical_total is None or geo_total is None or canonical_total <= 0:
        return out
    net_gap = round(canonical_total - geo_total, 2)
    if net_gap <= 0:
        return out
    if ((detail or {}).get("reason") == "geo_report_does_not_reconcile_by_design"
            and not (detail or {}).get("missing_geo_dates")
            and not (detail or {}).get("campaigns_missing_geo")):
        out["eligible"] = True
        out["residual_native"] = net_gap
        out["residual_pct"] = round(net_gap / canonical_total * 100, 4)
    return out


def _verified_chunk_keys(start: date, end: date) -> tuple[set, bool]:
    """Chunk keys already proven verified in the durable geo coverage ledger.

    Returns ``(keys, ledger_readable)``. When the ledger cannot be read the set
    is empty AND ``ledger_readable`` is False — the caller must then re-fetch
    everything rather than assume nothing was covered, because "unreadable" and
    "nothing covered" are different facts and only one of them is safe to skip on.
    """
    coverage = repo.fetch_geo_coverage(start, end)
    if not coverage.get("available"):
        return set(), False
    return (
        {f"{c.get('chunk_start')}:{c.get('chunk_end')}"
         for c in coverage.get("chunks", [])
         if c.get("status") == "verified"},
        True,
    )


def analyze_geo_coverage(start: date | None, end: date) -> dict:
    """Geo coverage completeness over a window, via the SHARED analyzer.

    PR-ADS-153F. Deliberately reuses
    ``services.google_ads_spend_service.analyze_coverage`` rather than
    reimplementing day arithmetic: campaign coverage and geo coverage now answer
    "is this window covered" with ONE implementation, so they cannot drift into
    disagreeing about the same dates.
    """
    from services.google_ads_spend_service import analyze_coverage  # noqa: PLC0415

    ledger = repo.fetch_geo_coverage(start, end)
    if not ledger.get("available"):
        return {"available": False, "complete": False, "missing_days": None,
                "missing_ranges": [], "failed_chunks": [], "verified_chunks": [],
                "reason": "geo_coverage_ledger_unavailable"}
    result = analyze_coverage(start, end, ledger.get("chunks", []))
    result["available"] = True
    return result


# ─────────────────────────────────────────────────────────────────────────────
# THE geo readiness gate (PR-ADS-153F)
# ─────────────────────────────────────────────────────────────────────────────
# Every geo consumer asks the same question — "is the per-country spend
# denominator safe to divide by?" — and before this PR three of them answered it
# with their own code:
#
#   * this service derived country_spend_status from the reconciliation,
#   * services/revenue_decision_mart re-derived it from source_health, and
#   * the mart's page-difference audit used a STRICTER bar (`== "verified"`)
#     than Dashboard Countries (`in ("verified", "reconciled_with_residual")`),
#
# so the same window could be "ready" on one page and "blocked" on another.
# The states and the predicate now live here, once.
#
# NOTHING is loosened: the accepted states are exactly the two that were already
# accepted by Dashboard Countries and the mart's readiness block, and
# `reconciled_with_residual` still requires the unchanged PR-ADS-131 safe-residual
# predicate (`evaluate_country_residual`) to have passed.

#: Geo spend is a trustworthy Country ROAS denominator.
GEO_STATUS_VERIFIED = "verified"
#: Real country rows plus an explicit, structurally-explained residual bucket.
GEO_STATUS_RECONCILED_WITH_RESIDUAL = "reconciled_with_residual"
#: The totals differ for a reason that is NOT the by-design residual.
GEO_STATUS_MISMATCH = "mismatch"
#: Geo or campaign spend could not be read at all.
GEO_STATUS_UNAVAILABLE = "unavailable"

#: The ONLY states in which a country-level denominator may be divided by.
GEO_ACCEPTED_STATES = frozenset({
    GEO_STATUS_VERIFIED, GEO_STATUS_RECONCILED_WITH_RESIDUAL,
})

#: Stable machine reasons for a blocked country view. Consumers render these;
#: they never invent their own wording for the same condition.
GEO_GAP_MISSING_DATES = "missing_geo_dates"
GEO_GAP_CAMPAIGN_WITHOUT_GEO = "campaign_spend_without_geo"
GEO_GAP_BY_DESIGN_RESIDUAL = "geo_report_does_not_reconcile_by_design"
GEO_GAP_TOTALS_DIFFER = "totals_differ"


def resolve_country_spend_status(*, reconciled, residual_eligible,
                                 geo_readable=True) -> str:
    """Derive the ONE canonical country-spend status.

    ``reconciled`` is tri-state on purpose: ``True`` (within the unchanged
    ``SPEND_VARIANCE_TOLERANCE``), ``False`` (measured and outside it), or
    ``None`` (not measurable). ``None`` is NOT ``False`` — an unmeasured
    reconciliation is unavailable, never a mismatch, because reporting a
    mismatch would assert a comparison nobody performed.
    """
    if not geo_readable:
        return GEO_STATUS_UNAVAILABLE
    if reconciled is True:
        return GEO_STATUS_VERIFIED
    if residual_eligible:
        return GEO_STATUS_RECONCILED_WITH_RESIDUAL
    if reconciled is False:
        return GEO_STATUS_MISMATCH
    return GEO_STATUS_UNAVAILABLE


def country_geo_ready(country_spend_status) -> bool:
    """Whether a country-level ROAS denominator may be used.

    THE predicate. Every geo consumer calls this instead of comparing the status
    string itself, so no page can quietly adopt a stricter or looser bar.
    """
    return country_spend_status in GEO_ACCEPTED_STATES


def build_country_truth_disclosure(spend_truth: dict, window_block: dict | None = None,
                                   *, revenue_source=None, revenue_scope=None,
                                   revenue_available=True, revenue_reason=None,
                                   revenue_violation_codes=None, as_of=None) -> dict:
    """The ONE disclosure block every country surface publishes (PR-ADS-153F §8).

    A country view is only auditable if it says, in the response, WHICH sources
    it used, at WHAT scope, over WHICH exact instants, how fresh they are,
    whether coverage and reconciliation are safe, how large the residual is and
    whether it was accepted. Assembling that per page is how two pages end up
    disclosing different things about the same window, so it is assembled here.

    Withheld metrics are ``None`` at the call sites; this block explains why.
    ``legacy_fallback_used`` is a hard ``False``: there is no legacy geo or
    revenue path left to fall back to, and saying so in the payload is what
    makes that checkable from outside the process.
    """
    status = spend_truth.get("country_spend_status")
    ready = country_geo_ready(status)
    window_block = window_block or {}
    return {
        # Sources and scope — ownership stays split and stated.
        "revenue_source": revenue_source,
        "revenue_scope": revenue_scope,
        "spend_source": spend_truth.get("spend_source"),
        "geo_spend_source": "google_ads_geo_daily_spend",
        "geo_spend_grain": "customer_id, campaign_id, country_criterion_id, spend_date",
        "country_identity_contract": "analysis.country_identity",
        "estimate_grade_note": country_identity.ESTIMATE_GRADE_NOTE,
        # Window — the resolver's key AND the exact instants it resolved to.
        "window": {
            "key": window_block.get("key"),
            "label": window_block.get("label"),
            "start_date": window_block.get("start_date"),
            "end_date": window_block.get("end_date"),
            "start_utc": window_block.get("start_utc"),
            "end_utc_exclusive": window_block.get("end_utc_exclusive"),
            "bounds": "inclusive_start_exclusive_end_utc",
            "timezone": window_block.get("timezone"),
        },
        # Freshness / coverage / reconciliation.
        "as_of": as_of,
        "geo_coverage_status": spend_truth.get("geo_coverage_status"),
        "geo_coverage_missing_ranges": spend_truth.get("geo_coverage_missing_ranges") or [],
        "geo_failed_ranges": spend_truth.get("failed_geo_dates") or [],
        "campaign_spend_coverage_status": spend_truth.get("campaign_spend_coverage_status"),
        "fx_status": spend_truth.get("fx_status"),
        "reconciliation_status": status,
        "reconciliation_tolerance": spend_truth.get("country_spend_tolerance"),
        "geo_ready": ready,
        "geo_accepted_states": sorted(GEO_ACCEPTED_STATES),
        "gap_codes": ([] if ready else [c for c in (spend_truth.get("country_gap_reason"),)
                                        if c]),
        # Residual — amount, count, and whether it was ACCEPTED as safe.
        "residual_accepted": status == GEO_STATUS_RECONCILED_WITH_RESIDUAL,
        "residual_label": country_identity.RESIDUAL_LABEL,
        "residual_key": country_identity.RESIDUAL_KEY,
        "residual_spend_native": spend_truth.get("country_residual_native"),
        "residual_spend_usd": spend_truth.get("country_residual_usd"),
        "residual_spend_pct": spend_truth.get("country_residual_pct"),
        # Revenue availability, stated separately from spend availability.
        "revenue_available": bool(revenue_available),
        "revenue_unavailable_reason": (None if revenue_available else revenue_reason),
        "revenue_violation_codes": list(revenue_violation_codes or []),
        "legacy_fallback_used": False,
    }


def run_google_ads_geo_sync(
    *,
    window: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    dry_run: bool = True,
    chunk_months: int = 1,
    now: datetime | None = None,
    job_id: str | None = None,
    progress: dict | None = None,
    checkpoint=None,
    resume: bool = True,
    require_lease: bool | None = None,
) -> dict:
    """Sync canonical Google Ads geo (country) daily spend in monthly chunks.

    Reads Google Ads read-only (geographic_view) and writes ONLY the local
    canonical google_ads_geo_daily_spend table plus its coverage/state ledgers
    (when not dry_run). NEVER writes to Google Ads or HubSpot. Re-running is
    idempotent (unique upsert).

    PR-ADS-153F adds the durable half that was missing:

      * **Resume.** A chunk already recorded ``verified`` in
        ``google_ads_geo_coverage`` is SKIPPED, so a recovery run re-fetches only
        the ranges that are missing or failed instead of replaying history.
      * **Per-chunk failure evidence.** A failed chunk is written to the ledger
        as ``failed`` (never silently absent), so the next run retries exactly it.
      * **Write-before-claim.** A chunk is marked ``verified`` only AFTER its rows
        are durably written, and the success checkpoint advances only after every
        requested chunk is covered. A partial run can never publish complete
        coverage or a healthy freshness signal.
      * **Overlap safety.** A durable lease stops the scheduler and the manual
        recovery trigger from running concurrently across instances.

    ``resume=False`` forces a re-fetch of already-verified chunks (recovery from
    a suspected bad fetch). ``require_lease`` defaults to "on unless dry_run" — a
    dry run writes nothing, so it cannot corrupt a concurrent real run.

    The window may be given as a business window (resolved in the account time
    zone) or as an explicit date_from/date_to. Returns a summary mirroring the
    spend backfill (status, summary, chunks, errors).
    """
    from dateutil.relativedelta import relativedelta  # noqa: PLC0415
    import db.writers as db_writers  # noqa: PLC0415

    account_time_zone = repo.fetch_account_time_zone()
    if window:
        _resolved, start, end = _window_bounds(window, now, account_time_zone)
        if start is None:
            start = DEFAULT_GEO_SYNC_START
    else:
        end = date.fromisoformat(date_to) if date_to else datetime.now(tz=timezone.utc).date()
        start = date.fromisoformat(date_from) if date_from else DEFAULT_GEO_SYNC_START
    if start > end:
        raise ValueError("date_from must be before or equal to date_to")

    customer_id = configured_customer_id()
    if require_lease is None:
        require_lease = not dry_run

    started_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = progress if progress is not None else {}
    state.update({"running": True, "dry_run": dry_run, "phase": "starting", "job_id": job_id,
                  "started_at": started_at})

    # ── Overlap guard ────────────────────────────────────────────────────────
    # Refusing to start is the SAFE outcome: the range stays uncovered, the gate
    # keeps Country ROAS blocked, and the next run picks it up. Two concurrent
    # runs writing the same coverage rows is the unsafe one.
    lease_held = False
    if require_lease:
        lease = db_writers.try_claim_geo_sync_lease(customer_id, run_id=job_id)
        lease_held = lease == "acquired"
        if lease == "unavailable":
            # The lease store is unreachable. Proceeding is the safer error: the
            # writes below either land or fail loudly, whereas skipping would let
            # geo go stale with nothing reporting why — the exact invisibility
            # this PR exists to remove.
            log.warning("[geo_sync] lease store unavailable — running without a lease")
        if lease == "held":
            skipped = {
                "status": "skipped_locked", "dry_run": dry_run,
                "date_from": start.isoformat(), "date_to": end.isoformat(),
                "reason": "another_geo_sync_is_running",
                "customer_id": customer_id,
                "started_at": started_at, "finished_at": started_at,
                "summary": {"chunks_verified": 0, "chunks_failed": 0,
                            "chunks_skipped": 0, "rows_written": 0},
                "chunks": [], "errors": [],
            }
            state.update({"running": False, "phase": "skipped_locked",
                          "finished_at": started_at, "latest": skipped})
            log.warning("[geo_sync] lease not acquired — another run is in progress")
            return skipped

    summary = {"chunks_verified": 0, "chunks_failed": 0, "chunks_skipped": 0,
               "rows_written": 0, "geo_cost_micros": 0, "country_criteria": 0,
               "coverage_start": None, "coverage_end": None}
    chunks_detail: list = []
    errors: list = []
    seen_countries: set = set()

    already_verified, ledger_readable = (
        _verified_chunk_keys(start, end) if (resume and not dry_run) else (set(), True))
    if resume and not dry_run and not ledger_readable:
        log.warning("[geo_sync] coverage ledger unreadable — re-fetching the full range")

    def _emit(status):
        if checkpoint is None:
            return
        try:
            payload = {
                "status": status, "phase": state.get("phase"),
                "current_chunk": state.get("current_chunk"),
                "summary": dict(summary), "chunks": list(chunks_detail), "errors": list(errors),
            }
            if state.get("finished_at"):
                payload["finished_at"] = state["finished_at"]
            checkpoint(job_id, payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("[geo_sync] checkpoint failed: %s", exc)

    try:
        return _run_chunks(
            start=start, end=end, chunk_months=chunk_months, dry_run=dry_run,
            job_id=job_id, customer_id=customer_id, state=state, summary=summary,
            chunks_detail=chunks_detail, errors=errors, seen_countries=seen_countries,
            already_verified=already_verified, resume=resume, started_at=started_at,
            emit=_emit, db_writers=db_writers, relativedelta=relativedelta,
        )
    except BaseException:
        # An unexpected failure must not leave the lease held until it expires —
        # that would block the next scheduled run for the whole lease window.
        if lease_held:
            db_writers.release_geo_sync_lease(customer_id, status="failed")
        state.update({"running": False, "phase": "failed"})
        raise


def _run_chunks(*, start, end, chunk_months, dry_run, job_id, customer_id, state,
                summary, chunks_detail, errors, seen_countries, already_verified,
                resume, started_at, emit, db_writers, relativedelta) -> dict:
    """The chunk loop for :func:`run_google_ads_geo_sync`.

    Split out only so the caller can hold the durable lease across it and release
    it on any unexpected failure. Contains no policy the caller does not state.
    """
    _emit = emit
    cursor = start
    step = relativedelta(months=max(1, chunk_months))
    while cursor <= end:
        chunk_to = min(cursor + step - relativedelta(days=1), end)
        chunk_key = f"{cursor.isoformat()}:{chunk_to.isoformat()}"
        state["current_chunk"] = chunk_key

        if chunk_key in already_verified:
            summary["chunks_skipped"] += 1
            chunks_detail.append({"chunk": chunk_key, "rows": 0, "cost_micros": 0,
                                  "status": "skipped_verified"})
            if summary["coverage_start"] is None:
                summary["coverage_start"] = cursor.isoformat()
            summary["coverage_end"] = chunk_to.isoformat()
            cursor = chunk_to + relativedelta(days=1)
            continue

        state["phase"] = "fetching"
        chunk = {"chunk": chunk_key, "rows": 0, "cost_micros": 0, "status": "verified"}
        try:
            payload = fetch_geo_daily(cursor.isoformat(), chunk_to.isoformat())
            rows = payload.get("rows", [])
            micros = sum(int(r.get("cost_micros") or 0) for r in rows)
            for r in rows:
                seen_countries.add(r.get("country_criterion_id"))
            # Resolve criterion ids -> country code/name so the canonical geo table
            # itself carries the country identity that feeds named country rows.
            _resolve_country_metadata(rows)
            chunk["rows"] = len(rows)
            chunk["cost_micros"] = micros
            if not dry_run:
                written = db_writers.upsert_geo_daily_spend(rows, sync_run_id=job_id)
                # Fail closed: a successful READ that did not persist must not be
                # recorded as covered. Without this, a write outage would quietly
                # produce verified-but-empty ranges the gate would treat as real.
                if rows and written != len(rows):
                    raise GeoPersistenceError(
                        f"geo upsert wrote {written}/{len(rows)} rows")
                summary["rows_written"] += written
                # Only NOW is the chunk provably covered.
                if not db_writers.upsert_geo_coverage(
                        customer_id, cursor.isoformat(), chunk_to.isoformat(),
                        "verified", rows_written=written, cost_micros_total=micros,
                        country_count=len({r.get("country_criterion_id")
                                           for r in rows if r.get("country_criterion_id")}),
                        source_query_version=payload.get("source_query_version"),
                        sync_run_id=job_id):
                    raise GeoPersistenceError("geo coverage-ledger upsert failed")
            summary["geo_cost_micros"] += micros
            summary["chunks_verified"] += 1
        except Exception as exc:  # noqa: BLE001
            chunk["status"] = "failed"
            chunk["error"] = str(exc)[:500]
            summary["chunks_failed"] += 1
            errors.append(f"{chunk_key}: {exc}")
            if not dry_run:
                # Durable failure evidence so the next run retries exactly this
                # range. Never demotes an already-verified chunk (writer guard).
                db_writers.upsert_geo_coverage(
                    customer_id, cursor.isoformat(), chunk_to.isoformat(), "failed",
                    error_message=str(exc)[:1000], sync_run_id=job_id)

        chunks_detail.append(chunk)
        if summary["coverage_start"] is None:
            summary["coverage_start"] = cursor.isoformat()
        summary["coverage_end"] = chunk_to.isoformat()
        _emit("running")
        cursor = chunk_to + relativedelta(days=1)

    summary["country_criteria"] = len([c for c in seen_countries if c])
    summary["geo_total_spend"] = round(summary["geo_cost_micros"] / 1_000_000, 6)
    finished_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    status = "failed" if (errors and not (summary["chunks_verified"] or summary["chunks_skipped"])) \
        else ("partial" if errors else "success")

    # Coverage is re-read from the LEDGER, not inferred from this run's counters:
    # a run that skipped every chunk because they were already verified is
    # complete, and a run that verified every chunk it attempted is still
    # incomplete if an earlier failed chunk remains unrepaired.
    coverage = analyze_geo_coverage(start, end) if not dry_run else {
        "available": False, "complete": False, "reason": "dry_run"}
    coverage_complete = bool(coverage.get("complete"))

    if not dry_run:
        state_fields = {
            "last_status": status,
            "last_finished_at": datetime.now(tz=timezone.utc),
            "requested_start": start,
            "requested_end": end,
            "chunks_verified": summary["chunks_verified"],
            "chunks_failed": summary["chunks_failed"],
            "chunks_skipped": summary["chunks_skipped"],
            "rows_written": summary["rows_written"],
            "last_error": ("; ".join(errors)[:1000] or None),
            "last_run_id": job_id,
        }
        # The checkpoint and the "last successful completion" marker advance ONLY
        # when the ledger itself says the requested window is fully covered. A
        # partial run therefore cannot publish healthy freshness, which is the
        # whole point of keeping them out of the counter-based summary above.
        if status == "success" and coverage_complete:
            state_fields["checkpoint_date"] = end
            state_fields["last_successful_completed_at"] = datetime.now(tz=timezone.utc)
        db_writers.upsert_geo_sync_state(customer_id, **state_fields)

    result = {
        "status": status, "dry_run": dry_run,
        "customer_id": customer_id,
        "date_from": start.isoformat(), "date_to": end.isoformat(),
        "chunk_months": chunk_months, "started_at": started_at, "finished_at": finished_at,
        "resume": resume,
        "coverage_complete": coverage_complete,
        "coverage_missing_ranges": coverage.get("missing_ranges", []),
        "coverage_failed_chunks": coverage.get("failed_chunks", []),
        "summary": summary, "chunks": chunks_detail, "errors": errors,
    }
    state.update({"running": False, "phase": "done", "finished_at": finished_at,
                  "current_chunk": None, "latest": result})
    _emit(status)
    return result
