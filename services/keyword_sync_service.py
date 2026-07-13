"""
Keyword-fact synchronisation + full-history bootstrap (PR-ADS-146A).

ONE shared path keeps the durable ``keyword_daily_facts`` table current and
complete so operators never need a Render Shell for normal use:

  - ``sync_keyword_daily_facts(date_from, date_to, sync_type)`` — the single
    keyword-fact sync used by the daily/weekly/monthly schedulers, the admin
    refresh action and the bootstrap. Pulls the range from the DIRECT Google Ads
    API and upserts on the immutable natural key (no duplication), tracking a
    ``google_ads_api/keyword_facts`` sync batch + state. Skipped-identity rows or
    a fetched-but-zero-written result mark the batch failed.
  - ``run_keyword_bootstrap()`` — resumable full-history backfill in monthly
    chunks, resuming from durable ``revenue_recovery_jobs`` checkpoints; complete
    only when every requested chunk has succeeded.
  - ``maybe_start_bootstrap_on_deploy()`` — detects empty / partial durable
    coverage on startup and spawns the resumable bootstrap on a daemon thread
    (never blocks startup; never restarts a completed bootstrap).
  - ``keyword_history_status()`` — All-time completeness metadata.

Strictly READ-ONLY relative to Google Ads (pull only) and never touches the
legacy ``keywords`` snapshot table.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

KEYWORD_FACTS_SOURCE = "google_ads_api"
KEYWORD_FACTS_DATASET = "keyword_facts"
BOOTSTRAP_JOB_TYPE = "keyword_bootstrap"
DEFAULT_INCREMENTAL_DAYS = 30   # today + previous 29 account-local dates

# Process-local guard so an auto/deploy bootstrap and a manual one can't both
# spawn worker threads at once (durable job status is the cross-process guard).
_bootstrap_lock = threading.Lock()
_bootstrap_running = False


class KeywordBootstrapError(RuntimeError):
    """Start-date could not be resolved (fail closed — never guess a date)."""


def _account_today(now: datetime | None = None) -> date:
    from services.campaign_evidence_service import _account_today as _at  # noqa: PLC0415
    return _at(now)


# ── Start-date resolution (§1) ───────────────────────────────────────────────
def _configured_customer_id() -> str | None:
    return (os.getenv("GOOGLE_ADS_CUSTOMER_ID") or "").strip() or None


def _earliest_canonical_spend_date() -> date | None:
    """MIN(spend_date) from durable canonical Google Ads campaign spend, scoped to
    the configured account when available. None when the table is empty/unreachable."""
    from db.connection import get_conn  # noqa: PLC0415
    cid = _configured_customer_id()
    try:
        with get_conn() as conn:
            if conn is None:
                return None
            with conn.cursor() as cur:
                if cid:
                    cur.execute("SELECT MIN(spend_date) FROM google_ads_campaign_daily_spend "
                                "WHERE customer_id = %s", (cid,))
                else:
                    cur.execute("SELECT MIN(spend_date) FROM google_ads_campaign_daily_spend")
                row = cur.fetchone()
                return row[0] if row and row[0] else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("earliest canonical spend lookup failed: %s", exc)
        return None


def resolve_history_start_date() -> tuple[date, str]:
    """Resolve the keyword-history start date (§1 precedence):
      1. explicit GOOGLE_ADS_HISTORY_START_DATE;
      2. earliest durable canonical Google Ads campaign spend date;
      3. fail closed (raise) — never guess an arbitrary account-start date.
    Returns ``(start_date, source_label)``."""
    env = (os.getenv("GOOGLE_ADS_HISTORY_START_DATE") or "").strip()
    if env:
        try:
            return date.fromisoformat(env), "configured GOOGLE_ADS_HISTORY_START_DATE"
        except ValueError as exc:
            raise KeywordBootstrapError(
                f"GOOGLE_ADS_HISTORY_START_DATE is not a valid ISO date: {env!r}") from exc
    earliest = _earliest_canonical_spend_date()
    if earliest is not None:
        return earliest, "earliest durable canonical Google Ads campaign spend date"
    raise KeywordBootstrapError(
        "Cannot resolve keyword-history start date: set GOOGLE_ADS_HISTORY_START_DATE, "
        "or backfill canonical Google Ads campaign spend first "
        "(google_ads_campaign_daily_spend is empty).")


# ── Durable coverage ─────────────────────────────────────────────────────────
def _durable_coverage() -> tuple[date | None, date | None, int]:
    """(min_source_date, max_source_date, row_count) of keyword_daily_facts."""
    from db.connection import get_conn  # noqa: PLC0415
    try:
        with get_conn() as conn:
            if conn is None:
                return None, None, 0
            with conn.cursor() as cur:
                cur.execute("SELECT MIN(source_date), MAX(source_date), COUNT(*) "
                            "FROM keyword_daily_facts")
                mn, mx, n = cur.fetchone()
                return mn, mx, int(n or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("durable keyword coverage lookup failed: %s", exc)
        return None, None, 0


# ── Shared incremental sync (§3) ─────────────────────────────────────────────
def sync_keyword_daily_facts(date_from: date, date_to: date, sync_type: str, *,
                             run_id: int | None = None) -> dict:
    """Pull [date_from, date_to] from the direct Google Ads API and upsert durable
    keyword facts. Creates + finishes a keyword_facts sync batch and updates
    sync_state. Read-only vs Google Ads. Returns a stats dict with ``ok``.

    A fetched-but-zero-written result, or any skipped-identity/no-date row, marks
    the batch failed (partial persistence never reports full success)."""
    import db.writers as w  # noqa: PLC0415
    from connectors.google_ads_source import pull_keyword_performance_range  # noqa: PLC0415

    batch_id = w.start_sync_batch(
        source=KEYWORD_FACTS_SOURCE, dataset=KEYWORD_FACTS_DATASET,
        sync_type=sync_type, date_from=date_from, date_to=date_to, run_id=run_id)

    try:
        rows = pull_keyword_performance_range(date_from.isoformat(), date_to.isoformat())
    except Exception as exc:  # noqa: BLE001
        logger.error("keyword pull failed (%s → %s): %s", date_from, date_to, exc)
        if batch_id:
            w.finish_sync_batch(batch_id=batch_id, status="failed", row_count=0,
                                error_message=f"keyword pull failed: {exc}")
        return {"ok": False, "fetched": 0, "prepared": 0, "written": 0,
                "skipped_missing_identity": 0, "skipped_no_date": 0,
                "db_unavailable": False, "error": str(exc),
                "date_from": date_from.isoformat(), "date_to": date_to.isoformat()}

    stats = w.write_keyword_daily_facts(run_id=run_id, keyword_rows=rows,
                                        sync_batch_id=batch_id or None)
    fetched = stats.get("fetched", 0)
    skipped = stats.get("skipped_missing_identity", 0) + stats.get("skipped_no_date", 0)
    fetched_not_written = fetched > 0 and stats.get("written", 0) == 0
    ok = (not stats.get("db_unavailable") and skipped == 0
          and stats.get("written", 0) == stats.get("prepared", 0)
          and not fetched_not_written)

    if batch_id:
        w.finish_sync_batch(
            batch_id=batch_id,
            status="success" if ok else "failed",
            row_count=stats.get("written", 0),
            last_source_date=date_to,   # watermark advances even on a verified-empty range
            error_message=None if ok else f"keyword-fact sync partial/failed: {stats}")

    return {"ok": ok, "batch_id": batch_id or None,
            "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
            "currency_incomplete_rows": sum(1 for r in (rows or []) if not r.get("currency_code")),
            **stats}


def sync_recent_keyword_facts(sync_type: str = "daily", *, days: int = DEFAULT_INCREMENTAL_DAYS,
                              now: datetime | None = None, run_id: int | None = None) -> dict:
    """Rolling recent incremental (today + previous ``days-1`` account-local dates)
    so late Google Ads metric/conversion adjustments update existing durable facts
    without duplication (§3)."""
    end = _account_today(now)
    start = end - timedelta(days=days - 1)
    return sync_keyword_daily_facts(start, end, sync_type, run_id=run_id)


# ── Full-history bootstrap (§1) ──────────────────────────────────────────────
def _month_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Calendar-month bounded chunks covering [start, end] inclusive."""
    chunks: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        nxt = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
        chunk_end = min(nxt - timedelta(days=1), end)
        chunks.append((cur, chunk_end))
        cur = chunk_end + timedelta(days=1)
    return chunks


def _chunk_key(a: date, b: date) -> str:
    return f"{a.isoformat()}/{b.isoformat()}"


def _covers(job: dict, start: date, end: date) -> bool:
    df, dt = job.get("date_from"), job.get("date_to")
    return bool(df and dt and str(df) <= start.isoformat() and str(dt) >= end.isoformat())


def run_keyword_bootstrap(*, now: datetime | None = None, force: bool = False) -> dict:
    """Resumable full-history bootstrap in monthly chunks. Idempotent (natural-key
    upsert) and resumable (durable ``completed_chunks`` checkpoint). Complete only
    when EVERY requested chunk has succeeded. Read-only vs Google Ads."""
    import uuid  # noqa: PLC0415
    import db.writers as w  # noqa: PLC0415

    end = _account_today(now)
    try:
        start, start_source = resolve_history_start_date()
    except KeywordBootstrapError as exc:
        logger.error("keyword bootstrap aborted — %s", exc)
        return {"status": "failed", "error": str(exc), "reason": "start_date_unresolved"}

    chunks = _month_chunks(start, end)
    all_keys = [_chunk_key(a, b) for a, b in chunks]

    existing = w.get_latest_recovery_job(BOOTSTRAP_JOB_TYPE)
    # Don't restart a completed full-history bootstrap on every deploy (§2), and
    # don't stack a second running job.
    if existing and not force:
        if existing.get("status") == "running":
            return {"status": "running", "job_id": existing.get("job_id"),
                    "reason": "already_running"}
        if existing.get("status") == "success" and _covers(existing, start, end):
            return {"status": "success", "job_id": existing.get("job_id"),
                    "reason": "already_complete"}

    # Resume the same-range job's checkpoints, else start a fresh job.
    if existing and _covers(existing, start, end) and existing.get("status") in ("partial", "failed", "queued", "running"):
        job_id = existing["job_id"]
        completed = set(existing.get("completed_chunks") or [])
    else:
        job_id = f"kwbs_{end.isoformat()}_{uuid.uuid4().hex[:8]}"
        completed = set()
        w.create_recovery_job(job_id, dry_run=False, date_from=start, date_to=end,
                              chunk_months=1, job_type=BOOTSTRAP_JOB_TYPE)

    w.update_recovery_job(job_id, status="running", phase="backfill")

    totals = {"fetched": 0, "written": 0, "skipped_identity": 0, "currency_incomplete": 0}
    failed_chunk = None
    errors: list = []
    for a, b in chunks:
        key = _chunk_key(a, b)
        if key in completed:
            continue   # resume — never repeat a succeeded chunk
        w.update_recovery_job(job_id, current_chunk=key)
        res = sync_keyword_daily_facts(a, b, "backfill")
        totals["fetched"] += res.get("fetched", 0)
        totals["written"] += res.get("written", 0)
        totals["skipped_identity"] += res.get("skipped_missing_identity", 0)
        totals["currency_incomplete"] += res.get("currency_incomplete_rows", 0)
        if res.get("ok"):
            completed.add(key)
            w.update_recovery_job(job_id, completed_chunks=sorted(completed))
        else:
            failed_chunk = key
            errors.append({"chunk": key, "error": res.get("error") or "partial persistence"})
            w.update_recovery_job(job_id, errors=errors)
            # Keep going so other chunks still land; the job stays incomplete.

    completed_through = None
    if completed:
        completed_through = max(k.split("/")[1] for k in completed)
    all_ok = set(all_keys).issubset(completed)
    summary = {
        "requested_start": start.isoformat(), "requested_end": end.isoformat(),
        "start_source": start_source, "completed_through": completed_through,
        "total_chunks": len(all_keys), "completed_chunks": len(completed),
        "failed_chunk": failed_chunk,
        "fetched_rows": totals["fetched"], "written_rows": totals["written"],
        "skipped_identity_rows": totals["skipped_identity"],
        "currency_incomplete_rows": totals["currency_incomplete"],
    }
    w.update_recovery_job(job_id, status="success" if all_ok else "partial",
                          current_chunk=None, summary=summary,
                          finished_at=datetime.utcnow().isoformat())
    return {"status": "success" if all_ok else "partial", "job_id": job_id, "summary": summary}


# ── Auto bootstrap on deploy (§2) ────────────────────────────────────────────
def _bootstrap_needed() -> bool:
    """True when the durable table is empty OR coverage starts after the resolved
    history start (i.e. earlier dates are not yet stored)."""
    cov_start, _cov_end, rows = _durable_coverage()
    if rows == 0 or cov_start is None:
        return True
    try:
        expected, _src = resolve_history_start_date()
    except KeywordBootstrapError:
        return False   # can't resolve a start → nothing to bootstrap toward
    return cov_start > expected


def maybe_start_bootstrap_on_deploy(*, now: datetime | None = None) -> str:
    """Called from FastAPI startup. Spawns the resumable bootstrap on a daemon
    thread when coverage is empty/partial and no bootstrap is already complete or
    running. Never blocks startup; never restarts a completed bootstrap. Returns a
    short status string for logging/tests."""
    global _bootstrap_running
    try:
        import db.writers as w  # noqa: PLC0415
        existing = w.get_latest_recovery_job(BOOTSTRAP_JOB_TYPE)
        if existing and existing.get("status") == "running":
            return "already_running"
        if not _bootstrap_needed():
            # Completed / fully covered — do not restart on deploy.
            return "not_needed"
        with _bootstrap_lock:
            if _bootstrap_running:
                return "already_running"
            _bootstrap_running = True

        def _worker():
            global _bootstrap_running
            try:
                run_keyword_bootstrap(now=now)
            except Exception as exc:  # noqa: BLE001
                logger.error("keyword bootstrap worker failed: %s", exc)
            finally:
                with _bootstrap_lock:
                    _bootstrap_running = False

        threading.Thread(target=_worker, name="keyword-bootstrap", daemon=True).start()
        return "started"
    except Exception as exc:  # noqa: BLE001
        logger.error("maybe_start_bootstrap_on_deploy failed: %s", exc)
        return "error"


# ── All-time completeness metadata (§6) ──────────────────────────────────────
def keyword_history_status(now: datetime | None = None) -> dict:
    """Metadata that lets the page state whether All-time is genuinely complete."""
    import db.writers as w  # noqa: PLC0415
    end = _account_today(now)
    try:
        expected, _src = resolve_history_start_date()
        expected_iso = expected.isoformat()
    except KeywordBootstrapError:
        expected = None
        expected_iso = None

    cov_start, cov_end, rows = _durable_coverage()
    job = w.get_latest_recovery_job(BOOTSTRAP_JOB_TYPE)
    bootstrap_status = (job or {}).get("status") or "never_run"

    # Missing ranges: the requested chunks not yet completed by the latest job.
    missing: list = []
    if expected is not None:
        all_keys = [_chunk_key(a, b) for a, b in _month_chunks(expected, end)]
        completed = set((job or {}).get("completed_chunks") or [])
        for key in all_keys:
            if key not in completed:
                a, b = key.split("/")
                missing.append({"start": a, "end": b})

    history_complete = bool(
        expected is not None and cov_start is not None and cov_end is not None
        and cov_start <= expected and (end - cov_end).days <= 1
        and not missing and bootstrap_status == "success")

    return {
        "history_start_expected": expected_iso,
        "durable_coverage_start": cov_start.isoformat() if cov_start else None,
        "durable_coverage_end": cov_end.isoformat() if cov_end else None,
        "durable_row_count": rows,
        "history_complete": history_complete,
        "missing_date_ranges": missing,
        "bootstrap_status": bootstrap_status,
        "bootstrap_summary": (job or {}).get("summary"),
    }
