"""
db/deal_company_association_repository.py

PR-ADS-161D2 — durable deal→company association evidence: the facts a CUSTOMER
count rests on.

Owns three tables and nothing else:
  * ``hubspot_deal_company_association``  — one row per (deal, company, path)
  * ``hubspot_deal_company_observation``  — per deal: latest attempt, latest
                                            success, and what that success saw
  * ``hubspot_deal_company_sync_state``   — run coverage and backfill generation

Guarantees
----------
**A success replaces, a failure preserves.** A successful observation replaces
the deal's association set and records itself in ONE transaction. A failed
lookup records the failed attempt and touches neither the bridge nor the
success columns — the last proven set stands, and a failure can never become a
"zero companies" fact.

**Zero is recorded, not inferred.** ``success_zero_companies`` is written by a
successful lookup that found nothing. A deal with no bridge rows and no success
observation was never observed; the two are different claims and this module
never lets one stand in for the other.

**Monotonic by observation start.** Every write carries the instant its HubSpot
read STARTED. A success applies only when it started at or after the stored
latest success; an attempt outcome applies only when it started at or after the
stored latest attempt. A slow historical pass therefore cannot overwrite a newer
observation, whatever order the commits land in.

**Database failures fail visibly.** Every function returns ``available: False``
with an error on any failure; none of them reports a failed read as an empty
result.

No external API calls. No writes to HubSpot, Google Ads or any other platform.
"""

from __future__ import annotations

import logging

from db.connection import get_conn

log = logging.getLogger(__name__)

BRIDGE_TABLE = "hubspot_deal_company_association"
OBSERVATION_TABLE = "hubspot_deal_company_observation"
SYNC_STATE_TABLE = "hubspot_deal_company_sync_state"
LEDGER_TABLE = "hubspot_deal_ledger"
SYNC_SCOPE = "deal_company_associations"

MODE_INCREMENTAL = "incremental"
MODE_BACKFILL = "backfill"
ALL_MODES = (MODE_INCREMENTAL, MODE_BACKFILL)

BACKFILL_IN_PROGRESS = "in_progress"
BACKFILL_COMPLETE = "complete"

#: Read-only, one snapshot — for the operator reads below.
READ_ONLY_TRANSACTION = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"

_STATE_COLUMNS = (
    "scope", "last_run_status", "last_run_mode", "last_run_started_at",
    "last_run_finished_at", "last_run_batch_id", "last_run_population",
    "last_run_attempted", "last_run_succeeded", "last_run_failed",
    "last_run_write_failures", "last_run_complete", "last_error",
    "last_full_run_status", "last_full_run_started_at", "last_full_run_complete",
    "last_complete_scan_started_at", "last_complete_scan_finished_at",
    "last_complete_scan_population", "backfill_status", "backfill_started_at",
    "backfill_completed_at", "updated_at",
)

OBSERVATION_COLUMNS = (
    "deal_id", "last_attempt_status", "last_attempt_started_at",
    "last_attempt_finished_at", "last_attempt_batch_id", "last_failure_reason",
    "last_success_started_at", "last_success_at", "last_success_batch_id",
    "success_company_count", "success_path_count", "success_zero_companies",
)

BRIDGE_COLUMNS = (
    "deal_id", "company_id", "association_category", "association_type_id",
    "association_label", "association_path_key", "last_observed_batch_id",
    "last_observed_at",
)


def _unavailable(**extra) -> dict:
    out = {"available": False, "reason": "database_unavailable"}
    out.update(extra)
    return out


def _iso(value):
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _rows(cur) -> list:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _normalise(row: dict) -> dict:
    return {k: (_iso(v) if hasattr(v, "isoformat") else v)
            for k, v in row.items()}


def _validated_paths(companies) -> list:
    """The observed paths, validated BEFORE any write. A malformed path aborts
    the whole observation rather than writing part of it."""
    paths, seen = [], set()
    for c in companies or []:
        company_id = str(c.get("company_id") or "").strip()
        category = str(c.get("association_category") or "").strip()
        type_id = str(c.get("association_type_id") or "").strip()
        path_key = str(c.get("association_path_key") or "").strip()
        if not (company_id and category and type_id and path_key):
            raise ValueError("malformed association path")
        if (company_id, path_key) in seen:
            continue
        seen.add((company_id, path_key))
        paths.append({"company_id": company_id, "association_category": category,
                      "association_type_id": type_id,
                      "association_label": c.get("association_label"),
                      "association_path_key": path_key})
    return paths


# ═════════════════════════════════════════════════════════════════════════════
# Writes
# ═════════════════════════════════════════════════════════════════════════════

def record_observation_success(deal_id, companies, *, observation_started_at,
                               batch_id=None) -> dict:
    """Record a COMPLETE, successful lookup and its association set, atomically.

    ``observation_started_at`` is when the HubSpot read began. The observation
    applies only if it is at least as recent as the stored latest success;
    otherwise nothing is written and ``skipped_stale`` is 1. The attempt columns
    move only if it is also at least as recent as the stored latest attempt — a
    newer FAILED attempt stays the latest attempt.

    Returns ``{available, applied, skipped_stale, paths_written, error}``.
    """
    if deal_id in (None, ""):
        return _unavailable(reason="missing_deal_id", error="missing_deal_id",
                            applied=False, skipped_stale=0, paths_written=0)
    if observation_started_at is None:
        return _unavailable(reason="missing_observation_started_at",
                            error="missing_observation_started_at",
                            applied=False, skipped_stale=0, paths_written=0)
    try:
        paths = _validated_paths(companies)
    except ValueError as exc:
        return _unavailable(reason="invalid_observation", error=str(exc),
                            applied=False, skipped_stale=0, paths_written=0)
    deal_id = str(deal_id)
    company_count = len({p["company_id"] for p in paths})
    params = {"deal_id": deal_id, "started": observation_started_at,
              "batch_id": batch_id, "companies": company_count,
              "paths": len(paths), "zero": company_count == 0}
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(applied=False, skipped_stale=0,
                                    paths_written=0, error="database_unavailable")
            with conn.cursor() as cur:
                # The conditional upsert both DECIDES and LOCKS: the row lock it
                # takes is held until commit, so two writers for one deal
                # serialize and the later observation wins regardless of which
                # commits first.
                cur.execute(
                    f"""
                    INSERT INTO {OBSERVATION_TABLE} (
                        deal_id, last_attempt_status, last_attempt_started_at,
                        last_attempt_finished_at, last_attempt_batch_id,
                        last_failure_reason, last_success_started_at,
                        last_success_at, last_success_batch_id,
                        success_company_count, success_path_count,
                        success_zero_companies, created_at, updated_at)
                    VALUES (%(deal_id)s, 'success', %(started)s,
                            clock_timestamp(), %(batch_id)s, NULL, %(started)s,
                            clock_timestamp(), %(batch_id)s, %(companies)s,
                            %(paths)s, %(zero)s, NOW(), NOW())
                    ON CONFLICT (deal_id) DO UPDATE SET
                        last_success_started_at = EXCLUDED.last_success_started_at,
                        last_success_at         = EXCLUDED.last_success_at,
                        last_success_batch_id   = EXCLUDED.last_success_batch_id,
                        success_company_count   = EXCLUDED.success_company_count,
                        success_path_count      = EXCLUDED.success_path_count,
                        success_zero_companies  = EXCLUDED.success_zero_companies,
                        -- The attempt columns follow only if this is ALSO the
                        -- most recent attempt; a newer failure stays latest.
                        last_attempt_status = CASE
                            WHEN EXCLUDED.last_attempt_started_at
                                 >= {OBSERVATION_TABLE}.last_attempt_started_at
                            THEN 'success'
                            ELSE {OBSERVATION_TABLE}.last_attempt_status END,
                        last_failure_reason = CASE
                            WHEN EXCLUDED.last_attempt_started_at
                                 >= {OBSERVATION_TABLE}.last_attempt_started_at
                            THEN NULL
                            ELSE {OBSERVATION_TABLE}.last_failure_reason END,
                        last_attempt_finished_at = CASE
                            WHEN EXCLUDED.last_attempt_started_at
                                 >= {OBSERVATION_TABLE}.last_attempt_started_at
                            THEN EXCLUDED.last_attempt_finished_at
                            ELSE {OBSERVATION_TABLE}.last_attempt_finished_at END,
                        last_attempt_batch_id = CASE
                            WHEN EXCLUDED.last_attempt_started_at
                                 >= {OBSERVATION_TABLE}.last_attempt_started_at
                            THEN EXCLUDED.last_attempt_batch_id
                            ELSE {OBSERVATION_TABLE}.last_attempt_batch_id END,
                        last_attempt_started_at = GREATEST(
                            EXCLUDED.last_attempt_started_at,
                            {OBSERVATION_TABLE}.last_attempt_started_at),
                        updated_at = NOW()
                    WHERE {OBSERVATION_TABLE}.last_success_started_at IS NULL
                       OR EXCLUDED.last_success_started_at
                          >= {OBSERVATION_TABLE}.last_success_started_at
                    RETURNING deal_id
                    """, params)
                applied = cur.fetchone() is not None
                if applied:
                    keys = [f"{p['company_id']}|{p['association_path_key']}"
                            for p in paths]
                    # Paths HubSpot no longer reports are removed — safe only
                    # because this is a successful, complete observation.
                    cur.execute(
                        f"DELETE FROM {BRIDGE_TABLE} WHERE deal_id = %s "
                        f"AND NOT ((company_id || '|' || association_path_key)"
                        f" = ANY(%s::text[]))", (deal_id, keys))
                    for p in paths:
                        cur.execute(
                            f"""
                            INSERT INTO {BRIDGE_TABLE} (
                                deal_id, company_id, association_category,
                                association_type_id, association_label,
                                association_path_key, last_observed_batch_id,
                                last_observed_at, created_at, updated_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                            ON CONFLICT (deal_id, company_id, association_path_key)
                            DO UPDATE SET
                                association_category = EXCLUDED.association_category,
                                association_type_id  = EXCLUDED.association_type_id,
                                association_label    = EXCLUDED.association_label,
                                last_observed_batch_id
                                    = EXCLUDED.last_observed_batch_id,
                                last_observed_at     = EXCLUDED.last_observed_at,
                                updated_at           = NOW()
                            """,
                            (deal_id, p["company_id"], p["association_category"],
                             p["association_type_id"], p["association_label"],
                             p["association_path_key"], batch_id,
                             observation_started_at))
        return {"available": True, "applied": applied,
                "skipped_stale": 0 if applied else 1,
                "paths_written": len(paths) if applied else 0, "error": None}
    except Exception as exc:  # noqa: BLE001
        log.error("record_observation_success failed for %s: %s", deal_id, exc)
        return _unavailable(applied=False, skipped_stale=0, paths_written=0,
                            error=type(exc).__name__)


def record_observation_failure(deal_id, *, observation_started_at, reason,
                               batch_id=None) -> dict:
    """Record a FAILED lookup. Never touches the bridge or the success columns.

    Applies only if the attempt started at or after the stored latest attempt.
    Returns ``{available, applied, error}``.
    """
    if deal_id in (None, "") or observation_started_at is None:
        return _unavailable(reason="invalid_failure_record",
                            error="missing deal_id or observation_started_at",
                            applied=False)
    reason = (str(reason or "lookup_failed"))[:200]
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(applied=False, error="database_unavailable")
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {OBSERVATION_TABLE} (
                        deal_id, last_attempt_status, last_attempt_started_at,
                        last_attempt_finished_at, last_attempt_batch_id,
                        last_failure_reason, created_at, updated_at)
                    VALUES (%s, 'failed', %s, clock_timestamp(), %s, %s,
                            NOW(), NOW())
                    ON CONFLICT (deal_id) DO UPDATE SET
                        last_attempt_status      = 'failed',
                        last_attempt_started_at  = EXCLUDED.last_attempt_started_at,
                        last_attempt_finished_at = EXCLUDED.last_attempt_finished_at,
                        last_attempt_batch_id    = EXCLUDED.last_attempt_batch_id,
                        last_failure_reason      = EXCLUDED.last_failure_reason,
                        updated_at               = NOW()
                    WHERE EXCLUDED.last_attempt_started_at
                          >= {OBSERVATION_TABLE}.last_attempt_started_at
                    RETURNING deal_id
                    """,
                    (str(deal_id), observation_started_at, batch_id, reason))
                applied = cur.fetchone() is not None
        return {"available": True, "applied": applied, "error": None}
    except Exception as exc:  # noqa: BLE001
        log.error("record_observation_failure failed for %s: %s", deal_id, exc)
        return _unavailable(applied=False, error=type(exc).__name__)


def record_sync_run(*, mode: str, status: str, started_at, batch_id=None,
                    population: int = 0, attempted: int = 0,
                    succeeded: int = 0, failed: int = 0,
                    write_failures: int = 0, complete: bool = False,
                    full_population: bool = False,
                    error: str | None = None) -> dict:
    """Record a run's outcome.

    ``full_population``: the run looked at the WHOLE won population (or failed
    before it could). Only such a run moves the ``last_full_run_*`` columns —
    the ones that answer "is the evidence current?" — so a backfill resume pass
    over a subset can neither prove nor disprove currency.

    The complete-scan columns move ONLY for a full-population run that
    succeeded AND observed every won deal — coverage is proven by the run that
    covered it, never assumed from rows existing.

    ``started_at`` must come from the DATABASE clock (:func:`db_now`): every
    run-state field is ordered by it, so an older run that commits after a
    newer one cannot overwrite the newer verdict."""
    if mode not in ALL_MODES:
        return _unavailable(reason="invalid_mode", error=f"unknown mode {mode!r}")
    if status not in ("success", "partial", "failed"):
        return _unavailable(reason="invalid_status",
                            error=f"unknown status {status!r}")
    if started_at is None:
        return _unavailable(reason="missing_started_at",
                            error="started_at is required (database clock)")
    proves = bool(complete and status == "success" and full_population)
    params = {"full": bool(full_population),"scope": SYNC_SCOPE, "status": status, "mode": mode,
              "started": started_at, "batch_id": batch_id,
              "population": int(population), "attempted": int(attempted),
              "succeeded": int(succeeded), "failed": int(failed),
              "write_failures": int(write_failures), "complete": bool(complete),
              "error": (error or None) and str(error)[:1000], "proves": proves}
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(error="database_unavailable")
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {SYNC_STATE_TABLE} (
                        scope, last_run_status, last_run_mode,
                        last_run_started_at, last_run_finished_at,
                        last_run_batch_id, last_run_population,
                        last_run_attempted, last_run_succeeded, last_run_failed,
                        last_run_write_failures, last_run_complete, last_error,
                        last_full_run_status, last_full_run_started_at,
                        last_full_run_complete,
                        last_complete_scan_started_at,
                        last_complete_scan_finished_at,
                        last_complete_scan_population, updated_at)
                    VALUES (%(scope)s, %(status)s, %(mode)s, %(started)s,
                            clock_timestamp(), %(batch_id)s, %(population)s,
                            %(attempted)s, %(succeeded)s, %(failed)s,
                            %(write_failures)s, %(complete)s, %(error)s,
                            CASE WHEN %(full)s THEN %(status)s END,
                            CASE WHEN %(full)s THEN %(started)s END,
                            CASE WHEN %(full)s THEN %(complete)s END,
                            CASE WHEN %(proves)s THEN %(started)s END,
                            CASE WHEN %(proves)s THEN clock_timestamp() END,
                            CASE WHEN %(proves)s THEN %(population)s END,
                            NOW())
                    ON CONFLICT (scope) DO UPDATE SET
                        -- Every run-state field is ordered by RUN START (database
                        -- clock), never by commit order: an older run committing
                        -- after a newer one must not overwrite its verdict.
                        last_run_status = CASE
                            WHEN {SYNC_STATE_TABLE}.last_run_started_at IS NULL
                              OR %(started)s >= {SYNC_STATE_TABLE}.last_run_started_at
                            THEN EXCLUDED.last_run_status ELSE {SYNC_STATE_TABLE}.last_run_status END,
                        last_run_mode = CASE
                            WHEN {SYNC_STATE_TABLE}.last_run_started_at IS NULL
                              OR %(started)s >= {SYNC_STATE_TABLE}.last_run_started_at
                            THEN EXCLUDED.last_run_mode ELSE {SYNC_STATE_TABLE}.last_run_mode END,
                        last_run_started_at = CASE
                            WHEN {SYNC_STATE_TABLE}.last_run_started_at IS NULL
                              OR %(started)s >= {SYNC_STATE_TABLE}.last_run_started_at
                            THEN EXCLUDED.last_run_started_at ELSE {SYNC_STATE_TABLE}.last_run_started_at END,
                        last_run_finished_at = CASE
                            WHEN {SYNC_STATE_TABLE}.last_run_started_at IS NULL
                              OR %(started)s >= {SYNC_STATE_TABLE}.last_run_started_at
                            THEN EXCLUDED.last_run_finished_at ELSE {SYNC_STATE_TABLE}.last_run_finished_at END,
                        last_run_batch_id = CASE
                            WHEN {SYNC_STATE_TABLE}.last_run_started_at IS NULL
                              OR %(started)s >= {SYNC_STATE_TABLE}.last_run_started_at
                            THEN EXCLUDED.last_run_batch_id ELSE {SYNC_STATE_TABLE}.last_run_batch_id END,
                        last_run_population = CASE
                            WHEN {SYNC_STATE_TABLE}.last_run_started_at IS NULL
                              OR %(started)s >= {SYNC_STATE_TABLE}.last_run_started_at
                            THEN EXCLUDED.last_run_population ELSE {SYNC_STATE_TABLE}.last_run_population END,
                        last_run_attempted = CASE
                            WHEN {SYNC_STATE_TABLE}.last_run_started_at IS NULL
                              OR %(started)s >= {SYNC_STATE_TABLE}.last_run_started_at
                            THEN EXCLUDED.last_run_attempted ELSE {SYNC_STATE_TABLE}.last_run_attempted END,
                        last_run_succeeded = CASE
                            WHEN {SYNC_STATE_TABLE}.last_run_started_at IS NULL
                              OR %(started)s >= {SYNC_STATE_TABLE}.last_run_started_at
                            THEN EXCLUDED.last_run_succeeded ELSE {SYNC_STATE_TABLE}.last_run_succeeded END,
                        last_run_failed = CASE
                            WHEN {SYNC_STATE_TABLE}.last_run_started_at IS NULL
                              OR %(started)s >= {SYNC_STATE_TABLE}.last_run_started_at
                            THEN EXCLUDED.last_run_failed ELSE {SYNC_STATE_TABLE}.last_run_failed END,
                        last_run_write_failures = CASE
                            WHEN {SYNC_STATE_TABLE}.last_run_started_at IS NULL
                              OR %(started)s >= {SYNC_STATE_TABLE}.last_run_started_at
                            THEN EXCLUDED.last_run_write_failures ELSE {SYNC_STATE_TABLE}.last_run_write_failures END,
                        last_run_complete = CASE
                            WHEN {SYNC_STATE_TABLE}.last_run_started_at IS NULL
                              OR %(started)s >= {SYNC_STATE_TABLE}.last_run_started_at
                            THEN EXCLUDED.last_run_complete ELSE {SYNC_STATE_TABLE}.last_run_complete END,
                        last_error = CASE
                            WHEN {SYNC_STATE_TABLE}.last_run_started_at IS NULL
                              OR %(started)s >= {SYNC_STATE_TABLE}.last_run_started_at
                            THEN EXCLUDED.last_error ELSE {SYNC_STATE_TABLE}.last_error END,
                        last_full_run_status = CASE
                            WHEN %(full)s AND ({SYNC_STATE_TABLE}.last_full_run_started_at IS NULL
                              OR %(started)s >= {SYNC_STATE_TABLE}.last_full_run_started_at)
                            THEN EXCLUDED.last_run_status ELSE {SYNC_STATE_TABLE}.last_full_run_status END,
                        last_full_run_complete = CASE
                            WHEN %(full)s AND ({SYNC_STATE_TABLE}.last_full_run_started_at IS NULL
                              OR %(started)s >= {SYNC_STATE_TABLE}.last_full_run_started_at)
                            THEN EXCLUDED.last_run_complete ELSE {SYNC_STATE_TABLE}.last_full_run_complete END,
                        last_full_run_started_at = CASE
                            WHEN %(full)s AND ({SYNC_STATE_TABLE}.last_full_run_started_at IS NULL
                              OR %(started)s >= {SYNC_STATE_TABLE}.last_full_run_started_at)
                            THEN EXCLUDED.last_run_started_at
                            ELSE {SYNC_STATE_TABLE}.last_full_run_started_at END,
                        -- A complete scan is never un-proven by a later partial
                        -- run, and never moved backwards by an older one.
                        last_complete_scan_started_at = CASE
                            WHEN %(proves)s AND (
                                {SYNC_STATE_TABLE}.last_complete_scan_started_at IS NULL
                                OR %(started)s
                                   >= {SYNC_STATE_TABLE}.last_complete_scan_started_at)
                            THEN %(started)s
                            ELSE {SYNC_STATE_TABLE}.last_complete_scan_started_at END,
                        last_complete_scan_finished_at = CASE
                            WHEN %(proves)s AND (
                                {SYNC_STATE_TABLE}.last_complete_scan_started_at IS NULL
                                OR %(started)s
                                   >= {SYNC_STATE_TABLE}.last_complete_scan_started_at)
                            THEN clock_timestamp()
                            ELSE {SYNC_STATE_TABLE}.last_complete_scan_finished_at END,
                        last_complete_scan_population = CASE
                            WHEN %(proves)s AND (
                                {SYNC_STATE_TABLE}.last_complete_scan_started_at IS NULL
                                OR %(started)s
                                   >= {SYNC_STATE_TABLE}.last_complete_scan_started_at)
                            THEN %(population)s
                            ELSE {SYNC_STATE_TABLE}.last_complete_scan_population END,
                        updated_at = NOW()
                    """, params)
        return {"available": True, "error": None}
    except Exception as exc:  # noqa: BLE001
        log.error("record_sync_run failed: %s", exc)
        return _unavailable(error=type(exc).__name__)


def start_backfill_generation(started_at) -> dict:
    """Open a new historical backfill generation anchored at ``started_at``."""
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(error="database_unavailable")
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {SYNC_STATE_TABLE} (
                        scope, backfill_status, backfill_started_at,
                        backfill_completed_at, updated_at)
                    VALUES (%s, %s, %s, NULL, NOW())
                    ON CONFLICT (scope) DO UPDATE SET
                        backfill_status = EXCLUDED.backfill_status,
                        backfill_started_at = EXCLUDED.backfill_started_at,
                        backfill_completed_at = NULL,
                        updated_at = NOW()
                    """, (SYNC_SCOPE, BACKFILL_IN_PROGRESS, started_at))
        return {"available": True, "error": None}
    except Exception as exc:  # noqa: BLE001
        log.error("start_backfill_generation failed: %s", exc)
        return _unavailable(error=type(exc).__name__)


def complete_backfill_generation(generation_started_at, *,
                                 population: int) -> dict:
    """Mark the generation complete — ONLY the generation it was proven for.

    Matching on the generation's own start instant means a stale process can
    never complete a generation an operator has since restarted. A completed
    generation proves every won deal was observed successfully at or after its
    start, so it also stands as a complete scan (moved forward only).
    Returns ``{available, completed}``.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(completed=False, error="database_unavailable")
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {SYNC_STATE_TABLE}
                       SET backfill_status = %(complete)s,
                           backfill_completed_at = clock_timestamp(),
                           -- A completed generation observed every won deal:
                           -- it is a full, successful, complete run.
                           last_full_run_status = CASE
                               WHEN last_full_run_started_at IS NULL
                                 OR %(gen)s >= last_full_run_started_at
                               THEN 'success' ELSE last_full_run_status END,
                           last_full_run_complete = CASE
                               WHEN last_full_run_started_at IS NULL
                                 OR %(gen)s >= last_full_run_started_at
                               THEN TRUE ELSE last_full_run_complete END,
                           last_full_run_started_at = GREATEST(
                               last_full_run_started_at, %(gen)s::timestamptz),
                           last_complete_scan_started_at = CASE
                               WHEN last_complete_scan_started_at IS NULL
                                 OR %(gen)s >= last_complete_scan_started_at
                               THEN %(gen)s
                               ELSE last_complete_scan_started_at END,
                           last_complete_scan_finished_at = CASE
                               WHEN last_complete_scan_started_at IS NULL
                                 OR %(gen)s >= last_complete_scan_started_at
                               THEN clock_timestamp()
                               ELSE last_complete_scan_finished_at END,
                           last_complete_scan_population = CASE
                               WHEN last_complete_scan_started_at IS NULL
                                 OR %(gen)s >= last_complete_scan_started_at
                               THEN %(population)s
                               ELSE last_complete_scan_population END,
                           updated_at = NOW()
                     WHERE scope = %(scope)s AND backfill_started_at = %(gen)s
                       AND backfill_status = %(in_progress)s
                     RETURNING scope
                    """, {"complete": BACKFILL_COMPLETE, "scope": SYNC_SCOPE,
                          "gen": generation_started_at,
                          "population": int(population),
                          "in_progress": BACKFILL_IN_PROGRESS})
                completed = cur.fetchone() is not None
        return {"available": True, "completed": completed, "error": None}
    except Exception as exc:  # noqa: BLE001
        log.error("complete_backfill_generation failed: %s", exc)
        return _unavailable(completed=False, error=type(exc).__name__)


def db_now() -> dict:
    """``{available, now}`` — the DATABASE clock, the one instant every
    observation is ordered by. Using the caller's clock would let a host whose
    clock runs ahead make genuinely newer reads look stale."""
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(now=None)
            with conn.cursor() as cur:
                cur.execute("SELECT clock_timestamp()")
                return {"available": True, "now": cur.fetchone()[0]}
    except Exception as exc:  # noqa: BLE001
        log.error("db_now failed: %s", exc)
        return _unavailable(now=None, error=type(exc).__name__)


# ═════════════════════════════════════════════════════════════════════════════
# Reads (READ ONLY transactions)
# ═════════════════════════════════════════════════════════════════════════════

def fetch_won_deal_population() -> dict:
    """The authoritative population: every deal the canonical ledger holds as
    won (``hs_is_closed_won IS TRUE``), with its last modification time.

    Returns ``{available, deals: [{deal_id, hubspot_lastmodified_at}]}`` ordered
    by deal_id. ``available: False`` on any failure — never an empty population.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(deals=[])
            with conn.cursor() as cur:
                cur.execute(READ_ONLY_TRANSACTION)
                cur.execute(
                    f"SELECT deal_id, hubspot_lastmodified_at FROM {LEDGER_TABLE} "
                    f"WHERE hs_is_closed_won IS TRUE ORDER BY deal_id")
                deals = [_normalise(r) for r in _rows(cur)]
        return {"available": True, "deals": deals}
    except Exception as exc:  # noqa: BLE001
        log.error("fetch_won_deal_population failed: %s", exc)
        return _unavailable(deals=[], error=type(exc).__name__)


def fetch_sync_state() -> dict:
    """``{available, row}`` — ``row`` is None when no run was ever recorded."""
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(row=None)
            with conn.cursor() as cur:
                cur.execute(READ_ONLY_TRANSACTION)
                rows = read_sync_state(cur)
        return {"available": True, "row": rows}
    except Exception as exc:  # noqa: BLE001
        log.error("fetch_sync_state failed: %s", exc)
        return _unavailable(row=None, error=type(exc).__name__)


def fetch_backfill_progress(generation_started_at) -> dict:
    """Which won deals still lack a success observed at or after the generation
    anchor. One read-only snapshot.

    Returns ``{available, population, done, remaining: [deal_id, ...]}``.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return _unavailable(population=None, done=None, remaining=[])
            with conn.cursor() as cur:
                cur.execute(READ_ONLY_TRANSACTION)
                cur.execute(
                    f"""
                    SELECT l.deal_id,
                           (o.last_success_started_at IS NOT NULL
                            AND o.last_success_started_at >= %s) AS done
                      FROM {LEDGER_TABLE} l
                      LEFT JOIN {OBSERVATION_TABLE} o ON o.deal_id = l.deal_id
                     WHERE l.hs_is_closed_won IS TRUE
                     ORDER BY l.deal_id
                    """, (generation_started_at,))
                rows = _rows(cur)
        remaining = [str(r["deal_id"]) for r in rows if not r["done"]]
        return {"available": True, "population": len(rows),
                "done": len(rows) - len(remaining), "remaining": remaining}
    except Exception as exc:  # noqa: BLE001
        log.error("fetch_backfill_progress failed: %s", exc)
        return _unavailable(population=None, done=None, remaining=[],
                            error=type(exc).__name__)


# ── Snapshot helpers: run on the CALLER'S cursor, inside its transaction ────

def read_sync_state(cur) -> dict | None:
    cur.execute(f"SELECT {', '.join(_STATE_COLUMNS)} FROM {SYNC_STATE_TABLE} "
                f"WHERE scope = %s", (SYNC_SCOPE,))
    rows = [_normalise(r) for r in _rows(cur)]
    return rows[0] if rows else None


def read_won_deal_company_evidence(cur) -> dict:
    """Association paths and observation state for every won deal, read on the
    caller's cursor so they share the caller's snapshot.

    Returns ``{paths, observations, sync_state}``. The bridge rows are those of
    each deal's latest SUCCESSFUL observation; a deal with no observation row
    was never looked at. Each observation carries the deal's ledger
    ``hubspot_lastmodified_at`` (``deal_lastmodified_at``) so a reader can tell
    evidence observed before the deal last changed.
    """
    cur.execute(
        f"""
        SELECT a.deal_id, a.company_id, a.association_category,
               a.association_type_id, a.association_path_key,
               a.last_observed_batch_id, a.last_observed_at
          FROM {BRIDGE_TABLE} a
          JOIN {LEDGER_TABLE} l
            ON l.deal_id = a.deal_id AND l.hs_is_closed_won IS TRUE
         ORDER BY a.deal_id, a.company_id, a.association_path_key
        """)
    paths = [_normalise(r) for r in _rows(cur)]
    cur.execute(
        f"""
        SELECT {', '.join('o.' + c for c in OBSERVATION_COLUMNS)},
               l.hubspot_lastmodified_at AS deal_lastmodified_at
          FROM {OBSERVATION_TABLE} o
          JOIN {LEDGER_TABLE} l
            ON l.deal_id = o.deal_id AND l.hs_is_closed_won IS TRUE
         ORDER BY o.deal_id
        """)
    observations = [_normalise(r) for r in _rows(cur)]
    return {"paths": paths, "observations": observations,
            "sync_state": read_sync_state(cur)}


__all__ = [
    "BRIDGE_TABLE", "OBSERVATION_TABLE", "SYNC_STATE_TABLE", "SYNC_SCOPE",
    "MODE_INCREMENTAL", "MODE_BACKFILL", "BACKFILL_IN_PROGRESS",
    "BACKFILL_COMPLETE", "READ_ONLY_TRANSACTION",
    "record_observation_success", "record_observation_failure",
    "record_sync_run", "start_backfill_generation",
    "complete_backfill_generation", "db_now", "fetch_won_deal_population",
    "fetch_sync_state", "fetch_backfill_progress", "read_sync_state",
    "read_won_deal_company_evidence",
]
