"""
FX service (PR-ADS-119) — daily currency normalization.

Google Ads native spend is GBP; HubSpot closed-won revenue is USD. To compute a
mathematically honest ROAS, each daily spend row is converted to USD using the FX
rate for its OWN spend_date — never a single current spot rate for a whole window.

Doctrine: reads published reference FX rates read-only; writes ONLY the local
fx_rates table. No external writes.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone, timedelta

from analysis.business_windows import resolve_window
from db import revenue_repository as repo

log = logging.getLogger(__name__)

# Google Ads native account currency → USD reporting currency.
NATIVE_CURRENCY = "GBP"
REPORTING_CURRENCY = "USD"


def fetch_fx_rate(rate_date: str, base_currency: str, quote_currency: str) -> dict:
    """Thin seam over the FX connector (late import).

    Keeps the connector out of callers' import graphs and lets tests patch this
    directly. Read-only.
    """
    from connectors.fx_rates import fetch_fx_rate as _fetch  # noqa: PLC0415
    return _fetch(rate_date, base_currency, quote_currency)


def _window_bounds_safe(window: str):
    """Resolve a business window to (start_date|None, end_date) date objects."""
    resolved = resolve_window(window)
    start = date.fromisoformat(resolved["start_date"]) if resolved["start_date"] else None
    end = date.fromisoformat(resolved["end_date"])
    return start, end


def _enumerate_days(start: date, end: date) -> list:
    days, d = [], start
    while d <= end:
        days.append(d)
        d += timedelta(days=1)
    return days


def analyze_fx_coverage(spend_dates, fx_dates) -> dict:
    """Which spend dates lack an FX rate. A missing date is NEVER converted.

    Returns {complete, missing_dates, covered_days, spend_days}. With no spend
    dates there is nothing to convert → complete.
    """
    have = {_as_iso(d) for d in (fx_dates or [])}
    need = [_as_iso(d) for d in (spend_dates or [])]
    missing = sorted({d for d in need if d not in have})
    return {
        "complete": not missing,
        "missing_dates": missing,
        "covered_days": len(set(need)) - len(missing),
        "spend_days": len(set(need)),
    }


def convert_daily_spend_to_usd(daily_rows: list, fx_by_date: dict) -> dict:
    """Convert per-day native spend to USD using each row's OWN spend_date rate.

    daily_rows: [{spend_date, spend_native_amount, spend_native_currency}].
    fx_by_date: {iso_date: rate (native->USD)}. Returns {complete, rows, total_usd,
    total_native, missing_dates}. Rows whose date has no rate get spend_usd=None
    and fx_rate_to_usd=None (never converted at a wrong rate).
    """
    out_rows = []
    total_usd = 0.0
    total_native = 0.0
    missing = []
    for r in daily_rows:
        iso = _as_iso(r.get("spend_date"))
        native = float(r.get("spend_native_amount") or 0.0)
        total_native += native
        rate = fx_by_date.get(iso)
        if rate is None:
            missing.append(iso)
            usd = None
        else:
            usd = round(native * float(rate), 6)
            total_usd += usd
        out_rows.append({
            "spend_date": iso,
            "spend_native_amount": native,
            "spend_native_currency": r.get("spend_native_currency") or NATIVE_CURRENCY,
            "fx_rate_to_usd": float(rate) if rate is not None else None,
            "spend_usd": usd,
        })
    complete = not missing
    return {
        "complete": complete,
        "rows": out_rows,
        "total_usd": round(total_usd, 6) if complete else None,
        "total_native": round(total_native, 6),
        "missing_dates": sorted(set(missing)),
    }


def build_fx_coverage(start: date | None, end: date,
                      base_currency: str = NATIVE_CURRENCY) -> dict:
    """FX coverage status for the canonical spend dates in a window (audit/gating)."""
    cov = repo.fetch_fx_coverage(start, end, base_currency, REPORTING_CURRENCY)
    if not cov.get("available"):
        return {"state": "UNAVAILABLE", "complete": False, "missing_dates": []}
    return {
        "state": "VERIFIED" if cov.get("complete") else "INCOMPLETE",
        "complete": bool(cov.get("complete")),
        "spend_days": cov.get("spend_days", 0),
        "covered_days": cov.get("covered_days", 0),
        "missing_dates": [_as_iso(d) for d in cov.get("missing_dates", [])],
        "base_currency": base_currency,
        "quote_currency": REPORTING_CURRENCY,
    }


def ensure_fx_rates(start: date, end: date, *,
                    base_currency: str = NATIVE_CURRENCY,
                    quote_currency: str = REPORTING_CURRENCY,
                    only_missing: bool = True) -> dict:
    """Fetch + upsert daily FX rates for [start, end]. Idempotent. Read GA-published
    rates read-only; write ONLY the local fx_rates table.

    When ``only_missing`` is True, dates already present locally are skipped (no
    redundant network calls). Returns {rows_written, fetched, failed, missing_dates}.
    """
    import db.writers as db_writers  # noqa: PLC0415

    existing = set()
    if only_missing:
        have = repo.fetch_fx_rates(start, end, base_currency, quote_currency)
        existing = {_as_iso(d) for d in (have.get("rates") or {}).keys()}

    written = 0
    fetched = 0
    failed = []
    for d in _enumerate_days(start, end):
        iso = d.isoformat()
        if only_missing and iso in existing:
            continue
        try:
            payload = fetch_fx_rate(iso, base_currency, quote_currency)
            fetched += 1
            n = db_writers.upsert_fx_rates([payload])
            written += n
        except Exception as exc:  # noqa: BLE001
            failed.append({"rate_date": iso, "error": str(exc)[:200]})
            log.warning("[fx] rate fetch failed for %s: %s", iso, exc)

    return {
        "rows_written": written,
        "fetched": fetched,
        "failed": failed,
        "base_currency": base_currency,
        "quote_currency": quote_currency,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def _as_iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    return str(value)
