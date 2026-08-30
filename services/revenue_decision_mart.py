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
from datetime import datetime, timedelta, timezone

# PR-ADS-154C: THE canonical window anchor — the Google Ads account calendar
# day, not UTC. See services/canonical_contract.resolve_canonical_window.
from services.canonical_contract import resolve_canonical_window
from services import canonical_contract
from analysis import country_identity, revenue_scope
from db import revenue_repository as repo
from services import canonical_revenue_service as canonical_revenue
from services import google_ads_geo_sync_service as geo_gate
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
    # PR-ADS-153F: publish the EXACT instants the window resolved to, not only
    # its calendar dates. A consumer comparing two pages needs to see that both
    # used the same inclusive-start / exclusive-end UTC bounds — dates alone
    # leave the day-boundary convention implicit, which is exactly where
    # timezone-dependent population differences hide.
    #
    # The instants are DERIVED from the dates this window already resolved to,
    # not recomputed from the window key. Calling `get_window_bounds(key)` again
    # would re-resolve against the current clock, so a mart built with an
    # explicit `now` (a deterministic run, a previous-period comparison) could
    # disclose bounds for a different window than the one it actually used —
    # publishing a second answer to a question that already has one, which is
    # the defect class this whole programme exists to remove.
    start_utc = end_utc = None
    key = resolved.get("key")
    try:
        if resolved.get("start_date"):
            start_utc = datetime.fromisoformat(
                str(resolved["start_date"])).replace(tzinfo=timezone.utc).isoformat()
        if resolved.get("end_date"):
            # The upper bound is EXCLUSIVE: the start of the day after end_date,
            # matching analysis.business_windows.get_window_bounds.
            end_dt = datetime.fromisoformat(str(resolved["end_date"])).replace(
                tzinfo=timezone.utc) + timedelta(days=1)
            end_utc = end_dt.isoformat()
    except (TypeError, ValueError):  # disclosure must never break the mart
        logger.warning("window bounds unavailable for key=%s", key)
    return {
        "key": key,
        "label": resolved.get("label"),
        "start_date": resolved.get("start_date"),
        "end_date": resolved.get("end_date"),
        "start_utc": start_utc,
        "end_utc_exclusive": end_utc,
        "bounds": "inclusive_start_exclusive_end_utc",
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
    # PR-ADS-131: a by-design unattributed residual (geo assigns most spend to
    # countries; the shortfall is location-less spend geographic_view omits) is a
    # SAFE unblock — reconciled_with_residual, distinct from a plain verified
    # reconcile and from a blocking mismatch. The residual is surfaced as an explicit
    # bucket, never distributed across countries, never loosening tolerance.
    #
    # PR-ADS-153F: derived by THE shared gate rather than re-implemented here.
    # This block and services/google_ads_geo_sync_service used to compute the
    # same status independently, and the audit below used a stricter bar than
    # Dashboard Countries — so one window could be ready on one page and blocked
    # on another. Same states, same ordering, one implementation.
    #
    # PR-ADS-153F blocker 1: the gate takes ALL its inputs. Passing only the
    # reconciliation verdict let two unproven totals that happened to agree
    # become `verified` while campaign coverage, FX or geo coverage was
    # incomplete — matching numbers are not evidence when the inputs behind them
    # were never established.
    country_spend_status, country_gap_codes = geo_gate.resolve_country_spend_status(
        reconciled=health.get("country_spend_reconciled"),
        residual_eligible=bool(health.get("country_residual_eligible")),
        campaign_spend_readable=(health.get("spend_source") is not None),
        campaign_coverage_complete=(health.get("spend_coverage_status") == "complete"),
        fx_complete=(health.get("fx_coverage_status") == "complete"),
        # Read fail-CLOSED. These used to be `.get(key, True)` and
        # `!= "unavailable"`, so a `source_health` that never published the field
        # asserted the fact rather than admitting it was absent — the same
        # permissive-default defect the gate signature just removed, one layer up.
        geo_readable=(health.get("country_geo_query_readable") is True),
        geo_coverage_readable=(health.get("geo_coverage_status") in ("complete", "incomplete")),
        geo_coverage_complete=(health.get("geo_coverage_status") == "complete"),
        geo_failed_chunks=health.get("failed_geo_dates") or [],
        missing_geo_dates=health.get("missing_geo_dates") or [],
        campaigns_missing_geo=health.get("campaigns_missing_geo") or [],
        # PR-ADS-154B §2, read fail-CLOSED for the same reason as the three
        # above: absent evidence that the two sides were measured at the same
        # scope is not evidence that they were.
        comparison_like_for_like=(health.get("comparison_like_for_like") is True),
    )
    country_roas_unblockable = geo_gate.country_geo_ready(country_spend_status)

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
        # PR-ADS-129: auditable coverage classification — distinguishes a window
        # whose earlier dates were verified-zero (coverage complete) from one whose
        # earlier dates were never fetched / failed (coverage incomplete).
        "campaign_spend_coverage_status": health.get("campaign_spend_coverage_status"),
        "campaign_spend_coverage_reason": health.get("campaign_spend_coverage_reason"),
        "spend_coverage_detail": health.get("spend_coverage_detail") or {},
        # PR-ADS-153F: durable geo coverage evidence + the machine gap reason, so
        # a blocked country view can say WHICH ranges are missing or failed
        # rather than only that the totals differ.
        "geo_coverage_status": health.get("geo_coverage_status"),
        "country_gap_codes": country_gap_codes,
        "geo_coverage_missing_ranges": health.get("geo_coverage_missing_ranges") or [],
        "failed_geo_dates": health.get("failed_geo_dates") or [],
        "country_gap_reason": health.get("country_gap_reason"),
        # PR-ADS-129: exact geo↔canonical variance so the Country blocked card shows
        # real totals (campaign vs geo, variance, tolerance) — never fabricated.
        "campaign_spend_total": native_spend,
        "country_geo_total": _round2(health.get("country_geo_total")),
        "country_spend_variance": _round2(health.get("country_spend_variance")),
        "country_spend_variance_pct": health.get("country_spend_variance_pct"),
        "country_spend_tolerance": health.get("country_spend_tolerance"),
        # PR-ADS-131: safe unblock via an explicit residual bucket. country ROAS is
        # unblockable when spend is verified OR reconciled-with-residual.
        "country_roas_unblockable": country_roas_unblockable,
        "country_residual_native": _round2(health.get("country_residual_native")),
        "country_residual_usd": _round2(health.get("country_residual_usd")),
        "country_residual_pct": health.get("country_residual_pct"),
        "country_residual_label": (
            country_identity.RESIDUAL_LABEL
            if country_spend_status == "reconciled_with_residual" else None),
        "country_residual_reason": (
            "Google Ads geographic view does not assign this spend to a country."
            if country_spend_status == "reconciled_with_residual" else None),
    }


def _summary_block(core: dict, spend_truth: dict) -> dict:
    """Canonical top-line summary shared by every view.

    ``spend_usd`` is the canonical USD spend denominator (the same number every
    page must use). ``roas`` is exactly the revenue-attribution ROAS, which is
    already None whenever the spend denominator is unsafe — the mart never
    re-derives ROAS from an unsafe denominator.

    Two populations, named (PR-ADS-153E-B)
    --------------------------------------
    ``customers`` and ``won_revenue_usd`` are the BUSINESS totals: every won deal
    in the window, at ``all_source`` scope, straight from the canonical ledger.
    Until this PR they were the campaign-attributed figures, which is how the
    executive KPI on the Overview and Revenue pages came to exclude every won
    deal that arrived without Google Ads attribution.

    ``attributed_customers`` / ``attributed_won_revenue_usd`` keep the
    campaign-attributable subset, and they — not the business totals — remain the
    ROAS numerator. Dividing all-source revenue by Google Ads spend would credit
    advertising with revenue it did not produce.
    """
    summary = core.get("summary") or {}
    ladder = core.get("attribution_coverage") or {}
    scopes = ladder.get("scopes") or {}
    all_source = scopes.get(revenue_scope.SCOPE_ALL_SOURCE) or {}
    attributed = scopes.get(revenue_scope.SCOPE_CAMPAIGN_ATTRIBUTABLE) or {}
    ladder_available = bool(ladder.get("available"))

    # PR-ADS-154C-F3. `summarize_deals` returns the sum of the deals whose
    # currency WAS proven as soon as at least one such deal exists. Over a
    # population holding one deal with no proven amount, that is a partial figure
    # — smaller than the truth — and the mart published it as the business total
    # under `truth_status: ready`. Channels, Campaigns, Countries and Deals each
    # already refused a partial sum; the mart, and therefore the Overview and
    # Revenue pages that read it, were the exception. That is how All Time came
    # to show $878,324.80 on three pages and "unavailable" on four.
    #
    # PR-ADS-154C-F3-F1: the verdict is READ from the scope ladder, which gets it
    # from `canonical_revenue_service.revenue_total_publishable`. The first cut
    # of this fix re-derived the same condition here — a fifth private copy of
    # the rule the shared function existed to consolidate, and the exact shape of
    # drift that let the mart diverge from four other consumers in the first
    # place. The mart now decides nothing about revenue completeness.
    #
    # The COUNT is unaffected: `won_deals` is complete whatever the amounts are,
    # so customers stay published. Blanking a count we did measure would be its
    # own fabrication.
    all_source_revenue_ok = bool(ladder_available
                                 and all_source.get("revenue_total_available"))
    attributed_revenue_ok = bool(ladder_available
                                 and attributed.get("revenue_total_available"))
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
        # Business totals — all_source. None (not 0) when the canonical ledger
        # could not be read or its coverage is unproven.
        "customers": all_source.get("won_deals") if ladder_available else None,
        "won_revenue_usd": all_source.get("revenue_usd") if all_source_revenue_ok else None,
        "revenue_scope": revenue_scope.SCOPE_ALL_SOURCE,
        "revenue_source": canonical_revenue.CANONICAL_SOURCE,
        "revenue_available": ladder_available,
        # Separate from `revenue_available`: the POPULATION can be readable while
        # its total is unknown because a deal in it has no proven amount.
        "revenue_total_available": all_source_revenue_ok,
        "attributed_revenue_total_available": attributed_revenue_ok,
        # The reason comes from the ladder's verdict too, so the mart cannot
        # describe a refusal it did not make.
        "revenue_total_unavailable_reason": (
            None if all_source_revenue_ok else (
                all_source.get("revenue_total_unavailable_reason")
                if ladder_available else ladder.get("reason"))),
        # PR-ADS-154C-F3-F1 §4: the machine-readable half of the same refusal,
        # from the canonical helper. Every page that withholds revenue republishes
        # these rather than inventing its own vocabulary for one decision.
        "revenue_total_violation_codes": (
            [] if all_source_revenue_ok else (
                all_source.get("revenue_total_violation_codes") or []
                if ladder_available else sorted(ladder.get("violation_codes") or []))),
        "currency_unavailable_deals": (all_source.get("currency_unavailable_deals")
                                       if ladder_available else None),
        # PR-ADS-155 §5. The whole fail-closed picture in one block: the complete
        # deal count, how many of them are priced, how many are not, what the
        # priced ones are worth under a label that names its own denominator, and
        # the NULL total with its reason. Built by the canonical contract from
        # this same ladder — the mart still decides nothing about revenue.
        "revenue_disclosure": canonical_revenue.disclosure_from_ladder(
            ladder, revenue_scope.SCOPE_ALL_SOURCE),
        "attributed_revenue_disclosure": canonical_revenue.disclosure_from_ladder(
            ladder, revenue_scope.SCOPE_CAMPAIGN_ATTRIBUTABLE),
        "ambiguous_associations": (all_source.get("ambiguous_associations")
                                   if ladder_available else None),
        "failed_associations": (all_source.get("failed_associations")
                                if ladder_available else None),
        # Advertising subset — the ROAS numerator and its scope. Same rule: a
        # partial sum is not the subset's total either.
        "attributed_customers": summary.get("customers"),
        "attributed_won_revenue_usd": (_round2(summary.get("won_revenue"))
                                       if attributed_revenue_ok else None),
        "attributed_revenue_scope": revenue_scope.SCOPE_CAMPAIGN_ATTRIBUTABLE,
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
        detail = spend_truth.get("spend_coverage_detail") or {}
        reason = spend_truth.get("campaign_spend_coverage_reason")
        missing = detail.get("missing_chunks") or []
        failed = detail.get("failed_chunks") or []
        req_start = detail.get("requested_start")
        first_spend = detail.get("first_spend_date")
        # PR-ADS-129: name the exact missing/unverified period so the reason is
        # actionable — never a bare "incomplete".
        if failed:
            extra = (" One or more Google Ads spend backfill chunks FAILED — "
                     "re-run the spend backfill for the affected dates.")
        elif missing:
            gap = missing[0]
            extra = (f" Durable spend is unverified for {gap.get('date_from')} → "
                     f"{gap.get('date_to')} (reason: {reason}). Run the Google Ads "
                     "spend backfill for that period, or verify it was zero spend.")
        elif req_start and first_spend and first_spend > req_start:
            extra = (f" Durable spend rows start {first_spend}; verify "
                     f"{req_start} → {first_spend} was zero spend to unblock ROAS.")
        else:
            extra = ""
        diags.append({
            "code": "campaign_spend_coverage",
            "message": (
                "Canonical campaign spend coverage is "
                f"'{spend_truth['campaign_spend_status']}' — ROAS is unavailable "
                f"(never a fake $0) until coverage is complete.{extra}"
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
    if spend_truth["country_spend_status"] == "reconciled_with_residual":
        res_native = spend_truth.get("country_residual_native")
        res_pct = spend_truth.get("country_residual_pct")
        cur = spend_truth.get("native_currency") or "GBP"
        diags.append({
            "code": "country_spend_residual",
            "message": (
                "Country ROAS is shown with an explicit "
                f"'{country_identity.RESIDUAL_LABEL}' residual of {res_native} {cur}"
                f"{'' if res_pct is None else f' ({res_pct}%)'} — Google Ads assigned "
                "most spend to countries, and this shortfall is spend the geographic "
                "view does not assign to any country. The residual is surfaced, never "
                "spread across real countries; real country rows use country-attributed "
                "spend only."
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
    # PR-ADS-131: country ROAS is decision-ready when the spend denominator is
    # verified OR reconciled-with-residual (real country rows + explicit residual).
    country_decision_ready = bool(
        spend_decision_ready
        and geo_gate.country_geo_ready(spend_truth["country_spend_status"])
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


def _view_payload(view: str, window: str, core: dict, now: datetime | None) -> dict:
    """Rows for the requested grain plus any view-specific health/summary.

    Returns {rows, ledger_summary, data_health}:
      - rows: the controlled-grain rows for this view.
      - ledger_summary: the deal view's OWN ledger summary (deal_count /
        won_revenue / average_deal_value / exact_gclid_count) so the Deals page
        derives every KPI from the SAME source as its rows — never mixing the
        canonical attribution total with the raw deal-ledger count.
      - data_health: view-specific availability the canonical spend_truth cannot
        carry. For the deal view this surfaces deal_ledger_status so a durable
        ledger OUTAGE is never silently rendered as a truthful empty window.
    """
    payload = {"rows": [], "ledger_summary": None, "data_health": {},
               "source_spend_truth": None}
    if view == "campaign":
        payload["rows"] = core.get("campaigns") or []
    elif view == "country":
        payload["rows"] = core.get("countries") or []
    elif view == "source":
        source = build_revenue_by_source(window, now=now)
        payload["rows"] = source.get("groups") or []
        # PR-ADS-140: carry the Google Ads spend-truth proof block so the page can
        # prove its Google Ads spend reconciles with the canonical top-line.
        payload["source_spend_truth"] = source.get("source_spend_truth")
    elif view == "deal":
        deals = build_revenue_deals(window, now=now)
        payload["rows"] = deals.get("deals") or []
        payload["ledger_summary"] = deals.get("summary")
        payload["data_health"] = {
            "deal_ledger_status": (deals.get("source_health") or {}).get("ledger_status"),
        }
    return payload


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
    resolve_canonical_window(window, now=now)

    core = _canonical_core(window, now)

    spend_truth = _spend_truth_block(core)
    summary = _summary_block(core, spend_truth)
    readiness = _readiness_block(core, spend_truth)
    payload = _view_payload(view, window, core, now)
    diagnostics = _diagnostics(core, view, spend_truth)

    # PR-ADS-152: the mart top-line (and ROAS by Campaign) is the
    # campaign-attributable SQL subset — campaign ROAS requires campaign identity.
    # Disclose that scope explicitly and reconcile against the canonical
    # population, so the top-line is never mistaken for every Google Ads-source SQL.
    from services import canonical_contact_outcome_service as _canon  # noqa: PLC0415
    sql_reconciliation = _canon.page_reconciliation(
        _canon.WINDOW_BUSINESS, window, _canon.SCOPE_CAMPAIGN_ATTRIBUTABLE,
        now=now, consumer_count=summary.get("sqls"))

    window_block = _window_block(core)
    core_health = core.get("source_health") or {}
    return {
        "view": view,
        "window": window_block,
        "spend_truth": spend_truth,
        # PR-ADS-153F §8: the shared country-truth disclosure, present on the
        # country view so ROAS by Country and Dashboard Countries disclose the
        # SAME facts — sources, scope, exact UTC bounds, freshness, coverage,
        # reconciliation, residual, and whether the residual was accepted.
        "country_truth": (geo_gate.build_country_truth_disclosure(
            spend_truth, window_block,
            revenue_source=canonical_revenue.CANONICAL_SOURCE,
            revenue_scope=revenue_scope.SCOPE_ALL_SOURCE,
            revenue_available=core_health.get("revenue_available", True),
            revenue_reason=core_health.get("revenue_unavailable_reason"),
            revenue_violation_codes=core_health.get("revenue_violation_codes"),
            as_of=core_health.get("revenue_as_of"),
        ) if view == "country" else None),
        "summary": summary,
        # PR-ADS-152: canonical SQL-scope reconciliation metadata (§7). The mart's
        # ``summary.sqls`` is the campaign-attributable subset, not every Google
        # Ads-source SQL — disclosed here.
        "sql_reconciliation": sql_reconciliation,
        "readiness": readiness,
        "rows": payload["rows"],
        # Deal view: the ledger's own summary (consistent with its rows) and the
        # ledger availability signal. None / empty for the other views.
        "ledger_summary": payload["ledger_summary"],
        # Source view (PR-ADS-140): canonical Google Ads spend-truth proof block.
        "source_spend_truth": payload.get("source_spend_truth"),
        "data_health": payload["data_health"],
        "diagnostics": diagnostics,
        "source_truth": "revenue_decision_mart",
        "google_ads_conversion_value_used": False,
        # PR-ADS-154C-F1: per-metric provenance. The mart publishes spend AND
        # revenue AND customers, so one response-level source name describes at
        # most one of them correctly.
        canonical_contract.METRIC_TRUTH_KEY: canonical_contract.metric_truth_block(
            window_block, [
                {"metric": "google_ads_spend_usd",
                 "data_source": canonical_contract.SOURCE_CANONICAL_SPEND,
                 "scope": "google_ads_campaign_spend",
                 "truth_status": (canonical_contract.TRUTH_READY
                                  if spend_truth.get("campaign_spend_status") == "verified"
                                  else canonical_contract.TRUTH_NOT_READY),
                 "customer_id": spend_truth.get("customer_id")},
                # PR-ADS-154C-F3: the REVENUE contract follows
                # `revenue_total_available`, not `revenue_available`. The
                # population can be readable while its total is unknown because
                # a deal in it has no proven amount — and a `ready` contract over
                # a withheld figure is the contradiction the parity audit exists
                # to catch.
                {"metric": "closed_won_revenue_usd",
                 "data_source": canonical_contract.SOURCE_REVENUE_DECISION_MART,
                 "scope": "all_source_business_revenue",
                 "truth_status": (canonical_contract.TRUTH_READY
                                  if summary.get("revenue_total_available")
                                  else canonical_contract.TRUTH_NOT_READY),
                 "unavailable_reason": summary.get("revenue_total_unavailable_reason"),
                 "violation_codes": summary.get("revenue_total_violation_codes")},
                {"metric": "customers",
                 "data_source": canonical_contract.SOURCE_REVENUE_DECISION_MART,
                 "scope": "all_source_business_revenue",
                 "truth_status": (canonical_contract.TRUTH_READY
                                  if summary.get("revenue_available")
                                  else canonical_contract.TRUTH_NOT_READY)},
                # The advertising SUBSET. Same authority, different population —
                # so it carries its own scope and is never compared with the
                # all-source pair above.
                *[{"metric": m,
                   "data_source": canonical_contract.SOURCE_REVENUE_DECISION_MART,
                   "scope": "campaign_attributable_revenue",
                   "truth_status": (canonical_contract.TRUTH_READY
                                    if summary.get(field) is not None
                                    else canonical_contract.TRUTH_NOT_READY)}
                  for m, field in (
                      ("campaign_attributed_won_revenue_usd",
                       "attributed_won_revenue_usd"),
                      ("campaign_attributed_customers", "attributed_customers"))],
                # The mart's lead population, from which every page's "SQLs"
                # headline derives. Distinct from the canonical contact funnel's
                # lifecycle stages, which are dated by their own stage-entry
                # timestamps.
                *[{"metric": m,
                   "data_source": canonical_contract.SOURCE_REVENUE_DECISION_MART,
                   "scope": scope,
                   "truth_status": (canonical_contract.TRUTH_READY
                                    if summary.get(field) is not None
                                    else canonical_contract.TRUTH_NOT_READY)}
                  for m, scope, field in (
                      ("campaign_attributable_sqls", "campaign_attributable_sqls",
                       "sqls"),
                      ("campaign_attributable_leads", "campaign_attributable_leads",
                       "leads"))],
            ]),
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
    # PR-ADS-153F: the audit uses THE shared gate. It previously demanded
    # `== "verified"`, a stricter bar than every page it audits, so a window that
    # Dashboard Countries and the mart both accepted as
    # `reconciled_with_residual` was still reported here as differing "because
    # of geo reconciliation" — an explanation for a difference that the accepted
    # state does not cause.
    if page_key == "roas_by_country" and not geo_gate.country_geo_ready(
            spend_truth["country_spend_status"]):
        reasons.append("different_geo_reconciliation")
    if page_key == "revenue_by_source":
        # PR-ADS-140: Revenue by Source now reads the SAME canonical campaign-daily
        # spend as the mart (geo spend is diagnostic only), so a difference is
        # NEVER a different-source-table mismatch. Any residual difference is an
        # FX-coverage difference — the source USD spend is withheld until FX is safe.
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
    resolve_canonical_window(window, now=now)

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
