"""
Campaign-level Google Ads spend reconciliation drilldown (PR-ADS-122).

An AUDIT + PROOF layer: for a single ROAS campaign row, prove its spend by
keeping THREE distinct totals separate (never conflated):

  1. LOCAL canonical DB total (google_ads_campaign_daily_spend), and
  2. a FRESH Google Ads campaign-level API total (ALL ad-group statuses), and
  3. an ad-group-level breakdown WITH status (enabled / paused / removed).

Reconciliation rules:
  - The PRIMARY reconciliation is (1) vs (2): local canonical spend and the
    fresh campaign-level API total are EXPECTED to match. A mismatch means a
    canonical spend problem (stale local spend, a date-boundary issue, or a
    campaign identity/mapping issue) — it is NOT explained by the ad-group
    enabled filter.
  - The Google Ads UI screenshot (Campaign status: Enabled + Ad group status:
    Enabled) is explained ONLY by the enabled-only ad-group total — never by the
    campaign-level API total.
  - We never assert that enabled-only ad-group spend equals the campaign-level
    API total unless the live ad-group data proves it (the enabled-only vs
    campaign-minus-paused/removed variance is reported so the equality is
    earned, not assumed).

Doctrine: this is read-only. It NEVER changes the ROAS spend source and NEVER
writes to Google Ads. Canonical spend stays the Google Ads campaign-level total
unless reconciliation proves it wrong.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from db import revenue_repository as repo
from services.google_ads_spend_service import (
    SPEND_VARIANCE_TOLERANCE,
    _window_bounds,
    analyze_coverage,
    configured_customer_id,
)

log = logging.getLogger(__name__)

# Ad-group statuses the Google Ads UI hides under "Ad group status: Enabled".
_NON_ENABLED_STATUSES = ("PAUSED", "REMOVED")


def fetch_campaign_api_spend(start_date: str, end_date: str, campaign_id: str) -> dict:
    """Seam over the campaign-level Google Ads connector (late import). Read-only.

    Patched directly by tests so the google-ads SDK is never imported.
    """
    from connectors.google_ads_direct import (  # noqa: PLC0415
        fetch_campaign_daily_spend_for_campaign as _f,
    )
    return _f(start_date, end_date, campaign_id)


def fetch_ad_group_spend(start_date: str, end_date: str, campaign_id: str) -> dict:
    """Seam over the ad-group-level Google Ads connector (late import). Read-only.

    Patched directly by tests so the google-ads SDK is never imported.
    """
    from connectors.google_ads_direct import fetch_ad_group_daily_spend as _f  # noqa: PLC0415
    return _f(start_date, end_date, campaign_id)


def _micros_to_native(micros) -> float:
    return round(int(micros or 0) / 1_000_000, 6)


def _variance(a: float | None, b: float | None) -> dict:
    """Variance of a minus b, with a percentage relative to b. Never hides it.

    Returns {amount, pct, within_tolerance}. amount/pct are None when either
    side is unavailable (so a missing total is never read as a £0 reconciliation).
    """
    if a is None or b is None:
        return {"amount": None, "pct": None, "within_tolerance": None}
    amount = round(a - b, 6)
    if b == 0:
        pct = 0.0 if amount == 0 else None
        within = (amount == 0)
    else:
        pct = round((abs(amount) / b) * 100, 4)
        within = abs(amount) / b <= SPEND_VARIANCE_TOLERANCE
    return {"amount": amount, "pct": pct, "within_tolerance": within}


def _aliases_for_campaign(customer_id, campaign_id) -> list:
    """Mapped external labels (aliases) pointing at this canonical campaign.

    Read from the durable identity table — only approved mappings. Never touches
    the raw spend-table identity.
    """
    try:
        identity = repo.fetch_campaign_identity(customer_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("[reconcile] identity fetch failed: %s", exc)
        return []
    aliases = sorted({
        m.get("external_campaign_label")
        for m in (identity.get("mappings") or [])
        if str(m.get("campaign_id")) == str(campaign_id)
        and m.get("external_campaign_label")
        and m.get("match_method") != "not_google_ads"
    })
    return aliases


def _ad_group_breakdown(start: str, end: str, campaign_id: str) -> dict:
    """Ad-group-level spend totals by status (enabled / paused / removed).

    Best-effort: ad-group data may be unavailable for some accounts; that is
    reported as unavailable, never silently treated as zero. Read-only.

    Returns {available, statuses, total_native (all statuses), enabled_only,
    paused_removed, currency_code}.
    """
    try:
        payload = fetch_ad_group_spend(start, end, campaign_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("[reconcile] ad-group fetch failed: %s", exc)
        return {"available": False, "statuses": [], "reason": "ad-group spend fetch failed"}

    rows = payload.get("rows", [])
    by_status: dict[str, dict] = {}
    for r in rows:
        status = (r.get("ad_group_status") or "UNKNOWN").upper()
        entry = by_status.setdefault(status, {"cost_micros": 0, "ad_group_ids": set()})
        entry["cost_micros"] += int(r.get("cost_micros") or 0)
        if r.get("ad_group_id") is not None:
            entry["ad_group_ids"].add(str(r.get("ad_group_id")))

    statuses = sorted(
        (
            {
                "status": status,
                "cost_micros": data["cost_micros"],
                "spend": _micros_to_native(data["cost_micros"]),
                "ad_group_count": len(data["ad_group_ids"]),
            }
            for status, data in by_status.items()
        ),
        key=lambda s: s["cost_micros"],
        reverse=True,
    )

    total_micros = sum(s["cost_micros"] for s in statuses)
    enabled_micros = sum(s["cost_micros"] for s in statuses if s["status"] == "ENABLED")
    non_enabled_micros = sum(
        s["cost_micros"] for s in statuses if s["status"] in _NON_ENABLED_STATUSES
    )
    return {
        "available": True,
        "statuses": statuses,
        "total_native": _micros_to_native(total_micros),
        "enabled_only": _micros_to_native(enabled_micros),
        "paused_removed": _micros_to_native(non_enabled_micros),
        "currency_code": payload.get("currency_code"),
    }


def _coverage_state_for_date(d: date, verified_ranges: list) -> str:
    """'verified' when the date falls inside a verified spend chunk, else 'unverified'."""
    for cs, ce in verified_ranges:
        if cs <= d <= ce:
            return "verified"
    return "unverified"


def _build_daily(local_rows: list, api_rows: list, verified_ranges: list) -> list:
    """Merge local + API daily spend into one proof table keyed by date."""
    local_by_date: dict[str, int] = {}
    for r in local_rows:
        d = r.get("spend_date")
        d = d.isoformat() if hasattr(d, "isoformat") else str(d)
        local_by_date[d] = local_by_date.get(d, 0) + int(r.get("cost_micros") or 0)

    api_by_date: dict[str, int] = {}
    api_available = api_rows is not None
    for r in (api_rows or []):
        d = r.get("spend_date")
        d = d.isoformat() if hasattr(d, "isoformat") else str(d)
        api_by_date[d] = api_by_date.get(d, 0) + int(r.get("cost_micros") or 0)

    daily = []
    for d in sorted(set(local_by_date) | set(api_by_date)):
        local_micros = local_by_date.get(d, 0)
        api_micros = api_by_date.get(d) if api_available else None
        local_native = _micros_to_native(local_micros)
        api_native = _micros_to_native(api_micros) if api_micros is not None else None
        var = round(local_native - api_native, 6) if api_native is not None else None
        try:
            d_obj = date.fromisoformat(d)
            cov = _coverage_state_for_date(d_obj, verified_ranges)
        except ValueError:
            cov = "unverified"
        daily.append({
            "date": d,
            "local_cost": local_native,
            "api_cost": api_native,
            "variance": var,
            "coverage_state": cov,
        })
    return daily


def _possible_causes(*, primary, ag, ui_vs_enabled, currency: str) -> list:
    """Explain the totals honestly. The campaign mismatch and the UI-filter gap
    are kept strictly separate — a canonical mismatch is NEVER blamed on the
    ad-group enabled filter, and enabled-only is never claimed equal to the
    campaign-level API total unless the live data proves it.
    """
    causes = []

    # 1) PRIMARY reconciliation: local canonical vs fresh campaign-level API.
    if primary["within_tolerance"] is None:
        causes.append(
            "Fresh campaign-level Google Ads API total is unavailable, so the "
            "primary reconciliation could not be completed (not treated as zero)."
        )
    elif primary["within_tolerance"]:
        causes.append(
            "Local canonical spend matches the fresh Google Ads campaign-level API "
            "total (all ad-group statuses) for this campaign and window."
        )
    else:
        causes.append(
            "Canonical spend mismatch: local canonical spend does NOT match the fresh "
            "Google Ads campaign-level API total. Likely stale local spend, a "
            "date-boundary issue, or a campaign identity/mapping issue — inspect the "
            "daily breakdown. This is NOT explained by the ad-group enabled filter."
        )

    # 2) Google Ads UI screenshot is explained ONLY by the enabled-only ad-group
    #    total — never by the campaign-level API total.
    if ag.get("available"):
        paused_removed = ag.get("paused_removed") or 0
        enabled_only = ag.get("enabled_only") or 0
        if paused_removed > 0:
            causes.append(
                f"Google Ads UI screenshots filter to Ad group status: Enabled. The "
                f"enabled-only ad-group total is {enabled_only:.2f} {currency} "
                f"({paused_removed:.2f} {currency} of paused/removed ad-group spend is "
                f"excluded) — that enabled-only figure is what the UI screenshot should "
                f"be compared against, NOT the campaign-level API total."
            )
            if ui_vs_enabled["within_tolerance"] is True:
                causes.append(
                    "Live data confirms the enabled-only ad-group total reconciles to "
                    "the campaign-level API total minus paused/removed ad-group spend."
                )
            elif ui_vs_enabled["within_tolerance"] is False:
                causes.append(
                    "Caution: the enabled-only ad-group total does NOT reconcile to the "
                    "campaign-level API total minus paused/removed ad-group spend, so "
                    "enabled-only is only an APPROXIMATE estimate of the UI number, not a "
                    "proven equality."
                )
        else:
            causes.append(
                "No paused/removed ad-group spend found, so a Google Ads UI "
                "'Ad group status: Enabled' filter does not explain a lower UI total."
            )
    else:
        causes.append(
            "Ad-group-level spend was not available, so the Google Ads UI "
            "'Ad group status: Enabled' filter could not be confirmed or ruled out."
        )
    return causes


def build_campaign_spend_reconciliation(
    window: str,
    campaign_id: str,
    *,
    now: datetime | None = None,
    include_ad_groups: bool = True,
    customer_id: str | None = None,
    api_payload: dict | None = None,
) -> dict:
    """Reconcile one campaign's local canonical spend against fresh API totals.

    ``customer_id`` scopes the LOCAL canonical reads to a single Google Ads
    account so the same campaign_id across accounts cannot contaminate the total;
    when None it falls back to the configured account customer id. ``api_payload``
    may be injected (tests / a pre-fetched live result); when None a best-effort
    live campaign-level API total is attempted and reported unavailable on failure
    (never silently treated as zero).

    Read-only. NEVER writes to Google Ads and NEVER changes the ROAS spend source.
    """
    if not campaign_id:
        raise ValueError("campaign_id is required")
    campaign_id = str(campaign_id).strip()
    if not campaign_id.isdigit():
        raise ValueError("campaign_id must be numeric")

    # Scope local canonical reads to a single account so multi-customer DBs cannot
    # cross-contaminate the same campaign_id across accounts.
    account_customer_id = customer_id or configured_customer_id()

    account_time_zone = repo.fetch_account_time_zone()
    resolved, start, end = _window_bounds(window, now, account_time_zone)
    date_from = resolved.get("start_date")
    date_to = resolved.get("end_date")

    local = repo.fetch_campaign_daily_spend_local(
        campaign_id, start, end, customer_id=account_customer_id)
    coverage = repo.fetch_spend_coverage(start, end)
    cov = analyze_coverage(start, end, coverage.get("chunks", []))
    verified_chunks = cov.get("verified_chunks", [])
    verified_ranges = [
        (date.fromisoformat(c["chunk_start"]), date.fromisoformat(c["chunk_end"]))
        for c in verified_chunks
    ]
    coverage_status = "complete" if cov.get("complete") else "incomplete"

    duplicates = repo.fetch_campaign_spend_duplicate_dates(
        campaign_id, start, end, customer_id=account_customer_id)
    duplicate_dates = duplicates.get("duplicates", [])

    if not local.get("available"):
        return {
            "customer_id": account_customer_id,
            "campaign_id": campaign_id,
            "campaign_name": None,
            "window": window,
            "date_from": date_from,
            "date_to": date_to,
            "account_time_zone": account_time_zone,
            "currency_code": None,
            "local_canonical_campaign_total": None,
            "fresh_campaign_api_total_all_statuses": None,
            "fresh_ad_group_total_all_statuses": None,
            "fresh_ad_group_total_enabled_only": None,
            "fresh_ad_group_total_paused_removed": None,
            "variance_local_vs_fresh_campaign_api": None,
            "variance_local_vs_ad_group_all_statuses": None,
            "variance_google_ui_filter_estimate_vs_enabled_only": None,
            "google_ui_filter_estimate": None,
            # Back-compat summary aliases (primary reconciliation).
            "local_total_native": None,
            "api_total_native": None,
            "variance_native": None,
            "variance_pct": None,
            "status": "unavailable",
            "coverage_status": coverage_status,
            "rows_counted": 0,
            "date_chunks_verified": len(verified_chunks),
            "mapped_aliases": [],
            "ad_group_breakdown": {"available": False, "statuses": []},
            "duplicate_local_rows": duplicate_dates,
            "possible_causes": ["Local canonical spend is unavailable for this campaign/window."],
            "daily": [],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    resolved_customer_id = account_customer_id or local.get("customer_id")
    currency_code = local.get("currency_code") or "GBP"
    campaign_name = local.get("campaign_name")
    local_total = round(float(local.get("total_spend") or 0.0), 6)
    rows_counted = int(local.get("rows_counted") or 0)
    local_rows = local.get("rows", [])

    # Fresh campaign-level API total (ALL ad-group statuses) for the SAME
    # campaign/date — queried fresh, never the canonical table.
    if api_payload is None:
        try:
            api_payload = fetch_campaign_api_spend(
                date_from or "2000-01-01", date_to, campaign_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("[reconcile] live campaign API total unavailable: %s", exc)
            api_payload = None

    if api_payload is not None:
        api_rows = api_payload.get("rows", [])
        api_total = _micros_to_native(sum(int(r.get("cost_micros") or 0) for r in api_rows))
        if not campaign_name:
            campaign_name = api_payload.get("campaign_name")
        if not account_time_zone:
            account_time_zone = api_payload.get("account_time_zone")
    else:
        api_rows = None
        api_total = None

    # Ad-group-level totals by status (separate from the campaign-level API total).
    ag = (
        _ad_group_breakdown(date_from or "2000-01-01", date_to, campaign_id)
        if include_ad_groups else {"available": False, "statuses": []}
    )
    ag_all = ag.get("total_native") if ag.get("available") else None
    ag_enabled = ag.get("enabled_only") if ag.get("available") else None
    ag_paused_removed = ag.get("paused_removed") if ag.get("available") else None

    # The Google Ads UI screenshot estimate is derived from the AUTHORITATIVE
    # campaign-level API total minus paused/removed ad-group spend — an
    # independent path from the direct enabled-only ad-group sum. The variance
    # between the two is what EARNS the right to say enabled-only ≈ campaign API
    # minus paused/removed (we never assume it).
    if api_total is not None and ag_paused_removed is not None:
        ui_filter_estimate = round(api_total - ag_paused_removed, 6)
    else:
        ui_filter_estimate = None

    # Variances — each pairing kept strictly separate.
    var_local_vs_campaign = _variance(local_total, api_total)
    var_local_vs_adgroup_all = _variance(local_total, ag_all)
    var_ui_vs_enabled = _variance(ui_filter_estimate, ag_enabled)

    # The headline status reflects ONLY the primary reconciliation: local
    # canonical vs the fresh campaign-level API total.
    if var_local_vs_campaign["within_tolerance"] is None:
        status = "unavailable"
    elif var_local_vs_campaign["within_tolerance"]:
        status = "match"
    else:
        status = "mismatch"

    aliases = _aliases_for_campaign(resolved_customer_id, campaign_id)
    daily = _build_daily(local_rows, api_rows, verified_ranges)
    causes = _possible_causes(
        primary=var_local_vs_campaign, ag=ag,
        ui_vs_enabled=var_ui_vs_enabled, currency=currency_code)

    return {
        "customer_id": resolved_customer_id,
        "campaign_id": campaign_id,
        "campaign_name": campaign_name,
        "window": window,
        "date_from": date_from,
        "date_to": date_to,
        "account_time_zone": account_time_zone,
        "currency_code": currency_code,
        # ── Separate totals (never conflated) ──────────────────────────────
        "local_canonical_campaign_total": local_total,
        "fresh_campaign_api_total_all_statuses": api_total,
        "fresh_ad_group_total_all_statuses": ag_all,
        "fresh_ad_group_total_enabled_only": ag_enabled,
        "fresh_ad_group_total_paused_removed": ag_paused_removed,
        "google_ui_filter_estimate": ui_filter_estimate,
        # ── Separate variances ─────────────────────────────────────────────
        "variance_local_vs_fresh_campaign_api": var_local_vs_campaign["amount"],
        "variance_local_vs_fresh_campaign_api_pct": var_local_vs_campaign["pct"],
        "variance_local_vs_ad_group_all_statuses": var_local_vs_adgroup_all["amount"],
        "variance_google_ui_filter_estimate_vs_enabled_only": var_ui_vs_enabled["amount"],
        # ── Back-compat summary aliases (primary reconciliation) ───────────
        "local_total_native": local_total,
        "api_total_native": api_total,
        "variance_native": var_local_vs_campaign["amount"],
        "variance_pct": var_local_vs_campaign["pct"],
        # ── Status, coverage, integrity, evidence ──────────────────────────
        "status": status,
        "coverage_status": coverage_status,
        "rows_counted": rows_counted,
        "date_chunks_verified": len(verified_chunks),
        "verified_chunks": verified_chunks,
        "mapped_aliases": aliases,
        "ad_group_breakdown": ag,
        "duplicate_local_rows": duplicate_dates,
        "tolerance": SPEND_VARIANCE_TOLERANCE,
        "possible_causes": causes,
        "daily": daily,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
