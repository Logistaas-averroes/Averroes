"""
services/freshness_service.py

PR-ADS-067 — Canonical Freshness Semantics & Zero-Row Truth States

Provides the canonical freshness status model used across the platform.
Every dataset is assigned one unambiguous truth state so the UI never
displays a generic "Fresh" badge when the underlying data is empty,
stale, failed, or blocked by a dependency.

Phase 1 — Read Only.  No external writes.
"""

from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
from typing import Any


# ── Canonical Status Constants ──────────────────────────────────────────────

class CanonicalFreshnessStatus:
    """Enumeration of all possible canonical freshness states."""

    FRESH_WITH_DATA = "fresh_with_data"
    FRESH_BUT_EMPTY = "fresh_but_empty"
    STALE_WITH_DATA = "stale_with_data"
    STALE_AND_EMPTY = "stale_and_empty"
    FAILED = "failed"
    RUNNING = "running"
    NOT_RUN = "not_run"
    DEPENDENCY_BLOCKED = "dependency_blocked"
    DB_UNAVAILABLE = "db_unavailable"
    UNKNOWN = "unknown"

    ALL = [
        FRESH_WITH_DATA,
        FRESH_BUT_EMPTY,
        STALE_WITH_DATA,
        STALE_AND_EMPTY,
        FAILED,
        RUNNING,
        NOT_RUN,
        DEPENDENCY_BLOCKED,
        DB_UNAVAILABLE,
        UNKNOWN,
    ]


# ── Severity Levels ─────────────────────────────────────────────────────────

SEVERITY_MAP: dict[str, str] = {
    CanonicalFreshnessStatus.FRESH_WITH_DATA: "ok",
    CanonicalFreshnessStatus.FRESH_BUT_EMPTY: "warning",
    CanonicalFreshnessStatus.STALE_WITH_DATA: "warning",
    CanonicalFreshnessStatus.STALE_AND_EMPTY: "error",
    CanonicalFreshnessStatus.FAILED: "error",
    CanonicalFreshnessStatus.RUNNING: "neutral",
    CanonicalFreshnessStatus.NOT_RUN: "neutral",
    CanonicalFreshnessStatus.DEPENDENCY_BLOCKED: "warning",
    CanonicalFreshnessStatus.DB_UNAVAILABLE: "error",
    CanonicalFreshnessStatus.UNKNOWN: "neutral",
}


# ── Dataset Configuration ───────────────────────────────────────────────────

DATASET_FRESHNESS_CONFIG: dict[str, dict[str, Any]] = {
    "campaigns": {
        "table": "campaigns",
        "date_column": "run_date",
        "source": "windsor",
        "dataset": "campaigns",
        "stale_threshold_days": 8,
        "depends_on": [],
        "page": "campaigns",
    },
    "search_terms": {
        "table": "search_terms",
        "date_column": "source_date",
        "source": "windsor",
        "dataset": "search_terms",
        "stale_threshold_days": 8,
        "depends_on": [],
        "page": "search-terms",
    },
    "waste_terms": {
        "table": "waste_terms",
        "date_column": "run_date",
        "source": "analysis",
        "dataset": "waste_terms",
        "stale_threshold_days": 8,
        "depends_on": ["search_terms"],
        "page": "waste",
    },
    "ngrams": {
        "table": "search_terms",
        "date_column": "source_date",
        "source": "computed",
        "dataset": "ngrams",
        "stale_threshold_days": 8,
        "depends_on": ["search_terms"],
        "page": "ngrams",
    },
    "keywords": {
        "table": "keywords",
        "date_column": "run_date",
        "source": "windsor",
        "dataset": "keywords",
        "stale_threshold_days": 8,
        "depends_on": [],
        "page": "keywords",
    },
    "geo": {
        "table": "geo",
        "date_column": "run_date",
        "source": "windsor",
        "dataset": "geo",
        "stale_threshold_days": 8,
        "depends_on": [],
        "page": "geo",
    },
    "leads": {
        "table": "leads",
        "date_column": "created_at",
        "source": "hubspot",
        "dataset": "contacts",
        "stale_threshold_days": 8,
        "depends_on": [],
        "page": "leads",
    },
    "deals": {
        "table": "deals",
        "date_column": "close_date",
        "source": "hubspot",
        "dataset": "deals",
        "stale_threshold_days": 8,
        "depends_on": [],
        "page": "deals",
    },
    "gclid_attribution": {
        "table": "gclid_attribution",
        "date_column": "matched_at",
        "source": "gclid",
        "dataset": "matches",
        "stale_threshold_days": 8,
        "depends_on": [],
        "page": "gclid-attribution",
    },
    "gclid_coverage_snapshots": {
        "table": "gclid_coverage_snapshots",
        "date_column": "snapshot_date",
        "source": "gclid",
        "dataset": "coverage_snapshots",
        "stale_threshold_days": 8,
        "depends_on": [],
        "page": "gclid-attribution",
    },
    "historical_intelligence": {
        "table": "historical_intelligence",
        "date_column": "analysis_date",
        "source": "analysis",
        "dataset": "historical_intelligence",
        "stale_threshold_days": 14,
        "depends_on": [],
        "page": "historical-intelligence",
    },
}

# States that block dependents
BLOCKING_STATES = frozenset([
    CanonicalFreshnessStatus.FRESH_BUT_EMPTY,
    CanonicalFreshnessStatus.FAILED,
    CanonicalFreshnessStatus.DB_UNAVAILABLE,
    CanonicalFreshnessStatus.STALE_AND_EMPTY,
    CanonicalFreshnessStatus.NOT_RUN,
])


# ── Core Computation ────────────────────────────────────────────────────────

def compute_canonical_freshness(
    *,
    dataset: str,
    rows_in_window: int | None,
    latest_source_date: date | None,
    sync_status: str | None,
    latest_batch_status: str | None,
    latest_batch_row_count: int | None,
    last_successful_sync_at: datetime | None,
    stale_threshold_days: int,
    dependency_status: str | None = None,
) -> dict[str, Any]:
    """Compute the canonical freshness verdict for a single dataset.

    Pure function — no DB access, no side effects.

    Returns a dict with:
        canonical_status, severity, reason, next_action
    """

    # 1. Dependency blocked
    if dependency_status and dependency_status in BLOCKING_STATES:
        dep_cfg = DATASET_FRESHNESS_CONFIG.get(dataset, {})
        deps = dep_cfg.get("depends_on", [])
        dep_names = ", ".join(d.replace("_", " ").title() for d in deps) if deps else "upstream dataset"
        return _result(
            CanonicalFreshnessStatus.DEPENDENCY_BLOCKED,
            reason=f"{dataset.replace('_', ' ').title()} depends on {dep_names}. Dependency is {dependency_status.replace('_', ' ')}.",
            next_action=f"Fix {dep_names} first.",
        )

    # 2. DB unavailable (sync_status sentinel)
    if sync_status == "db_unavailable":
        return _result(
            CanonicalFreshnessStatus.DB_UNAVAILABLE,
            reason="Database connection unavailable.",
            next_action="Check database connectivity and restart if needed.",
        )

    # 3. Not run
    if sync_status is None and latest_batch_status is None:
        return _result(
            CanonicalFreshnessStatus.NOT_RUN,
            reason="No sync state or sync batches exist for this dataset.",
            next_action="Trigger a sync via scheduler or manual run.",
        )

    # 4. Running
    if latest_batch_status == "running" or sync_status == "running":
        return _result(
            CanonicalFreshnessStatus.RUNNING,
            reason="Latest sync batch is currently running.",
            next_action="Wait for the current sync to complete.",
        )

    # 5. Failed
    if latest_batch_status == "failed" or sync_status == "failed":
        return _result(
            CanonicalFreshnessStatus.FAILED,
            reason="Latest sync batch or sync state status is failed.",
            next_action="Check error logs and retry sync.",
        )

    # 6. Row count unavailable (cannot safely classify empty vs with_data)
    if rows_in_window is None:
        batch_hint = ""
        if latest_batch_row_count is not None:
            batch_hint = f" Latest batch row count: {latest_batch_row_count}."
        return _result(
            CanonicalFreshnessStatus.UNKNOWN,
            reason=f"Row count is unavailable for the selected window; cannot determine dataset freshness.{batch_hint}",
            next_action="Check dataset row-count query health and retry.",
        )

    # 7. Determine staleness
    is_stale = _is_stale(last_successful_sync_at, latest_source_date, stale_threshold_days)
    has_rows = rows_in_window > 0

    # 8. Classify
    if is_stale:
        if has_rows:
            return _result(
                CanonicalFreshnessStatus.STALE_WITH_DATA,
                reason="Rows exist, but latest source/sync date is older than threshold.",
                next_action="Check scheduler schedule and trigger a fresh sync.",
            )
        else:
            batch_hint = ""
            if latest_batch_row_count is not None:
                batch_hint = f" Latest batch row count: {latest_batch_row_count}."
            return _result(
                CanonicalFreshnessStatus.STALE_AND_EMPTY,
                reason=f"No rows and no recent sync.{batch_hint}",
                next_action="Trigger sync and verify data source is producing rows.",
            )
    else:
        if has_rows:
            return _result(
                CanonicalFreshnessStatus.FRESH_WITH_DATA,
                reason="Data present and recently synced.",
                next_action="No action needed.",
            )
        else:
            batch_hint = ""
            if latest_batch_row_count is not None:
                batch_hint = f" Latest batch row count: {latest_batch_row_count}."
            return _result(
                CanonicalFreshnessStatus.FRESH_BUT_EMPTY,
                reason=f"Latest sync succeeded but no rows exist in the selected window.{batch_hint}",
                next_action="Check source pull, sync batch row count, and pipeline verifier.",
            )


# ── Helpers ─────────────────────────────────────────────────────────────────

def _result(status: str, reason: str, next_action: str) -> dict[str, Any]:
    return {
        "canonical_status": status,
        "severity": SEVERITY_MAP.get(status, "neutral"),
        "reason": reason,
        "next_action": next_action,
    }


def _is_stale(
    last_successful_sync_at: datetime | None,
    latest_source_date: date | None,
    stale_threshold_days: int,
) -> bool:
    """Determine if a dataset is stale based on available timestamps."""
    now = datetime.now(timezone.utc)
    threshold = timedelta(days=stale_threshold_days)

    # Prefer last_successful_sync_at for staleness check
    if last_successful_sync_at:
        if last_successful_sync_at.tzinfo is None:
            last_successful_sync_at = last_successful_sync_at.replace(tzinfo=timezone.utc)
        return (now - last_successful_sync_at) > threshold

    # Fall back to latest_source_date
    if latest_source_date:
        source_dt = datetime.combine(latest_source_date, datetime.min.time(), tzinfo=timezone.utc)
        return (now - source_dt) > threshold

    # No date info — consider stale
    return True


def canonical_status_display_label(status: str) -> str:
    """Return the human-readable label for a canonical status."""
    labels = {
        CanonicalFreshnessStatus.FRESH_WITH_DATA: "Fresh with data",
        CanonicalFreshnessStatus.FRESH_BUT_EMPTY: "Fresh but empty",
        CanonicalFreshnessStatus.STALE_WITH_DATA: "Stale with data",
        CanonicalFreshnessStatus.STALE_AND_EMPTY: "Stale and empty",
        CanonicalFreshnessStatus.FAILED: "Failed",
        CanonicalFreshnessStatus.RUNNING: "Running",
        CanonicalFreshnessStatus.NOT_RUN: "Not run",
        CanonicalFreshnessStatus.DEPENDENCY_BLOCKED: "Blocked by dependency",
        CanonicalFreshnessStatus.DB_UNAVAILABLE: "Database unavailable",
        CanonicalFreshnessStatus.UNKNOWN: "Unknown",
    }
    return labels.get(status, "Unknown")
