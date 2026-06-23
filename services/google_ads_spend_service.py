"""
Google Ads Spend Truth service (PR-ADS-118)

Forensic audit + durable repair for Google Ads spend:

  - Reads canonical campaign-daily spend DIRECTLY from the Google Ads API
    (never derived from the geo table).
  - Audits API total, local canonical total, and legacy geo total side by side.
  - Backfills missing historical spend in resumable monthly chunks.
  - Distinguishes a genuinely zero-spend day INSIDE a fetched chunk from a date
    range that was never fetched — a missing chunk is NEVER treated as zero.

States: VERIFIED | PARTIAL | GEO_MISMATCH | UNAVAILABLE.

Doctrine: reads Google Ads read-only; writes ONLY the local canonical tables.
No Google Ads writes; no budget/bid/campaign/conversion changes.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone, timedelta

from analysis.business_windows import resolve_window
from db import revenue_repository as repo

log = logging.getLogger(__name__)


class SpendPersistenceError(Exception):
    """Raised when a fetched spend chunk cannot be durably persisted.

    Used to fail a chunk closed: a successful API read followed by an
    incomplete canonical upsert or a failed coverage-ledger write must NOT be
    recorded as verified/complete, and must not let a sync report success.
    """


def configured_customer_id() -> str | None:
    """Return the configured Google Ads customer ID, independent of any API call.

    Read directly from the environment so a failed/empty API fetch can still be
    recorded against the real account (no google-ads SDK import). Seam: tests
    patch this directly.
    """
    cid = (os.getenv("GOOGLE_ADS_CUSTOMER_ID") or "").strip()
    return cid or None

# Geo↔canonical reconciliation tolerance (fraction). Small by design — a country
# ROAS denominator may only be trusted when geo reconciles to canonical spend.
SPEND_VARIANCE_TOLERANCE = 0.02

# Default backfill floor — earliest required revenue/lead history.
DEFAULT_SPEND_BACKFILL_START = date(2024, 1, 1)
# Small daily lookback for late Google Ads spend adjustments.
DAILY_SPEND_LOOKBACK_DAYS = 7


def fetch_daily_spend(start_date: str, end_date: str) -> dict:
    """Thin seam over the Google Ads connector (late import).

    Keeps the google-ads SDK out of callers' import graphs (and out of tests),
    which patch this seam directly. Read-only.
    """
    from connectors.google_ads_direct import fetch_campaign_daily_spend  # noqa: PLC0415
    return fetch_campaign_daily_spend(start_date, end_date)


def _window_bounds(window: str, now):
    resolved = resolve_window(window, now=now)
    start = date.fromisoformat(resolved["start_date"]) if resolved["start_date"] else None
    end = date.fromisoformat(resolved["end_date"])
    return resolved, start, end


def _enumerate_days(start: date, end: date) -> set:
    days, d = set(), start
    while d <= end:
        days.add(d)
        d += timedelta(days=1)
    return days


def analyze_coverage(start: date | None, end: date, chunks: list) -> dict:
    """Compute spend-coverage completeness over [start, end].

    A day is covered only when it falls inside a VERIFIED chunk. Failed chunks
    and uncovered days both make the window incomplete. For all_time (start
    None), the effective lower bound is the earliest verified chunk.
    """
    verified = [c for c in chunks if c.get("status") == "verified" and c.get("chunk_start")]
    failed = [c for c in chunks if c.get("status") != "verified"]

    if start is None:
        starts = [date.fromisoformat(c["chunk_start"]) for c in verified]
        if not starts:
            return {"complete": False, "missing_days": None, "missing_ranges": [],
                    "failed_chunks": failed, "verified_chunks": verified}
        start = min(starts)

    window_days = _enumerate_days(start, end)
    covered = set()
    for c in verified:
        cs = max(date.fromisoformat(c["chunk_start"]), start)
        ce = min(date.fromisoformat(c["chunk_end"]), end)
        if cs <= ce:
            covered |= _enumerate_days(cs, ce)

    missing = sorted(window_days - covered)
    complete = (not missing) and (not failed)
    # Collapse missing days into contiguous ranges for display.
    ranges = []
    for d in missing:
        if ranges and d == ranges[-1][1] + timedelta(days=1):
            ranges[-1][1] = d
        else:
            ranges.append([d, d])
    missing_ranges = [{"start": r[0].isoformat(), "end": r[1].isoformat()} for r in ranges]
    return {
        "complete": complete,
        "missing_days": len(missing),
        "missing_ranges": missing_ranges,
        "failed_chunks": [{"chunk_start": c.get("chunk_start"), "chunk_end": c.get("chunk_end")} for c in failed],
        "verified_chunks": [{"chunk_start": c.get("chunk_start"), "chunk_end": c.get("chunk_end")} for c in verified],
    }


def _reconcile(canonical_spend: float, other_spend: float, tolerance: float) -> dict:
    amount = round(canonical_spend - other_spend, 6)
    pct = (abs(amount) / canonical_spend) if canonical_spend > 0 else None
    within = (canonical_spend == 0 and other_spend == 0) or (pct is not None and pct <= tolerance)
    return {"variance_amount": amount, "variance_pct": pct, "within_tolerance": within}


def build_google_ads_spend_audit(window: str, now: datetime | None = None,
                                 api_total_micros: int | None = None) -> dict:
    """Audit canonical vs local vs geo spend for a business window.

    ``api_total_micros`` may be injected (tests / a pre-fetched live total). When
    None, a best-effort live API total is attempted and ignored on failure.
    """
    resolved, start, end = _window_bounds(window, now)

    canonical = repo.fetch_canonical_campaign_spend(start, end)
    coverage = repo.fetch_spend_coverage(start, end)
    geo = repo.fetch_geo_spend_total(start, end)

    if not canonical.get("available"):
        return {
            "window": resolved,
            "state": "UNAVAILABLE",
            "reason": "canonical spend table unavailable",
            "customer_id": None, "currency_code": None,
            "canonical_api_total": None, "canonical_local_total": None,
            "legacy_geo_total": geo.get("total_spend") if geo.get("available") else None,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    local_micros = int(canonical.get("total_cost_micros") or 0)
    local_spend = local_micros / 1_000_000
    geo_total = geo.get("total_spend") if geo.get("available") else None

    # Best-effort live API total when not injected.
    if api_total_micros is None:
        try:
            payload = fetch_daily_spend(
                resolved["start_date"] or "2000-01-01", resolved["end_date"])
            api_total_micros = sum(int(r.get("cost_micros") or 0) for r in payload.get("rows", []))
        except Exception as exc:  # noqa: BLE001
            log.warning("[spend_audit] live API total unavailable: %s", exc)
            api_total_micros = None

    api_spend = (api_total_micros / 1_000_000) if api_total_micros is not None else None

    cov = analyze_coverage(start, end, coverage.get("chunks", []))
    geo_recon = _reconcile(local_spend, geo_total, SPEND_VARIANCE_TOLERANCE) if geo_total is not None else None
    api_recon = _reconcile(local_spend, api_spend, SPEND_VARIANCE_TOLERANCE) if api_spend is not None else None

    # Resolve the audit state.
    if not cov["complete"]:
        state = "PARTIAL"
    elif api_recon is not None and not api_recon["within_tolerance"]:
        state = "PARTIAL"  # the live API reports spend the local table is missing
    elif geo_recon is not None and not geo_recon["within_tolerance"]:
        state = "GEO_MISMATCH"
    else:
        state = "VERIFIED"

    geo_status = ("reconciled" if (geo_recon and geo_recon["within_tolerance"])
                  else ("mismatch" if geo_recon else "unavailable"))

    # PR-ADS-119: native-currency truth + USD reporting via daily FX.
    native_currency = canonical.get("currency_code") or "GBP"
    usd_total = canonical.get("total_spend_usd")
    try:
        from services.fx_service import build_fx_coverage  # noqa: PLC0415
        fx = build_fx_coverage(start, end, native_currency)
    except Exception as exc:  # noqa: BLE001
        log.warning("[spend_audit] fx coverage unavailable: %s", exc)
        fx = {"state": "UNAVAILABLE", "complete": False, "missing_dates": []}

    return {
        "window": resolved,
        "state": state,
        "customer_id": canonical.get("customer_id"),
        "currency_code": native_currency,
        # Native GBP spend truth (reconciles to the Google Ads UI within £0.01).
        "native_currency": native_currency,
        "native_total": round(local_spend, 6),
        "canonical_api_total": api_spend,
        "canonical_local_total": round(local_spend, 6),
        # USD reporting spend (daily-FX converted) + FX coverage.
        "reporting_currency": canonical.get("reporting_currency") or "USD",
        "usd_total": usd_total,
        "fx_coverage_state": fx.get("state"),
        "fx_coverage_complete": bool(fx.get("complete")),
        "fx_missing_dates": fx.get("missing_dates", []),
        "legacy_geo_total": geo_total,
        "variance_amount": geo_recon["variance_amount"] if geo_recon else None,
        "variance_pct": geo_recon["variance_pct"] if geo_recon else None,
        "coverage_start": canonical.get("coverage_start"),
        "coverage_end": canonical.get("coverage_end"),
        "verified_chunks": cov["verified_chunks"],
        "missing_chunks": cov["missing_ranges"],
        "failed_chunks": cov["failed_chunks"],
        "canonical_campaign_count": canonical.get("campaign_count"),
        "geo_reconciliation_status": geo_status,
        "tolerance": SPEND_VARIANCE_TOLERANCE,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def is_window_spend_verified(window: str, now: datetime | None = None) -> dict:
    """Cheap readiness check for ROAS consumers: is canonical spend complete?

    Returns {available, complete, partial, local_total_spend, customer_id,
    currency_code} based ONLY on local canonical + coverage (no live API call).
    """
    _resolved, start, end = _window_bounds(window, now)
    canonical = repo.fetch_canonical_campaign_spend(start, end)
    if not canonical.get("available"):
        return {"available": False, "complete": False, "partial": False}
    coverage = repo.fetch_spend_coverage(start, end)
    cov = analyze_coverage(start, end, coverage.get("chunks", []))
    return {
        "available": True,
        "complete": cov["complete"],
        "partial": not cov["complete"],
        "local_total_spend": round(int(canonical.get("total_cost_micros") or 0) / 1_000_000, 6),
        "customer_id": canonical.get("customer_id"),
        "currency_code": canonical.get("currency_code"),
    }


# ── Durable, resumable historical backfill (PR-ADS-114 job pattern) ──────────


def run_google_ads_spend_backfill(
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    dry_run: bool = True,
    chunk_months: int = 1,
    resume: bool = False,
    job_id: str | None = None,
    progress: dict | None = None,
    checkpoint=None,
    load_completed=None,
) -> dict:
    """Backfill canonical Google Ads spend in resumable monthly chunks.

    Reads Google Ads read-only; writes ONLY local canonical tables (when not
    dry_run). A failed chunk is NOT recorded complete (its coverage row is
    `failed`) and is retried on resume. Re-running a completed chunk is
    idempotent (unique upsert). NEVER writes to Google Ads.
    """
    from dateutil.relativedelta import relativedelta  # noqa: PLC0415
    import db.writers as db_writers  # noqa: PLC0415

    end = date.fromisoformat(date_to) if date_to else datetime.now(tz=timezone.utc).date()
    start = date.fromisoformat(date_from) if date_from else DEFAULT_SPEND_BACKFILL_START
    if start > end:
        raise ValueError("date_from must be before or equal to date_to")

    started_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = progress if progress is not None else {}
    state.update({"running": True, "dry_run": dry_run, "phase": "starting",
                  "job_id": job_id, "started_at": started_at})

    if resume and load_completed is not None:
        try:
            completed = set(load_completed() or [])
        except Exception:  # noqa: BLE001
            completed = set()
    else:
        completed = set()

    summary = {"chunks_verified": 0, "chunks_failed": 0, "rows_written": 0,
               "api_total_micros": 0, "coverage_start": None, "coverage_end": None}
    chunks_detail: list = []
    errors: list = []

    def _emit(status):
        if checkpoint is None:
            return
        try:
            checkpoint(job_id, {
                "status": status, "phase": state.get("phase"),
                "current_chunk": state.get("current_chunk"),
                "completed_chunks": list(completed),
                "summary": dict(summary), "chunks": list(chunks_detail), "errors": list(errors),
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("[spend_backfill] checkpoint failed: %s", exc)

    cursor = start
    step = relativedelta(months=max(1, chunk_months))
    while cursor <= end:
        chunk_to = min(cursor + step - relativedelta(days=1), end)
        chunk_key = f"{cursor.isoformat()}:{chunk_to.isoformat()}"
        if resume and chunk_key in completed:
            cursor = chunk_to + relativedelta(days=1)
            continue
        state["current_chunk"] = chunk_key
        state["phase"] = "fetching"
        chunk = {"chunk": chunk_key, "rows": 0, "cost_micros": 0, "status": "verified"}
        try:
            payload = fetch_daily_spend(cursor.isoformat(), chunk_to.isoformat())
            rows = payload.get("rows", [])
            micros = sum(int(r.get("cost_micros") or 0) for r in rows)
            chunk["rows"] = len(rows)
            chunk["cost_micros"] = micros
            # Persist with a real customer ID even when the payload omits it.
            customer_id = payload.get("customer_id") or configured_customer_id()
            if not dry_run:
                # Fail closed: a successful read must be durably persisted before
                # the chunk can be marked verified/complete.
                written = db_writers.upsert_campaign_daily_spend(rows, sync_run_id=job_id)
                if rows and written != len(rows):
                    raise SpendPersistenceError(
                        f"spend upsert wrote {written}/{len(rows)} rows")
                ok = db_writers.upsert_spend_coverage(
                    customer_id, cursor.isoformat(), chunk_to.isoformat(), "verified",
                    rows_written=written, cost_micros_total=micros,
                    source_query_version=payload.get("source_query_version"), sync_run_id=job_id)
                if not ok:
                    raise SpendPersistenceError("coverage-ledger upsert failed")
                summary["rows_written"] += written
            summary["api_total_micros"] += micros
            summary["chunks_verified"] += 1
            completed.add(chunk_key)
        except Exception as exc:  # noqa: BLE001
            chunk["status"] = "failed"
            summary["chunks_failed"] += 1
            errors.append(f"{chunk_key}: {exc}")
            if not dry_run:
                # Record a `failed` chunk against the real configured customer ID
                # (the API result may be unavailable). Never overwrites a prior
                # verified chunk (writer guard). Stays incomplete → retried.
                try:
                    db_writers.upsert_spend_coverage(
                        configured_customer_id(), cursor.isoformat(), chunk_to.isoformat(),
                        "failed", sync_run_id=job_id)
                except Exception:  # noqa: BLE001
                    pass
            # Failed chunk is NOT added to completed → retried on resume.

        chunks_detail.append(chunk)
        if summary["coverage_start"] is None:
            summary["coverage_start"] = cursor.isoformat()
        summary["coverage_end"] = chunk_to.isoformat()
        state["completed_chunks"] = list(completed)
        _emit("running")
        cursor = chunk_to + relativedelta(days=1)

    finished_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    status = "failed" if (errors and not chunks_detail) else ("partial" if errors else "success")
    summary["api_total_spend"] = round(summary["api_total_micros"] / 1_000_000, 6)

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
            "completed_chunks": list(completed), "summary": dict(summary),
            "chunks": list(chunks_detail), "errors": list(errors), "finished_at": finished_at,
        })
    return result
