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
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# PR-ADS-156: imported from the ONE registry rather than spelled again here.
# Two spellings of a (source, dataset) pair is precisely the drift that made
# canonical campaign spend report "never run" for weeks while its table filled
# up normally — the writer stamped one key and the freshness config read another.
from services.dataset_keys import (  # noqa: E402
    KEYWORD_FACTS_DATASET, KEYWORD_FACTS_SOURCE,
)
BOOTSTRAP_JOB_TYPE = "keyword_bootstrap"
DEFAULT_INCREMENTAL_DAYS = 30   # today + previous 29 account-local dates

# Durable lease TTL — a worker must heartbeat within this window or its lease is
# considered stale and another worker/deploy may recover the job (§1/§2).
LEASE_TTL_SECONDS = int(os.getenv("KEYWORD_BOOTSTRAP_LEASE_TTL_SECONDS", "900"))  # 15 min

# Fast in-process guard so one process doesn't spawn two worker threads. The DB
# lease (below) is the authoritative CROSS-process guard.
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
    if not batch_id:
        # §7 — no durable batch means we can't track the pull; fail closed with a
        # clear reason (and never hit Google Ads without a tracking record).
        msg = "sync-batch creation failed — tracking unavailable (database down?)"
        logger.error("keyword sync (%s → %s): %s", date_from, date_to, msg)
        return {"ok": False, "batch_id": None, "fetched": 0, "prepared": 0,
                "written": 0, "skipped_missing_identity": 0, "skipped_no_date": 0,
                "db_unavailable": True, "error": msg,
                "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
                "currency_incomplete_rows": 0, "verified_empty": False,
                "external_writes_performed": False,
                "source": KEYWORD_FACTS_SOURCE, "dataset": KEYWORD_FACTS_DATASET}

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
                "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
                # A FAILED pull also returns no rows. It is never verified empty.
                "currency_incomplete_rows": 0, "verified_empty": False,
                "external_writes_performed": False,
                "source": KEYWORD_FACTS_SOURCE, "dataset": KEYWORD_FACTS_DATASET}

    stats = w.write_keyword_daily_facts(run_id=run_id, keyword_rows=rows,
                                        sync_batch_id=batch_id or None)
    fetched = stats.get("fetched", 0)
    skipped = stats.get("skipped_missing_identity", 0) + stats.get("skipped_no_date", 0)
    fetched_not_written = fetched > 0 and stats.get("written", 0) == 0
    ok = (not stats.get("db_unavailable") and skipped == 0
          and stats.get("written", 0) == stats.get("prepared", 0)
          and not fetched_not_written)

    # Always surface a human-readable reason on failure so the admin refresh UI
    # never falls back to "Unknown error" for a partial-persistence outcome.
    error = None
    if not ok:
        if stats.get("db_unavailable"):
            error = "database unavailable — no keyword facts were written"
        elif fetched_not_written:
            error = f"pulled {fetched} keyword row(s) but wrote 0 — persistence failed"
        elif skipped:
            error = (f"{skipped} row(s) rejected (missing_identity="
                     f"{stats.get('skipped_missing_identity', 0)}, "
                     f"no_date={stats.get('skipped_no_date', 0)})")
        elif stats.get("written", 0) != stats.get("prepared", 0):
            error = (f"wrote {stats.get('written', 0)} of "
                     f"{stats.get('prepared', 0)} prepared row(s)")
        else:
            error = "keyword-fact sync did not complete cleanly"

    w.finish_sync_batch(
        batch_id=batch_id,
        status="success" if ok else "failed",
        row_count=stats.get("written", 0),
        # Watermark advances to date_to on success (incl. a verified-empty range);
        # a failed sync must NOT advance the proven-coverage watermark (§6).
        last_source_date=date_to if ok else None,
        error_message=None if ok else f"{error}: {stats}")

    return {"ok": ok, "batch_id": batch_id or None,
            "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
            "currency_incomplete_rows": sum(1 for r in (rows or []) if not r.get("currency_code")),
            "error": error,
            **stats,
            # PR-ADS-156 §3: stated, not inferred from `written == 0`. A pull
            # that failed also wrote nothing, and the two must never read the
            # same. This is true only when the query SUCCEEDED and the account
            # had no eligible keyword rows for the interval — a measurement.
            "verified_empty": bool(ok and fetched == 0),
            # Read-only against Google Ads; the only writes are local.
            "external_writes_performed": False,
            "source": KEYWORD_FACTS_SOURCE,
            "dataset": KEYWORD_FACTS_DATASET}


def sync_recent_keyword_facts(sync_type: str = "daily", *, days: int = DEFAULT_INCREMENTAL_DAYS,
                              now: datetime | None = None, run_id: int | None = None) -> dict:
    """Rolling recent incremental (today + previous ``days-1`` account-local dates)
    so late Google Ads metric/conversion adjustments update existing durable facts
    without duplication (§3)."""
    end = _account_today(now)
    start = end - timedelta(days=days - 1)
    return sync_keyword_daily_facts(start, end, sync_type, run_id=run_id)


# ── Stable calendar-month chunk identity (§4) ────────────────────────────────
# Historical months carry a STABLE key ``YYYY-MM`` — never a changing exact end
# date — so a completed chunk stays completed on the next calendar day. The
# current (open) month is excluded from historical completeness; the daily
# rolling incremental sync proves it instead.
def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    first = date(year, month, 1)
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return first, nxt - timedelta(days=1)


def _iter_months(start: date, end: date):
    """Yield (key, chunk_start, chunk_end, is_current_month) for each calendar
    month spanning [start, end], capped to the requested bounds."""
    current_key = _month_key(end)
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        mstart, mend = _month_bounds(y, m)
        key = f"{y:04d}-{m:02d}"
        yield key, max(mstart, start), min(mend, end), (key == current_key)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _bootstrap_chunks(start: date, end: date) -> list[tuple[str, date, date]]:
    """All month chunks to SYNC (incl. the current partial month)."""
    return [(key, cs, ce) for key, cs, ce, _cur in _iter_months(start, end)]


def _required_month_keys(start: date, end: date) -> list[str]:
    """Closed-month keys whose completion defines HISTORICAL completeness. The
    current open month is excluded — the daily incremental proves it (§4)."""
    return [key for key, _cs, _ce, is_cur in _iter_months(start, end) if not is_cur]


def _lease_active(job: dict | None, now: datetime | None = None) -> bool:
    """True when the job holds a non-expired durable lease (a live worker owns it)."""
    if not job:
        return False
    exp = job.get("lease_expires_at")
    if exp is None:
        return False
    if isinstance(exp, str):
        try:
            exp = datetime.fromisoformat(exp)
        except ValueError:
            return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    return exp > now_dt


# ── Full-history bootstrap (§1/§2/§5) ────────────────────────────────────────
def run_keyword_bootstrap(*, now: datetime | None = None, force: bool = False) -> dict:
    """Resumable full-history bootstrap in STABLE monthly chunks, guarded by a
    DB-backed atomic lease so only one worker runs at a time and a crashed
    worker's stale lease is recovered. Idempotent (natural-key upsert) and
    resumable (durable ``completed_chunks`` ledger). Read-only vs Google Ads.

    Fails closed (never pulls Google Ads) when the durable checkpoint cannot be
    created or renewed — a supposedly resumable bootstrap must never run without
    a durable record of progress (§5)."""
    import uuid  # noqa: PLC0415
    import db.writers as w  # noqa: PLC0415

    end = _account_today(now)
    try:
        start, start_source = resolve_history_start_date()
    except KeywordBootstrapError as exc:
        logger.error("keyword bootstrap aborted — %s", exc)
        return {"status": "failed", "error": str(exc), "reason": "start_date_unresolved"}

    chunks = _bootstrap_chunks(start, end)
    all_keys = [k for k, _cs, _ce in chunks]
    required = set(_required_month_keys(start, end))

    # Fast pre-check: an already-complete bootstrap that still covers every
    # required (closed) month — do not claim a lease just to no-op (§2).
    existing = w.get_latest_recovery_job(BOOTSTRAP_JOB_TYPE)
    if existing and not force and existing.get("status") == "success":
        done = set(existing.get("completed_chunks") or [])
        if required.issubset(done):
            return {"status": "success", "job_id": existing.get("job_id"),
                    "reason": "already_complete"}

    # Atomically claim (or take over a stale) durable job. Seed a fresh job with
    # any still-relevant completed months so we never re-sync them.
    lease_token = uuid.uuid4().hex
    seed = sorted(set((existing or {}).get("completed_chunks") or []) & set(all_keys))
    claim = w.acquire_recovery_lease(
        BOOTSTRAP_JOB_TYPE, lease_token, LEASE_TTL_SECONDS,
        date_from=start, date_to=end, chunk_months=1, seed_completed_chunks=seed)

    if not claim.get("claimed"):
        reason = claim.get("reason")
        if reason == "db_unavailable":
            return {"status": "failed", "reason": "durable_job_unavailable",
                    "error": "could not create/claim a durable bootstrap job "
                             "(database unavailable) — refusing to run without a checkpoint"}
        if reason == "active_lease":
            return {"status": "running", "reason": "already_running",
                    "job_id": (claim.get("job") or {}).get("job_id")}
        return {"status": "skipped", "reason": reason,
                "job_id": (claim.get("job") or {}).get("job_id")}

    job = claim["job"]
    job_id = job["job_id"]
    completed = set(job.get("completed_chunks") or [])
    errors: list = list(job.get("errors") or [])

    totals = {"fetched": 0, "written": 0, "skipped_identity": 0, "currency_incomplete": 0}
    failed_chunk = None
    for key, cs, ce in chunks:
        if key in completed:
            continue   # resume — never repeat a succeeded chunk (stable key)
        # Heartbeat + claim the current chunk. A lost lease means another worker
        # took over (or the DB is gone); stop rather than run without a checkpoint.
        if not w.renew_recovery_lease(job_id, lease_token, LEASE_TTL_SECONDS, current_chunk=key):
            logger.warning("keyword bootstrap %s lost its lease before chunk %s", job_id, key)
            return {"status": "interrupted", "reason": "lease_lost", "job_id": job_id,
                    "error": "bootstrap lease lost mid-run — another worker or a DB "
                             "outage took over; stopping to stay resumable"}
        res = sync_keyword_daily_facts(cs, ce, "backfill")
        totals["fetched"] += res.get("fetched", 0)
        totals["written"] += res.get("written", 0)
        totals["skipped_identity"] += res.get("skipped_missing_identity", 0)
        totals["currency_incomplete"] += res.get("currency_incomplete_rows", 0)
        if res.get("ok"):
            completed.add(key)
            if not w.renew_recovery_lease(job_id, lease_token, LEASE_TTL_SECONDS,
                                          completed_chunks=sorted(completed)):
                return {"status": "interrupted", "reason": "checkpoint_failed", "job_id": job_id,
                        "error": "could not persist bootstrap checkpoint — aborting to "
                                 "stay resumable rather than lose progress"}
        else:
            failed_chunk = key
            errors.append({"chunk": key, "error": res.get("error") or "partial persistence"})
            w.renew_recovery_lease(job_id, lease_token, LEASE_TTL_SECONDS, errors=errors)
            # Keep going so other chunks still land; the job stays incomplete.

    completed_through = max(completed) if completed else None
    all_ok = set(all_keys).issubset(completed)
    history_ready = required.issubset(completed)
    summary = {
        "requested_start": start.isoformat(), "requested_end": end.isoformat(),
        "start_source": start_source, "completed_through": completed_through,
        "total_chunks": len(all_keys), "completed_chunks": len(completed),
        "required_chunks": len(required), "history_ready": history_ready,
        "failed_chunk": failed_chunk,
        "fetched_rows": totals["fetched"], "written_rows": totals["written"],
        "skipped_identity_rows": totals["skipped_identity"],
        "currency_incomplete_rows": totals["currency_incomplete"],
    }
    final_status = "success" if all_ok else "partial"
    released = w.release_recovery_lease(
        job_id, lease_token, status=final_status, summary=summary,
        completed_chunks=sorted(completed),
        # Timezone-aware ISO (…+00:00) so the UI's new Date() never reads a naive
        # timestamp as browser-local time.
        finished_at=datetime.now(timezone.utc).isoformat())
    if not released:
        # §B2 — the final checkpoint did not persist (lease lost or DB down). Do
        # NOT report success: the job row keeps its running/lease state and stays
        # recoverable via stale-lease takeover once the lease expires.
        logger.error("keyword bootstrap %s could not persist its final checkpoint", job_id)
        return {"status": "interrupted", "reason": "final_checkpoint_failed",
                "job_id": job_id, "summary": summary, "history_ready": history_ready,
                "error": "could not persist the final bootstrap checkpoint (lease lost "
                         "or database unavailable); the job remains recoverable via "
                         "stale-lease takeover"}
    return {"status": final_status, "job_id": job_id, "summary": summary,
            "history_ready": history_ready}


# ── Auto bootstrap on deploy (§2/§3) ─────────────────────────────────────────
def _bootstrap_needed(now: datetime | None = None) -> bool:
    """Gap-aware need check (§3). Bootstrap is needed when the durable table is
    empty, a required (closed) month is missing from the ledger, the latest job
    is a stale-running one that must be recovered, or history cannot be proven.
    A live worker already holding the lease means it is NOT needed. A successful
    first month never hides a failed later month — the ledger is authoritative."""
    import db.writers as w  # noqa: PLC0415
    _cov_start, _cov_end, rows = _durable_coverage()
    try:
        expected, _src = resolve_history_start_date()
    except KeywordBootstrapError:
        return False   # can't resolve a start → nothing to bootstrap toward

    job = w.get_latest_recovery_job(BOOTSTRAP_JOB_TYPE)
    if job and job.get("status") == "running":
        # A live lease means a worker owns it; a stale lease means recover it.
        return not _lease_active(job, now)
    if rows == 0:
        return True
    required = set(_required_month_keys(expected, _account_today(now)))
    completed = set((job or {}).get("completed_chunks") or [])
    return not required.issubset(completed)


def maybe_start_bootstrap_on_deploy(*, now: datetime | None = None) -> str:
    """Called from FastAPI startup. Spawns the resumable bootstrap on a daemon
    thread when it is needed (empty / gap / stale-running). Never blocks startup;
    the DB lease — not this check — is the authoritative cross-process guard, so a
    completed or actively-running bootstrap is never restarted. Returns a short
    status string for logging/tests."""
    global _bootstrap_running
    try:
        if not _bootstrap_needed(now):
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


def _latest_successful_sync_date(now: datetime | None = None) -> date | None:
    """Furthest date the durable keyword facts have been PROVEN synced through —
    MAX(date_to) over successful keyword_facts sync batches, independent of
    whether recent dates hold any rows (zero-activity days). None when never
    synced. This is the authority for coverage proof (§6), not MAX(source_date)."""
    import db.writers as w  # noqa: PLC0415
    return w.latest_successful_batch_source_date(KEYWORD_FACTS_SOURCE, KEYWORD_FACTS_DATASET)


# ── All-time completeness metadata (§6) ──────────────────────────────────────
def keyword_history_status(now: datetime | None = None) -> dict:
    """Metadata that lets the page state whether All-time is genuinely complete.

    ``history_complete`` is true only when every required (closed) month is in the
    ledger AND the recent incremental sync is current — so it stays true on the
    next calendar day (the current open month is proven by the daily sync, not by
    a bootstrap chunk). Uses the proven sync watermark, not MAX(source_date), so
    zero-activity recent days never falsely mark history incomplete."""
    import db.writers as w  # noqa: PLC0415
    end = _account_today(now)
    try:
        expected, _src = resolve_history_start_date()
        expected_iso = expected.isoformat()
    except KeywordBootstrapError:
        expected = None
        expected_iso = None

    cov_start, cov_end, rows = _durable_coverage()
    latest_sync = _latest_successful_sync_date(now)
    job = w.get_latest_recovery_job(BOOTSTRAP_JOB_TYPE)
    bootstrap_status = (job or {}).get("status") or "never_run"

    missing: list = []
    if expected is not None:
        completed = set((job or {}).get("completed_chunks") or [])
        for key in _required_month_keys(expected, end):
            if key not in completed:
                y, m = key.split("-")
                cs, ce = _month_bounds(int(y), int(m))
                missing.append({"month": key, "start": cs.isoformat(), "end": ce.isoformat()})

    # Recent incremental proves the current open period.
    recent_current = latest_sync is not None and (end - latest_sync).days <= 1

    history_complete = bool(expected is not None and not missing and recent_current)

    return {
        "history_start_expected": expected_iso,
        "durable_coverage_start": cov_start.isoformat() if cov_start else None,
        "durable_coverage_end": cov_end.isoformat() if cov_end else None,
        "durable_row_count": rows,
        "latest_successful_sync_date": latest_sync.isoformat() if latest_sync else None,
        "history_complete": history_complete,
        "missing_date_ranges": missing,
        "bootstrap_status": bootstrap_status,
        "bootstrap_summary": (job or {}).get("summary"),
    }


# ── Interval-union coverage (§B1) ────────────────────────────────────────────
def _merge_intervals(intervals: list) -> list:
    """Merge overlapping OR adjacent (gap ≤ 1 day) date intervals into a sorted,
    non-overlapping list. Adjacent daily/monthly batches therefore fuse into one
    continuous covered range."""
    ivs = sorted((a, b) for a, b in intervals if a and b and a <= b)
    merged: list = []
    for a, b in ivs:
        if merged and a <= merged[-1][1] + timedelta(days=1):
            if b > merged[-1][1]:
                merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


def _uncovered_ranges(ws: date, we: date, merged: list) -> list:
    """Dates in [ws, we] NOT covered by the (sorted, merged) intervals."""
    missing: list = []
    cursor = ws
    for a, b in merged:
        a = max(a, ws)
        b = min(b, we)
        if b < a:                      # interval falls outside the window
            continue
        if a > cursor:
            missing.append((cursor, a - timedelta(days=1)))
        cursor = max(cursor, b + timedelta(days=1))
        if cursor > we:
            break
    if cursor <= we:
        missing.append((cursor, we))
    return missing


def _keyword_success_intervals(window_start: date | None, window_end: date | None) -> list:
    import db.writers as w  # noqa: PLC0415
    return w.successful_batch_intervals(
        KEYWORD_FACTS_SOURCE, KEYWORD_FACTS_DATASET, window_start, window_end)


# ── Selected-window coverage proof (§6 / §B1) ────────────────────────────────
def selected_window_coverage(window_start: date | None, window_end: date | None, *,
                             is_all_time: bool, now: datetime | None = None) -> dict:
    """Backend proof of whether the SELECTED window was actually synced — from the
    UNION of successful keyword_facts sync-batch intervals, never inferred from
    MIN(source_date)/MAX(date_to). A finite window is fully synced only when that
    union covers every date in it; any gap (e.g. a failed month between two
    successful ones) is returned as a missing range. All-time defers to
    ``history_complete``. The frontend may present ``$0 Complete`` only when
    ``selected_window_fully_synced`` is true."""
    latest = _latest_successful_sync_date(now)

    if is_all_time:
        hist = keyword_history_status(now)
        fully = hist["history_complete"]
        exp = hist.get("history_start_expected")
        exp_date = date.fromisoformat(exp) if exp else None
        # Without a resolvable history start there is no meaningful all-time
        # coverage window — skip the batch-interval query rather than scan every
        # historical successful batch.
        merged = (_merge_intervals(_keyword_success_intervals(exp_date, _account_today(now)))
                  if exp_date else [])
        cov_ranges = [{"start": a.isoformat(), "end": b.isoformat()} for a, b in merged]
        return {
            "selected_window_coverage_status": (
                "fully_synced" if fully else ("not_synced" if not merged else "partial")),
            "selected_window_fully_synced": fully,
            "selected_window_coverage_start": exp,
            "selected_window_coverage_end": latest.isoformat() if latest else None,
            "successful_coverage_ranges": cov_ranges,
            "missing_window_ranges": hist["missing_date_ranges"],
            "latest_successful_sync_date": latest.isoformat() if latest else None,
        }

    if window_start is None or window_end is None:
        return {
            "selected_window_coverage_status": "unknown",
            "selected_window_fully_synced": False,
            "selected_window_coverage_start": None,
            "selected_window_coverage_end": None,
            "successful_coverage_ranges": [],
            "missing_window_ranges": [],
            "latest_successful_sync_date": latest.isoformat() if latest else None,
        }

    merged = _merge_intervals(_keyword_success_intervals(window_start, window_end))
    # Clip the merged union to the window for reporting.
    clipped = [(max(a, window_start), min(b, window_end)) for a, b in merged
               if a <= window_end and b >= window_start]
    missing = _uncovered_ranges(window_start, window_end, merged)
    fully = not missing and bool(clipped)

    return {
        "selected_window_coverage_status": (
            "fully_synced" if fully else ("not_synced" if not clipped else "partial")),
        "selected_window_fully_synced": fully,
        "selected_window_coverage_start": clipped[0][0].isoformat() if clipped else None,
        "selected_window_coverage_end": clipped[-1][1].isoformat() if clipped else None,
        "successful_coverage_ranges": [
            {"start": a.isoformat(), "end": b.isoformat()} for a, b in clipped],
        "missing_window_ranges": [
            {"start": a.isoformat(), "end": b.isoformat()} for a, b in missing],
        "latest_successful_sync_date": latest.isoformat() if latest else None,
    }
