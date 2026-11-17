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
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

VERDICT_NOT_CERTIFIED = "NOT CERTIFIED"
EXIT_UNAVAILABLE = 2


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def summarize(report: dict) -> dict:
    """The stored summary: each audit's outcome and the CRM data to fix.

    The per-audit raw reports are dropped — they are large, and the verdict,
    violations and unavailable reasons are what a reader needs.
    """
    audits = []
    for a in report.get("audits") or []:
        audits.append({k: a.get(k) for k in (
            "key", "module", "outcome", "exit_code", "seconds", "violations",
            "unavailable", "detail")})
    return {"verdict": report.get("verdict"),
            "exit_code": report.get("exit_code"),
            "generated_at": report.get("generated_at"),
            "audits": audits,
            "crm_data_to_fix": report.get("crm_data_to_fix")}


def record(started_at, finished_at, exit_code: int, verdict: str,
           summary: dict, *, run_reason: str = "scheduled") -> bool:
    """Insert one verdict row. Returns False when the write did not happen."""
    from db.connection import get_conn  # noqa: PLC0415
    try:
        with get_conn() as conn:
            if conn is None:
                return False
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO platform_certification_runs
                       (started_at, finished_at, exit_code, verdict,
                        run_reason, summary)
                       VALUES (%s, %s, %s, %s, %s, %s::jsonb)""",
                    (started_at, finished_at, int(exit_code), verdict,
                     run_reason, json.dumps(summary, default=str)))
                return cur.rowcount == 1
    except Exception as exc:  # noqa: BLE001
        log.error("[platform_certification] could not record verdict: %s", exc)
        return False


def run_and_record(*, run_reason: str = "scheduled", runner=None,
                   writer=None, now=_utcnow) -> dict:
    """Run the certification once and record the verdict. Never raises."""
    if runner is None:
        from scripts import audit_platform_certification as apc  # noqa: PLC0415
        runner = apc.run
    writer = writer or record
    started = now()
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
