"""
api/monitoring.py

Read-only monitoring status computation for PR-ADS-069.

Responsibility: compute per-run-type monitoring state (consecutive failures,
staleness, warnings) from a list of run records.  No FastAPI imports.  No DB
access.  No external calls.  Pure function — safe to import in tests without
the web-framework stack.

Stale thresholds (configurable via config/thresholds.yaml ui.monitoring):
  daily:   stale after > 2 days without a successful run
  weekly:  stale after > 8 days without a successful run
  monthly: stale after > 35 days without a successful run

Severity ladder:
  green  — no warnings
  yellow — at least one stale run or a single failure
  red    — any run type has consecutive_failures >= consecutive_failure_warning
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Module-level defaults — overridden at call-time by config-loaded values.
STALE_DAYS_DEFAULT: dict[str, int] = {
    "daily":   2,
    "weekly":  8,
    "monthly": 35,
}
CONSECUTIVE_FAILURE_WARNING_DEFAULT = 2


def compute_monitoring_status(
    runs: list[dict],
    stale_after_days: dict[str, int],
    consecutive_failure_warning: int,
) -> dict[str, Any]:
    """Compute per-run-type monitoring state from a list of run records.

    Args:
        runs: Run records ordered by started_at DESC (newest first).
              Each record must have: run_type, status, started_at, finished_at.
        stale_after_days: Mapping of run_type → threshold in days.
        consecutive_failure_warning: Number of consecutive failures that
            triggers a warning (and sets severity to red).

    Returns:
        Monitoring summary dict::

            {
                "status":      "ok",
                "severity":    "green" | "yellow" | "red",
                "latest_runs": {
                    "daily":   { "last_success_at", "last_status",
                                 "consecutive_failures", "stale" },
                    "weekly":  { ... },
                    "monthly": { ... },
                },
                "warnings": ["..."],
            }

    This function is read-only — it performs no mutations and has no side
    effects.  All errors in individual records are handled gracefully.
    """
    now = datetime.now(timezone.utc)

    # Group runs by run_type, preserving DESC order (newest first per type).
    by_type: dict[str, list[dict]] = {"daily": [], "weekly": [], "monthly": []}
    for r in runs:
        rt = r.get("run_type")
        if rt in by_type:
            by_type[rt].append(r)

    latest_runs: dict[str, Any] = {}
    warnings: list[str] = []

    for run_type, type_runs in by_type.items():
        threshold_days = stale_after_days.get(run_type, STALE_DAYS_DEFAULT.get(run_type, 2))

        if not type_runs:
            latest_runs[run_type] = {
                "last_success_at": None,
                "last_status": None,
                "consecutive_failures": 0,
                "stale": True,
            }
            warnings.append(f"No {run_type} run found in history.")
            continue

        # Count consecutive failures from the top (newest first).
        consecutive_failures = 0
        for r in type_runs:
            if (r.get("status") or "").lower() not in ("success", "partial"):
                consecutive_failures += 1
            else:
                break

        # Find last successful run timestamp.
        last_success_at: str | None = None
        for r in type_runs:
            if (r.get("status") or "").lower() in ("success", "partial"):
                last_success_at = r.get("finished_at") or r.get("started_at")
                break

        latest_status = (type_runs[0].get("status") or "unknown").lower()

        # Compute stale: no successful run within the threshold window.
        stale = True
        if last_success_at:
            try:
                ts_str = last_success_at.rstrip("Z")
                ts = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
                age_days = (now - ts).total_seconds() / 86400.0
                stale = age_days > threshold_days
            except (ValueError, OverflowError):
                stale = True

        latest_runs[run_type] = {
            "last_success_at": last_success_at,
            "last_status": latest_status,
            "consecutive_failures": consecutive_failures,
            "stale": stale,
        }

        if consecutive_failures >= consecutive_failure_warning:
            warnings.append(
                f"{run_type.capitalize()} run has failed "
                f"{consecutive_failures} time{'s' if consecutive_failures != 1 else ''} in a row."
            )
        elif stale:
            warnings.append(f"{run_type.capitalize()} run data is stale.")

    # Determine overall severity.
    any_repeated_failure = any(
        v["consecutive_failures"] >= consecutive_failure_warning
        for v in latest_runs.values()
        if v.get("consecutive_failures") is not None
    )
    any_stale = any(v.get("stale") for v in latest_runs.values())

    if any_repeated_failure:
        severity = "red"
    elif any_stale or warnings:
        severity = "yellow"
    else:
        severity = "green"

    return {
        "status": "ok",
        "severity": severity,
        "latest_runs": latest_runs,
        "warnings": warnings,
    }
