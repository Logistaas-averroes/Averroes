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

from db import revenue_repository as repo
from services.google_ads_spend_service import (
    SPEND_VARIANCE_TOLERANCE,
    _window_bounds,
    configured_customer_id,
)

log = logging.getLogger(__name__)

# Earliest geo history to sync by default (matches the spend backfill floor).
DEFAULT_GEO_SYNC_START = date(2024, 1, 1)


def fetch_geo_daily(start_date: str, end_date: str) -> dict:
    """Thin seam over the canonical Google Ads geo connector (late import).

    Read-only. Patched directly by tests so the google-ads SDK is never imported.
    """
    from connectors.google_ads_direct import fetch_geo_daily_spend as _f  # noqa: PLC0415
    return _f(start_date, end_date)


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
        "geo_rows_counted": int(geo.get("rows_counted") or 0) if geo_available else 0,
        "geo_country_count": int(geo.get("country_count") or 0) if geo_available else 0,
        "last_geo_sync_at": geo.get("last_synced_at") if geo_available else None,
        "tolerance": SPEND_VARIANCE_TOLERANCE,
        "generated_at": datetime.now(timezone.utc).isoformat(),
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
) -> dict:
    """Sync canonical Google Ads geo (country) daily spend in monthly chunks.

    Reads Google Ads read-only (geographic_view) and writes ONLY the local
    canonical google_ads_geo_daily_spend table (when not dry_run). NEVER writes
    to Google Ads or HubSpot. Re-running is idempotent (unique upsert).

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

    started_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = progress if progress is not None else {}
    state.update({"running": True, "dry_run": dry_run, "phase": "starting", "job_id": job_id,
                  "started_at": started_at})

    summary = {"chunks_verified": 0, "chunks_failed": 0, "rows_written": 0,
               "geo_cost_micros": 0, "country_criteria": 0,
               "coverage_start": None, "coverage_end": None}
    chunks_detail: list = []
    errors: list = []
    seen_countries: set = set()

    def _emit(status):
        if checkpoint is None:
            return
        try:
            checkpoint(job_id, {
                "status": status, "phase": state.get("phase"),
                "current_chunk": state.get("current_chunk"),
                "summary": dict(summary), "chunks": list(chunks_detail), "errors": list(errors),
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("[geo_sync] checkpoint failed: %s", exc)

    cursor = start
    step = relativedelta(months=max(1, chunk_months))
    while cursor <= end:
        chunk_to = min(cursor + step - relativedelta(days=1), end)
        chunk_key = f"{cursor.isoformat()}:{chunk_to.isoformat()}"
        state["current_chunk"] = chunk_key
        state["phase"] = "fetching"
        chunk = {"chunk": chunk_key, "rows": 0, "cost_micros": 0, "status": "verified"}
        try:
            payload = fetch_geo_daily(cursor.isoformat(), chunk_to.isoformat())
            rows = payload.get("rows", [])
            micros = sum(int(r.get("cost_micros") or 0) for r in rows)
            for r in rows:
                seen_countries.add(r.get("country_criterion_id"))
            chunk["rows"] = len(rows)
            chunk["cost_micros"] = micros
            if not dry_run:
                written = db_writers.upsert_geo_daily_spend(rows, sync_run_id=job_id)
                summary["rows_written"] += written
            summary["geo_cost_micros"] += micros
            summary["chunks_verified"] += 1
        except Exception as exc:  # noqa: BLE001
            chunk["status"] = "failed"
            summary["chunks_failed"] += 1
            errors.append(f"{chunk_key}: {exc}")

        chunks_detail.append(chunk)
        if summary["coverage_start"] is None:
            summary["coverage_start"] = cursor.isoformat()
        summary["coverage_end"] = chunk_to.isoformat()
        _emit("running")
        cursor = chunk_to + relativedelta(days=1)

    summary["country_criteria"] = len([c for c in seen_countries if c])
    summary["geo_total_spend"] = round(summary["geo_cost_micros"] / 1_000_000, 6)
    finished_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    status = "failed" if (errors and not summary["chunks_verified"]) else (
        "partial" if errors else "success")

    result = {
        "status": status, "dry_run": dry_run,
        "date_from": start.isoformat(), "date_to": end.isoformat(),
        "chunk_months": chunk_months, "started_at": started_at, "finished_at": finished_at,
        "summary": summary, "chunks": chunks_detail, "errors": errors,
    }
    state.update({"running": False, "phase": "done", "finished_at": finished_at, "latest": result})
    if checkpoint is not None:
        state["phase"] = "done"
        checkpoint(job_id, {
            "status": status, "phase": "done", "current_chunk": None,
            "summary": dict(summary), "chunks": list(chunks_detail),
            "errors": list(errors), "finished_at": finished_at,
        })
    return result
