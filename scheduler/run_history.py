"""
scheduler/run_history.py

Persistent run history for Logistaas scheduler executions.

Responsibility: write one JSONL record per scheduler run to runtime_logs/.
No business logic. No analysis. No API calls.

Record schema:
  run_type            — "daily" | "weekly" | "monthly"
  started_at          — ISO-8601 UTC timestamp
  finished_at         — ISO-8601 UTC timestamp (null until run completes)
  status              — "success" | "failed" | "partial"
  failed_step         — step label if the run failed, else null
  error_message       — exception message if the run failed, else null
  report_path         — path to generated report file, else null
  delivery_attempted  — true | false
  delivery_success    — true | false | null (null = not attempted)

Failure behaviour:
  All write errors are logged to stderr but never suppress the original
  scheduler exception.  The caller is always responsible for re-raising.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

_LOG_DIR = "runtime_logs"
_LOG_FILE = os.path.join(_LOG_DIR, "run_history.jsonl")


def _utc_now() -> str:
    """Return current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def start_run(run_type: str) -> dict:
    """
    Build and return an in-memory run record at the start of a scheduler run.

    The record is NOT written to disk here — call write_run_record() when the
    run finishes so that the final status, finished_at, and error details are
    included in the single persisted record.

    Args:
        run_type: One of "daily", "weekly", "monthly".

    Returns:
        A mutable dict representing the run record.
    """
    return {
        "run_type": run_type,
        "started_at": _utc_now(),
        "finished_at": None,
        "status": "failed",          # pessimistic default; updated on success
        "failed_step": None,
        "error_message": None,
        "report_path": None,
        "delivery_attempted": False,
        "delivery_success": None,
    }


def write_run_record(record: dict) -> None:
    """
    Append the completed run record to runtime_logs/run_history.jsonl.

    Creates the runtime_logs/ directory if it does not exist.  All I/O errors
    are logged and silently swallowed — this function must never suppress a
    scheduler error that occurred upstream.

    Args:
        record: A run record dict as returned by start_run() and subsequently
                mutated with finished_at, status, etc.
    """
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        with open(_LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        log.info(
            "[RUN_HISTORY] Record written — run_type=%s status=%s started_at=%s",
            record.get("run_type"),
            record.get("status"),
            record.get("started_at"),
        )
    except OSError as exc:
        log.error("[RUN_HISTORY] Could not write run record: %s", exc)


def finish_run(
    record: dict,
    *,
    status: str,
    report_path: Optional[str] = None,
    delivery_attempted: bool = False,
    delivery_success: Optional[bool] = None,
    failed_step: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    """
    Finalise the run record in-memory and persist it to disk.

    Args:
        record:             The dict returned by start_run().
        status:             "success" | "failed" | "partial"
        report_path:        Path to the saved report file, or None.
        delivery_attempted: Whether delivery was attempted.
        delivery_success:   True/False/None (None = not attempted).
        failed_step:        Step label that raised the error, or None.
        error_message:      Exception string, or None.
    """
    record["finished_at"] = _utc_now()
    record["status"] = status
    record["report_path"] = report_path
    record["delivery_attempted"] = delivery_attempted
    record["delivery_success"] = delivery_success
    record["failed_step"] = failed_step
    record["error_message"] = error_message
    write_run_record(record)
