"""
Campaign-level Google Ads spend reconciliation drilldown (PR-ADS-122).

An AUDIT + PROOF layer: for a single ROAS campaign row, prove its spend by
comparing, for the SAME campaign and date window:

  1. the LOCAL canonical DB total (google_ads_campaign_daily_spend), and
  2. a FRESH Google Ads campaign-level API total queried right now, and
  3. an optional ad-group-level breakdown WITH status (enabled / paused /
     removed) so a lower Google Ads UI total can be explained by the UI's
     "Ad group status: Enabled" filter excluding paused/removed ad-group spend.

It returns a daily breakdown (local cost vs API cost vs variance vs coverage
state) plus the variance, coverage status, rows counted, and verified date
chunks so the UI can never hide a mismatch.

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


def _variance(local_native: float, api_native: float | None) -> tuple:
    """Return (variance_native, variance_pct) for local minus API. Never hides it."""
    if api_native is None:
        return None, None
    amount = round(local_native - api_native, 6)
    pct = round((abs(amount) / api_native) * 100, 4) if api_native else (0.0 if amount == 0 else None)
    return amount, pct


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
        "enabled_total_native": _micros_to_native(enabled_micros),
        "non_enabled_total_native": _micros_to_native(non_enabled_micros),
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


def _possible_causes(*, within_tolerance: bool, ad_groups: dict, currency: str) -> list:
    """Explain a variance honestly — the UI never hides a mismatch."""
    causes = []
    if within_tolerance:
        causes.append(
            "Local canonical spend matches the fresh Google Ads campaign-level API "
            "total for this campaign and window."
        )
    else:
        causes.append(
            "Local canonical spend differs from the fresh Google Ads campaign-level "
            "API total — inspect the daily breakdown for the date(s) that diverge."
        )
    causes.append(
        "Local canonical spend includes ALL campaign spend returned by the "
        "campaign-level API (every ad-group status)."
    )
    if ad_groups.get("available"):
        non_enabled = ad_groups.get("non_enabled_total_native") or 0
        if non_enabled > 0:
            causes.append(
                f"Google Ads UI filters (Ad group status: Enabled) exclude "
                f"{non_enabled:.2f} {currency} of paused/removed ad-group spend that "
                f"the campaign-level total includes."
            )
        else:
            causes.append(
                "No paused/removed ad-group spend found, so a Google Ads UI "
                "'Ad group status: Enabled' filter does not explain a lower UI total."
            )
    else:
        causes.append(
            "Ad-group-level spend was not available, so a paused/removed ad-group "
            "filter could not be confirmed or ruled out."
        )
    return causes


def build_campaign_spend_reconciliation(
    window: str,
    campaign_id: str,
    *,
    now: datetime | None = None,
    include_ad_groups: bool = True,
    api_payload: dict | None = None,
) -> dict:
    """Reconcile one campaign's local canonical spend against a fresh API total.

    ``api_payload`` may be injected (tests / a pre-fetched live result). When
    None, a best-effort live campaign-level API total is attempted; on failure
    the API side is reported unavailable (never silently treated as zero).

    Read-only. NEVER writes to Google Ads and NEVER changes the ROAS spend source.
    """
    if not campaign_id:
        raise ValueError("campaign_id is required")
    campaign_id = str(campaign_id).strip()

    account_time_zone = repo.fetch_account_time_zone()
    resolved, start, end = _window_bounds(window, now, account_time_zone)
    date_from = resolved.get("start_date")
    date_to = resolved.get("end_date")

    local = repo.fetch_campaign_daily_spend_local(campaign_id, start, end)
    coverage = repo.fetch_spend_coverage(start, end)
    cov = analyze_coverage(start, end, coverage.get("chunks", []))
    verified_chunks = cov.get("verified_chunks", [])
    verified_ranges = [
        (date.fromisoformat(c["chunk_start"]), date.fromisoformat(c["chunk_end"]))
        for c in verified_chunks
    ]
    coverage_status = "complete" if cov.get("complete") else "incomplete"

    duplicates = repo.fetch_campaign_spend_duplicate_dates(campaign_id, start, end)
    duplicate_dates = duplicates.get("duplicates", [])

    if not local.get("available"):
        return {
            "customer_id": None,
            "campaign_id": campaign_id,
            "campaign_name": None,
            "window": window,
            "date_from": date_from,
            "date_to": date_to,
            "account_time_zone": account_time_zone,
            "currency_code": None,
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

    customer_id = local.get("customer_id")
    currency_code = local.get("currency_code") or "GBP"
    campaign_name = local.get("campaign_name")
    local_total = round(float(local.get("total_spend") or 0.0), 6)
    rows_counted = int(local.get("rows_counted") or 0)
    local_rows = local.get("rows", [])

    # Fresh campaign-level API total for the SAME campaign/date (never the table).
    if api_payload is None:
        try:
            api_payload = fetch_campaign_api_spend(
                date_from or "2000-01-01", date_to, campaign_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("[reconcile] live campaign API total unavailable: %s", exc)
            api_payload = None

    if api_payload is not None:
        api_rows = api_payload.get("rows", [])
        api_total_micros = sum(int(r.get("cost_micros") or 0) for r in api_rows)
        api_total = _micros_to_native(api_total_micros)
        if not campaign_name:
            campaign_name = api_payload.get("campaign_name")
        if not account_time_zone:
            account_time_zone = api_payload.get("account_time_zone")
    else:
        api_rows = None
        api_total = None

    variance_native, variance_pct = _variance(local_total, api_total)
    if api_total is None:
        within_tolerance = False
        status = "unavailable"
    else:
        within_tolerance = (
            (api_total == 0 and local_total == 0)
            or (api_total > 0 and abs(variance_native) / api_total <= SPEND_VARIANCE_TOLERANCE)
        )
        status = "match" if within_tolerance else "mismatch"

    ad_groups = (
        _ad_group_breakdown(date_from or "2000-01-01", date_to, campaign_id)
        if include_ad_groups else {"available": False, "statuses": []}
    )

    aliases = _aliases_for_campaign(customer_id, campaign_id)
    daily = _build_daily(local_rows, api_rows, verified_ranges)
    causes = _possible_causes(
        within_tolerance=within_tolerance, ad_groups=ad_groups, currency=currency_code)

    return {
        "customer_id": customer_id,
        "campaign_id": campaign_id,
        "campaign_name": campaign_name,
        "window": window,
        "date_from": date_from,
        "date_to": date_to,
        "account_time_zone": account_time_zone,
        "currency_code": currency_code,
        "local_total_native": local_total,
        "api_total_native": api_total,
        "variance_native": variance_native,
        "variance_pct": variance_pct,
        "status": status,
        "coverage_status": coverage_status,
        "rows_counted": rows_counted,
        "date_chunks_verified": len(verified_chunks),
        "verified_chunks": verified_chunks,
        "mapped_aliases": aliases,
        "ad_group_breakdown": ad_groups,
        "duplicate_local_rows": duplicate_dates,
        "tolerance": SPEND_VARIANCE_TOLERANCE,
        "possible_causes": causes,
        "daily": daily,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
