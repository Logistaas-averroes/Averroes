"""PR-ADS-161J — run the platform certification and record its verdict.

The daily scheduler calls :func:`run_and_record` after the morning sync. It
runs ``scripts.audit_platform_certification`` (which runs the six production
truth audits, each in its own subprocess) and stores ONE row in
``platform_certification_runs``. No gate logic lives here.

Fail closed: if the certification itself cannot run, the recorded verdict is
``NOT CERTIFIED`` (exit 2) with the error — never a missing row that could be
read as "nothing to report", and never a pass.

Writes only to the local database. No HubSpot or Google Ads call is made here;
the audits it runs are read-only.

PR-ADS-169A — a run that dies still leaves a row. The row is written BEFORE
the audits start (``status = 'running'``, already NOT CERTIFIED, no
``finished_at``), its ``heartbeat_at`` moves while the audits run, and it is
finalized in place afterwards. A ``running`` row whose heartbeat stopped for
``interrupted_after_minutes`` is presumed interrupted — its cause is not
observed (the 512Mi instance was OOM-killed mid-run on 2026-11-19 and
2026-11-20 and no row was written): :func:`close_interrupted_runs` closes it NOT CERTIFIED, "heartbeat
stopped; presumed interrupted". It runs at startup, again once the grace has
passed, and before every run. Never a missing day, never a pass.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

VERDICT_NOT_CERTIFIED = "NOT CERTIFIED"
EXIT_UNAVAILABLE = 2

STATUS_RUNNING = "running"
STATUS_FINISHED = "finished"
STATUS_INTERRUPTED = "interrupted"
INTERRUPTED_ERROR = "heartbeat stopped; presumed interrupted"
DEFAULT_INTERRUPTED_AFTER_MINUTES = 5


def interrupted_after_minutes() -> float:
    """``platform_certification.interrupted_after_minutes`` (config)."""
    try:
        import yaml  # noqa: PLC0415
        with open("config/thresholds.yaml", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        return float(cfg["platform_certification"]["interrupted_after_minutes"])
    except Exception:  # noqa: BLE001
        return float(DEFAULT_INTERRUPTED_AFTER_MINUTES)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def summarize(report: dict) -> dict:
    """The stored summary: each audit's outcome and the CRM data to fix.

    The per-audit raw reports are dropped — they are large, and the verdict,
    violations and unavailable reasons are what a reader needs.
    """
    audits = []
    for a in report.get("audits") or []:
        kept = _drawer_populations(a.get("report"))
        if kept is not None:
            a = {**a, "drawer_populations": kept}
        pinning = snapshot_pinning(a.get("report"))
        if pinning is not None:
            a = {**a, "snapshot_pinning": pinning}
        audits.append({k: a.get(k) for k in (
            "key", "module", "outcome", "exit_code", "seconds", "violations",
            "unavailable", "detail",
            # PR-ADS-169A: what each audit really used, against its cap.
            "memory_budget_mb", "peak_rss_mb", "vm_peak_mb",
            # The budget's inputs (limit, usage, margin, floor, backstop).
            "memory")
            # PR-ADS-177C-F1: present only for an audit that probes campaign
            # drawers (campaigns) — see `_drawer_populations`.
            + (("drawer_populations",) if "drawer_populations" in a else ())
            # PR-ADS-179B-F1: present only for an audit that reads windows in
            # pinned snapshots — see `snapshot_pinning`.
            + (("snapshot_pinning",) if "snapshot_pinning" in a else ())})
    return {"verdict": report.get("verdict"),
            "exit_code": report.get("exit_code"),
            "generated_at": report.get("generated_at"),
            "audits": audits,
            "crm_data_to_fix": report.get("crm_data_to_fix")}


def snapshot_pinning(audit_report) -> dict | None:
    """PR-ADS-179B-F1 — an audit report's ``snapshot_pinning`` summary
    (``services.snapshot_memo.pinning_summary``), or None for an audit that
    reads no certification window.

    The raw report is dropped from the stored summary. Without this, a
    CERTIFIED day whose windows ran unpinned — or broke — stored the same row
    as one read in intact snapshots. The windows that were not pinned are
    named, with their reasons."""
    if not isinstance(audit_report, dict):
        return None
    pinning = audit_report.get("snapshot_pinning")
    if not isinstance(pinning, dict):
        return None
    return {"all_pinned": pinning.get("all_pinned") is True,
            "windows": pinning.get("windows"), "pinned": pinning.get("pinned"),
            "unpinned": [{"window": u.get("window"), "reason": u.get("reason")}
                         for u in pinning.get("unpinned") or []
                         if isinstance(u, dict)],
            "note": pinning.get("note")}


def _drawer_populations(audit_report) -> dict | None:
    """PR-ADS-177C-F1 — ``{window: {drawers_probed, rows_with_deals,
    canonical_revenue}}`` from an audit report that probed campaign drawers
    (the Campaigns certification's L6), else None.

    The raw report is dropped from the stored summary. Without this, a run
    whose L6 held because it probed NO drawer (no row carried deals, or the
    window was not built) stored the same row as one that probed every drawer.
    This replaces PR-ADS-172A's ``_drawer_sections``, the record of the retired
    Campaign Evidence drawer sections. A window not audited is stored with
    ``drawers_probed`` None, not left out.
    """
    if not isinstance(audit_report, dict):
        return None
    per_window = audit_report.get("per_window")
    if not isinstance(per_window, dict):
        return None
    if not any(isinstance(w, dict) and "drawers_probed" in w for w in per_window.values()):
        return None
    out = {}
    for window, result in per_window.items():
        result = result if isinstance(result, dict) else {}
        out[str(window)] = {k: result.get(k) for k in
                            ("drawers_probed", "rows_with_deals", "canonical_revenue")}
    return out


def _execute(sql: str, params=(), *, fetch: bool = False):
    """Run one statement. Returns rowcount (or the first column of the first
    row when ``fetch``); None when the database is unavailable or it failed."""
    from db.connection import get_conn  # noqa: PLC0415
    try:
        with get_conn() as conn:
            if conn is None:
                return None
            with conn.cursor() as cur:
                cur.execute(sql, params)
                if fetch:
                    row = cur.fetchone()
                    return row[0] if row else None
                return cur.rowcount
    except Exception as exc:  # noqa: BLE001
        log.error("[platform_certification] database write failed: %s", exc)
        return None


def running_summary(current_audit=None) -> dict:
    """The summary a run carries while it runs: NOT CERTIFIED, nothing held."""
    return {"verdict": VERDICT_NOT_CERTIFIED, "exit_code": EXIT_UNAVAILABLE,
            "status": STATUS_RUNNING, "current_audit": current_audit,
            "audits": [], "crm_data_to_fix": None}


def open_run(started_at, *, run_reason: str = "scheduled"):
    """Write the run's row before any audit starts. Returns its id, or None."""
    return _execute(
        """INSERT INTO platform_certification_runs
           (started_at, finished_at, exit_code, verdict, run_reason, summary,
            status, heartbeat_at)
           VALUES (%s, NULL, %s, %s, %s, %s::jsonb, %s, clock_timestamp())
           RETURNING id""",
        (started_at, EXIT_UNAVAILABLE, VERDICT_NOT_CERTIFIED, run_reason,
         json.dumps(running_summary()), STATUS_RUNNING), fetch=True)


def heartbeat(run_id, current_audit=None) -> bool:
    """Show the run is alive (and, when given, which audit it is on)."""
    if run_id is None:
        return False
    if current_audit is None:
        n = _execute("""UPDATE platform_certification_runs
                        SET heartbeat_at = clock_timestamp()
                        WHERE id = %s AND status = %s""",
                     (run_id, STATUS_RUNNING))
    else:
        n = _execute("""UPDATE platform_certification_runs
                        SET heartbeat_at = clock_timestamp(),
                            summary = jsonb_set(summary, '{current_audit}',
                                                to_jsonb(%s::text))
                        WHERE id = %s AND status = %s""",
                     (current_audit, run_id, STATUS_RUNNING))
    return n == 1


def close_interrupted_runs(*, older_than_minutes: float | None = None) -> int | None:
    """Close every ``running`` row whose heartbeat stopped as NOT CERTIFIED.

    Returns how many rows the database closed (None if it could not be asked).
    A row is judged by the database's own clock, against its last heartbeat
    (or its start, if it never beat).
    """
    minutes = interrupted_after_minutes() if older_than_minutes is None \
        else float(older_than_minutes)
    n = _execute(
        """UPDATE platform_certification_runs
           SET status = %s, finished_at = clock_timestamp(), exit_code = %s,
               verdict = %s,
               summary = summary || jsonb_build_object(
                   'status', %s::text, 'verdict', %s::text,
                   'exit_code', %s::int, 'error', %s::text)
           WHERE status = %s
             AND COALESCE(heartbeat_at, started_at)
                 < clock_timestamp() - make_interval(secs => %s)""",
        (STATUS_INTERRUPTED, EXIT_UNAVAILABLE, VERDICT_NOT_CERTIFIED,
         STATUS_INTERRUPTED, VERDICT_NOT_CERTIFIED, EXIT_UNAVAILABLE,
         INTERRUPTED_ERROR, STATUS_RUNNING, minutes * 60.0))
    if n:
        log.error("[platform_certification] closed %s interrupted run(s) as "
                  "NOT CERTIFIED: %s", n, INTERRUPTED_ERROR)
    return n


def record(started_at, finished_at, exit_code: int, verdict: str,
           summary: dict, *, run_reason: str = "scheduled",
           run_id=None) -> bool:
    """Finalize the run's row (``run_id``), or insert one verdict row when no
    row was opened. Returns False when the write did not happen.

    A row already closed as interrupted (its heartbeat was missed while the
    run in fact went on) is finalized all the same: the real verdict replaces
    the guess.
    """
    params = (finished_at, int(exit_code), verdict,
              json.dumps(summary, default=str), STATUS_FINISHED)
    if run_id is not None:
        n = _execute(
            """UPDATE platform_certification_runs
               SET finished_at = %s, exit_code = %s, verdict = %s,
                   summary = %s::jsonb, status = %s,
                   heartbeat_at = clock_timestamp()
               WHERE id = %s""", params + (run_id,))
        if n == 1:
            return True
    n = _execute(
        """INSERT INTO platform_certification_runs
           (finished_at, exit_code, verdict, summary, status, started_at,
            run_reason)
           VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)""",
        params + (started_at, run_reason))
    return n == 1


def run_and_record(*, run_reason: str = "scheduled", runner=None,
                   writer=None, now=_utcnow, opener=None) -> dict:
    """Run the certification once and record the verdict. Never raises.

    The row is opened first (``opener``, default :func:`open_run`) and
    finalized by ``writer``; with the default writer the opened row is
    updated in place. A caller-supplied ``writer`` gets the original
    six-argument call.
    """
    started = now()
    run_id = None
    if writer is None:
        try:
            close_interrupted_runs()
        except Exception as exc:  # noqa: BLE001
            log.error("[platform_certification] interrupted-run sweep failed: %s", exc)
        try:
            run_id = (opener or open_run)(started, run_reason=run_reason)
        except Exception as exc:  # noqa: BLE001
            log.error("[platform_certification] could not open the run row: %s", exc)
        if run_id is None:
            log.error("[platform_certification] no running row was written; the "
                      "verdict will be inserted when the run ends")

        def writer(s_, f_, code, verdict, summary, *, run_reason="scheduled"):
            return record(s_, f_, code, verdict, summary, run_reason=run_reason,
                          run_id=run_id)
    if runner is None:
        from scripts import audit_platform_certification as apc  # noqa: PLC0415

        def runner():
            return apc.run(heartbeat=lambda: heartbeat(run_id),
                           on_audit_start=lambda key: heartbeat(run_id, key))
    try:
        report = runner()
        summary = summarize(report)
        exit_code = int(report["exit_code"])
        verdict = str(report["verdict"])
        if exit_code not in (0, 1, 2):
            raise ValueError(f"unrecognised certification exit code {exit_code}")
    except Exception as exc:  # noqa: BLE001
        log.error("[platform_certification] certification did not run: %s",
                  exc, exc_info=True)
        exit_code, verdict = EXIT_UNAVAILABLE, VERDICT_NOT_CERTIFIED
        summary = {"verdict": verdict, "exit_code": exit_code,
                   "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                   "audits": [], "crm_data_to_fix": None}
    finished = now()
    recorded = writer(started, finished, exit_code, verdict, summary,
                      run_reason=run_reason)
    if not recorded:
        log.error("[platform_certification] verdict %s was NOT recorded", verdict)
    return {"verdict": verdict, "exit_code": exit_code, "recorded": recorded,
            "started_at": started.isoformat(), "finished_at": finished.isoformat()}
