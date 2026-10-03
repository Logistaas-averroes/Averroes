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
  yellow — at least one stale run, a partial latest run, or a single failure
  red    — any run type has consecutive_failures >= consecutive_failure_warning

Three outcomes, not two (PR-ADS-160, fourth review)
---------------------------------------------------
A run ends `success`, `partial` or `failed`, and `partial` is genuinely neither
of the others: real work landed, and the run did not finish what it set out to
do. This module used to fold it into `success` for BOTH of its measurements,
which made a pipeline producing nothing but partial runs look perfectly healthy
— green severity, no warning, a freshness clock ticking over on runs that never
completed.

The distinction is kept in two places, and they are different questions:

  consecutive_failures   partial is NOT a failure — it does not accumulate
                         toward the red threshold, and it breaks a failure
                         streak, because something did run and did land.
  last_success_at        partial does NOT advance it. This timestamp is the
                         proven-complete coverage claim that staleness is
                         measured against, and a run that stopped short has
                         proven nothing about coverage.

`last_completed_at` is reported alongside for readers that want "when did this
pipeline last do anything", so dropping partial from `last_success_at` loses no
information — it just stops that information being called success.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Module-level defaults — overridden at call-time by config-loaded values.
STALE_DAYS_DEFAULT: dict[str, int] = {
    "daily":                  2,
    "daily_incremental_sync": 2,
    "weekly":                 8,
    "monthly":                35,
}
CONSECUTIVE_FAILURE_WARNING_DEFAULT = 2


# ── Run type → monitoring cadence (PR-ADS-160-F1, corrected in F2) ──────────
#
# Monitoring reports on CADENCES. Production emits concrete RUN TYPES,
# and they are not always spelled like the cadence they belong to: the
# incremental sync writes ``daily_incremental_sync``
# (``scheduler.incremental_sync.RUN_TYPE``), which is the durable value in the
# `runs` table.
#
# Before this mapping existed, grouping matched the cadence names literally, so
# every `daily_incremental_sync` row was DISCARDED before any of the logic below
# ran. The result was silent and the wrong way round: the daily bucket reported
# "No daily run found in history" — a warning about absence — while real daily
# runs were happening and their partial/failed outcomes were invisible.
#
# The durable run type is NOT renamed to suit this module. This is the one place
# the translation happens, so a reader can see the whole mapping at once instead
# of discovering it as a string comparison somewhere downstream.
#
# PR-ADS-160-F2. F1 stopped discarding the incremental rows but routed them into
# the SAME cadence as the 06:00 legacy pulse, and everything below is computed
# once per cadence: one failure streak, one `last_success_at`, one severity. Two
# unrelated pipelines then voted on one verdict, and the healthier one won:
#
#   * `consecutive_failures` stops at the first success in the bucket, so a
#     pulse succeeding at 06:00 reset the incremental sync's streak every
#     morning. It could never reach the red threshold of 2.
#   * `last_success_at` took the newest success of EITHER pipeline, so a pulse
#     that proves nothing about canonical spend, the contact funnel, the deal
#     ledger, geo or SQL coverage reset the clock their staleness is measured
#     against — the same substitution this module forbids for `partial`.
#
#   Five days of incremental-sync failures behind a healthy pulse reported
#   `severity: green, warnings: []`, and `static/app.js` renders nothing on
#   green. Measured, not reasoned: see `tests/test_pr_ads_160_f2_*`.
#
# So a cadence is now one pipeline's health, not a name several pipelines share.
# Two run types may still map to one cadence — but only where they are the same
# pipeline, because the verdict they receive is indivisible.
MONITORING_CADENCES: tuple[str, ...] = (
    "daily", "daily_incremental_sync", "weekly", "monthly",
)

#: Concrete run type → cadence. Add a row here when a new scheduler starts
#: writing to `runs`; that is a deliberate decision about whose health it
#: reports, which is exactly why it is not inferred from the name.
RUN_TYPE_CADENCE: dict[str, str] = {
    "daily":                  "daily",
    "daily_incremental_sync": "daily_incremental_sync",
    "weekly":                 "weekly",
    "monthly":                "monthly",
}

#: Cadence → the words a human reads in a warning. `str.capitalize()` was fine
#: while every cadence was one lowercase word; it renders the incremental
#: cadence as "Daily_incremental_sync".
CADENCE_LABELS: dict[str, str] = {
    "daily":                  "Daily",
    "daily_incremental_sync": "Daily incremental sync",
    "weekly":                 "Weekly",
    "monthly":                "Monthly",
}


def cadence_label(cadence: str) -> str:
    """The human label for a cadence, for warning text only."""
    return CADENCE_LABELS.get(cadence, cadence.replace("_", " ").capitalize())


def monitoring_cadence(run_type: str | None) -> str | None:
    """The monitoring cadence a concrete run type belongs to, or ``None``.

    ``None`` means "not monitored" and the run is skipped. An unknown run type
    is deliberately NOT folded into ``daily``: a new scheduler would then start
    voting on the daily cadence's severity — and could turn it red — without
    anyone having decided that its health belongs there. Unknown is not daily,
    the same way unknown is not zero anywhere else in this codebase.
    """
    if not run_type:
        return None
    return RUN_TYPE_CADENCE.get(str(run_type).strip().lower())


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

    # Group runs by CADENCE, preserving DESC order (newest first per cadence).
    # `monitoring_cadence` is the only translation from the durable run type;
    # a run type it does not recognise is skipped rather than assumed daily.
    by_type: dict[str, list[dict]] = {c: [] for c in MONITORING_CADENCES}
    for r in runs:
        cadence = monitoring_cadence(r.get("run_type"))
        if cadence is not None:
            by_type[cadence].append(r)

    latest_runs: dict[str, Any] = {}
    warnings: list[str] = []

    for run_type, type_runs in by_type.items():
        label = cadence_label(run_type)
        # A cadence with no configured threshold is a decision nobody made.
        # Say so rather than silently applying a 2-day default that happens to
        # suit `daily`; the cadence still reports, it just cannot go stale on
        # an invented number.
        threshold_days = stale_after_days.get(
            run_type, STALE_DAYS_DEFAULT.get(run_type))

        if not type_runs:
            latest_runs[run_type] = {
                "last_success_at": None,
                "last_completed_at": None,
                "last_status": None,
                "latest_partial": False,
                "consecutive_failures": 0,
                "stale": True,
            }
            warnings.append(f"No {label.lower()} run found in history.")
            continue

        # Count consecutive failures from the top (newest first). A partial run
        # BREAKS the streak: it is not a failure, and letting it accumulate
        # toward the red threshold would report an outage where there is none.
        consecutive_failures = 0
        for r in type_runs:
            if (r.get("status") or "").lower() not in ("success", "partial"):
                consecutive_failures += 1
            else:
                break

        # The proven-complete coverage claim. `success` ONLY — a partial run
        # stopped short, so it proves nothing about coverage and must not reset
        # the freshness clock. This is what staleness is measured against.
        last_success_at: str | None = None
        for r in type_runs:
            if (r.get("status") or "").lower() == "success":
                last_success_at = r.get("finished_at") or r.get("started_at")
                break

        # "When did this pipeline last do anything" — a different question, and
        # reported separately so excluding partial above hides nothing.
        last_completed_at: str | None = None
        for r in type_runs:
            if (r.get("status") or "").lower() in ("success", "partial"):
                last_completed_at = r.get("finished_at") or r.get("started_at")
                break

        latest_status = (type_runs[0].get("status") or "unknown").lower()
        latest_partial = latest_status == "partial"

        # Compute stale: no successful run within the threshold window.
        stale = True
        if threshold_days is None:
            # Unmeasurable, not stale: no threshold was ever chosen for this
            # cadence. Unknown is not a failure, the same way unknown is not
            # zero, so it warns rather than reporting a staleness verdict.
            stale = False
            warnings.append(
                f"{label} run has no staleness threshold configured — its "
                f"freshness is not being checked.")
        elif last_success_at:
            try:
                ts_str = last_success_at.rstrip("Z")
                ts = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
                age_days = (now - ts).total_seconds() / 86400.0
                stale = age_days > threshold_days
            except (ValueError, OverflowError):
                stale = True

        latest_runs[run_type] = {
            "last_success_at": last_success_at,
            "last_completed_at": last_completed_at,
            "last_status": latest_status,
            "latest_partial": latest_partial,
            "consecutive_failures": consecutive_failures,
            "stale": stale,
        }

        if consecutive_failures >= consecutive_failure_warning:
            warnings.append(
                f"{label} run has failed "
                f"{consecutive_failures} time{'s' if consecutive_failures != 1 else ''} in a row."
            )
        elif latest_partial:
            # Said plainly, and said even when nothing is stale yet. A partial
            # run is the one outcome an operator can miss entirely: it leaves
            # fresh-looking data behind, so nothing else on the page complains.
            warnings.append(
                f"{label} run completed partially — some "
                f"datasets were incomplete."
            )
        elif stale:
            warnings.append(f"{label} run data is stale.")

    # Determine overall severity.
    any_repeated_failure = any(
        v["consecutive_failures"] >= consecutive_failure_warning
        for v in latest_runs.values()
        if v.get("consecutive_failures") is not None
    )
    any_stale = any(v.get("stale") for v in latest_runs.values())
    any_partial = any(v.get("latest_partial") for v in latest_runs.values())

    if any_repeated_failure:
        severity = "red"
    elif any_stale or any_partial:
        # Partial is deliberately NOT green. It is also deliberately not red:
        # real work landed and the pipeline is not down. Yellow is the only
        # honest answer — something needs looking at, nothing is on fire.
        severity = "yellow"
    else:
        severity = "green"

    return {
        "status": "ok",
        "severity": severity,
        "latest_runs": latest_runs,
        "warnings": warnings,
    }
