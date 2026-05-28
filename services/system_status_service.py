"""
services/system_status_service.py

PR-ADS-068 — System Status War Room & Pipeline Dependency Map
PR-ADS-095 — System Status Truth & Window/Data Diagnostics

Provides consolidated system status logic combining canonical freshness,
pipeline dependencies, source health, scheduler state, and blockers.

PR-ADS-095 fixes over-blocking: a failed sync with usable rows is degraded,
not fatal. Derived pipelines with fresh upstream are action_needed, not blocked.

Phase 1 — Read Only. No external writes. No scheduler triggers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from services.freshness_service import (
    ACTION_NEEDED_STATES,
    BLOCKING_STATES,
    DEGRADED_STATES,
    CanonicalFreshnessStatus,
    SEVERITY_MAP,
)


# ── Pipeline Dependency Map ─────────────────────────────────────────────────

PIPELINE_DEPENDENCIES: dict[str, dict[str, Any]] = {
    "campaigns": {
        "label": "Campaigns",
        "source": "windsor",
        "page": "Campaigns",
        "depends_on": [],
        "blocks": [],
    },
    "search_terms": {
        "label": "Search Terms",
        "source": "windsor",
        "page": "Search Terms",
        "depends_on": [],
        "blocks": ["waste_terms", "ngrams"],
    },
    "waste_terms": {
        "label": "Waste Terms",
        "source": "analysis",
        "page": "Waste Terms",
        "depends_on": ["search_terms"],
        "blocks": [],
    },
    "ngrams": {
        "label": "N-Grams",
        "source": "computed",
        "page": "N-Grams",
        "depends_on": ["search_terms"],
        "blocks": [],
    },
    "keywords": {
        "label": "Keywords",
        "source": "windsor",
        "page": "Keywords",
        "depends_on": [],
        "blocks": [],
    },
    "geo": {
        "label": "Geo",
        "source": "windsor",
        "page": "Geo",
        "depends_on": [],
        "blocks": [],
    },
    "leads": {
        "label": "Leads",
        "source": "hubspot",
        "page": "Lead Quality",
        "depends_on": [],
        "blocks": [],
    },
    "deals": {
        "label": "Deals",
        "source": "hubspot",
        "page": "Deals",
        "depends_on": [],
        "blocks": [],
    },
    "gclid_attribution": {
        "label": "GCLID Attribution",
        "source": "gclid",
        "page": "GCLID Attribution",
        "depends_on": [],
        "blocks": [],
    },
    "gclid_coverage_snapshots": {
        "label": "GCLID Coverage",
        "source": "gclid",
        "page": "GCLID Attribution",
        "depends_on": [],
        "blocks": [],
    },
    "historical_intelligence": {
        "label": "Historical Intelligence",
        "source": "analysis",
        "page": "Historical Intelligence",
        "depends_on": [],
        "blocks": [],
    },
}

# ── Source Definitions ──────────────────────────────────────────────────────

SOURCE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "windsor": {
        "label": "Windsor / Google Ads",
        "datasets": ["campaigns", "search_terms", "keywords", "geo"],
    },
    "hubspot": {
        "label": "HubSpot CRM",
        "datasets": ["leads", "deals"],
    },
    "gclid": {
        "label": "GCLID Match",
        "datasets": ["gclid_attribution", "gclid_coverage_snapshots"],
    },
    "analysis": {
        "label": "Analysis Layer",
        "datasets": ["waste_terms", "historical_intelligence"],
    },
    "computed": {
        "label": "Computed Layer",
        "datasets": ["ngrams"],
    },
}

# ── Page Impact Mapping ─────────────────────────────────────────────────────

PAGE_PIPELINE_IMPACT: dict[str, list[str]] = {
    "dashboard": ["campaigns", "leads", "waste_terms"],
    "campaigns": ["campaigns"],
    "waste": ["waste_terms", "search_terms"],
    "search-terms": ["search_terms"],
    "ngrams": ["ngrams", "search_terms"],
    "geo": ["geo"],
    "keywords": ["keywords"],
    "leads": ["leads"],
    "deals": ["deals"],
    "gclid-attribution": ["gclid_attribution", "gclid_coverage_snapshots"],
    "opportunities": ["leads"],
    "health": ["all"],
    "backfill": ["admin"],
    "historical-intelligence": ["historical_intelligence"],
}

# ── Core datasets vs derived ───────────────────────────────────────────────

CORE_DATASETS = frozenset([
    "campaigns", "search_terms", "leads", "deals", "keywords", "geo",
])

DERIVED_DATASETS = frozenset([
    "waste_terms", "ngrams", "gclid_attribution",
    "gclid_coverage_snapshots", "historical_intelligence",
])


# ── Overall Status Logic ───────────────────────────────────────────────────

def compute_overall_status(dataset_statuses: dict[str, str]) -> tuple[str, str]:
    """Compute overall system status from dataset canonical statuses.

    PR-ADS-095: A core dataset with data_available_latest_sync_failed is
    degraded/warning, not a critical error. Only failed_no_data or db_unavailable
    in core datasets triggers overall error.

    Returns (status, label) where status is one of: ok, warning, error, neutral.
    """
    if not dataset_statuses:
        return "neutral", "No dataset status data available"

    has_error = False
    has_warning = False
    has_ok = False

    for ds_key, canonical_status in dataset_statuses.items():
        severity = SEVERITY_MAP.get(canonical_status, "neutral")
        if severity == "error":
            has_error = True
        elif severity == "warning":
            has_warning = True
        elif severity == "ok":
            has_ok = True

    # Check specifically for truly-blocking states in core datasets
    for ds_key in CORE_DATASETS:
        status = dataset_statuses.get(ds_key)
        if status in (
            CanonicalFreshnessStatus.DB_UNAVAILABLE,
            CanonicalFreshnessStatus.FAILED_NO_DATA,
        ):
            return "error", "Critical system failure — core dataset unavailable or has no data"
        # Legacy FAILED state (without row-count info) still counts as error
        if status == CanonicalFreshnessStatus.FAILED:
            return "error", "Critical system failure — core dataset sync failed"

    # data_available_latest_sync_failed in core is warning, not error
    if has_error:
        # Check if errors are only from non-core or from blocked_by_dependency
        core_errors = [
            ds for ds in CORE_DATASETS
            if SEVERITY_MAP.get(dataset_statuses.get(ds, ""), "neutral") == "error"
        ]
        if core_errors:
            return "error", "System has critical failures"
        # Errors only in derived datasets — warning, not system-wide error
        has_warning = True

    if has_warning:
        # Determine a more specific label
        degraded_datasets = [
            k for k, v in dataset_statuses.items()
            if v == CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED
        ]
        blocked_datasets = [
            k for k, v in dataset_statuses.items()
            if v == CanonicalFreshnessStatus.DEPENDENCY_BLOCKED
        ]
        empty_datasets = [
            k for k, v in dataset_statuses.items()
            if v == CanonicalFreshnessStatus.FRESH_BUT_EMPTY
        ]
        stale_datasets = [
            k for k, v in dataset_statuses.items()
            if v == CanonicalFreshnessStatus.STALE_WITH_DATA
        ]
        derivable_datasets = [
            k for k, v in dataset_statuses.items()
            if v == CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE
        ]

        parts = []
        if degraded_datasets:
            names = ", ".join(d.replace("_", " ").title() for d in degraded_datasets[:2])
            parts.append(f"{names} degraded (sync failed, data usable)")
        if empty_datasets:
            names = ", ".join(d.replace("_", " ").title() for d in empty_datasets[:2])
            parts.append(f"{names} empty")
        if blocked_datasets:
            parts.append(f"{len(blocked_datasets)} blocked")
        if stale_datasets:
            parts.append(f"{len(stale_datasets)} stale")
        if derivable_datasets:
            parts.append(f"{len(derivable_datasets)} derivable but not yet run")

        label = "System usable with issues"
        if parts:
            label = f"System usable — {'; '.join(parts)}"
        return "warning", label

    if has_ok:
        return "ok", "All systems operational"

    return "neutral", "Not enough data to determine system status"


# ── Summary Counts ─────────────────────────────────────────────────────────

def compute_summary_counts(dataset_statuses: dict[str, str]) -> dict[str, int]:
    """Count datasets by severity category."""
    counts = {"ok": 0, "warning": 0, "error": 0, "neutral": 0, "blocked": 0}
    for canonical_status in dataset_statuses.values():
        if canonical_status == CanonicalFreshnessStatus.DEPENDENCY_BLOCKED:
            counts["blocked"] += 1
        else:
            severity = SEVERITY_MAP.get(canonical_status, "neutral")
            counts[severity] = counts.get(severity, 0) + 1
    return counts


# ── Critical Blockers ──────────────────────────────────────────────────────

def compute_critical_blockers(
    dataset_statuses: dict[str, str],
) -> list[dict[str, Any]]:
    """Generate critical blocker objects from canonical statuses.

    PR-ADS-095: data_available_latest_sync_failed is NOT a blocker — it's degraded.
    Only failed_no_data and db_unavailable are true blockers.
    not_run_but_derivable is action_needed, not a blocker.
    """
    blockers: list[dict[str, Any]] = []

    # DB unavailable
    for ds_key, status in dataset_statuses.items():
        if status == CanonicalFreshnessStatus.DB_UNAVAILABLE:
            blockers.append({
                "id": "db_unavailable",
                "severity": "error",
                "title": "Database unavailable",
                "affected_pages": ["All pages"],
                "reason": "Database connection is unavailable.",
                "next_action": "Fix DB connection before investigating data.",
            })
            break  # Only one DB blocker needed

    # Failed datasets with NO data (true blockers)
    for ds_key, status in dataset_statuses.items():
        if status in (CanonicalFreshnessStatus.FAILED_NO_DATA, CanonicalFreshnessStatus.FAILED):
            pipeline = PIPELINE_DEPENDENCIES.get(ds_key, {})
            page = pipeline.get("page", ds_key.replace("_", " ").title())
            blockers.append({
                "id": f"{ds_key}_failed",
                "severity": "error",
                "title": f"{ds_key.replace('_', ' ').title()} sync failed — no usable data",
                "affected_pages": [page],
                "reason": f"{ds_key.replace('_', ' ').title()} latest sync failed and no usable rows are available.",
                "next_action": "Check sync_batches and scheduler logs.",
            })

    # Degraded datasets (warning, not blockers — data IS available)
    for ds_key, status in dataset_statuses.items():
        if status == CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED:
            pipeline = PIPELINE_DEPENDENCIES.get(ds_key, {})
            page = pipeline.get("page", ds_key.replace("_", " ").title())
            blockers.append({
                "id": f"{ds_key}_degraded",
                "severity": "warning",
                "title": f"{ds_key.replace('_', ' ').title()} sync failed — data still usable",
                "affected_pages": [page],
                "reason": f"{ds_key.replace('_', ' ').title()} latest sync failed, but usable rows exist. Page is degraded, not blocked.",
                "next_action": "Review latest sync error. Page can still render using existing data.",
            })

    # Derivable datasets (action_needed, not blockers)
    for ds_key, status in dataset_statuses.items():
        if status == CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE:
            pipeline = PIPELINE_DEPENDENCIES.get(ds_key, {})
            page = pipeline.get("page", ds_key.replace("_", " ").title())
            depends_on = pipeline.get("depends_on", [])
            dep_names = ", ".join(d.replace("_", " ").title() for d in depends_on)
            blockers.append({
                "id": f"{ds_key}_derivable",
                "severity": "warning",
                "title": f"{ds_key.replace('_', ' ').title()} not run — derivable from {dep_names}",
                "affected_pages": [page],
                "reason": f"Upstream data exists ({dep_names}), but derived analysis has not been generated.",
                "next_action": "Run derived analysis from existing upstream data.",
            })

    # Search Terms empty (special case — blocks Waste Terms and N-Grams)
    st_status = dataset_statuses.get("search_terms")
    if st_status == CanonicalFreshnessStatus.FRESH_BUT_EMPTY:
        blockers.append({
            "id": "search_terms_empty",
            "severity": "warning",
            "title": "Search Terms has no usable rows",
            "affected_pages": ["Search Terms", "Waste Terms", "N-Grams"],
            "reason": "Search Terms is fresh_but_empty.",
            "next_action": "Check Search Terms verdict and Windsor REST/MCP parity.",
        })

    # Dependency blocked datasets
    for ds_key, status in dataset_statuses.items():
        if status == CanonicalFreshnessStatus.DEPENDENCY_BLOCKED:
            pipeline = PIPELINE_DEPENDENCIES.get(ds_key, {})
            depends_on = pipeline.get("depends_on", [])
            dep_names = ", ".join(d.replace("_", " ").title() for d in depends_on)
            page = pipeline.get("page", ds_key.replace("_", " ").title())
            blockers.append({
                "id": f"{ds_key}_blocked",
                "severity": "warning",
                "title": f"{ds_key.replace('_', ' ').title()} blocked by {dep_names}",
                "affected_pages": [page],
                "reason": f"{ds_key.replace('_', ' ').title()} depends on {dep_names}.",
                "next_action": f"Fix {dep_names} first.",
            })

    # Stale with data for core datasets
    for ds_key in CORE_DATASETS:
        status = dataset_statuses.get(ds_key)
        if status == CanonicalFreshnessStatus.STALE_WITH_DATA:
            pipeline = PIPELINE_DEPENDENCIES.get(ds_key, {})
            page = pipeline.get("page", ds_key.replace("_", " ").title())
            blockers.append({
                "id": f"{ds_key}_stale",
                "severity": "warning",
                "title": f"{ds_key.replace('_', ' ').title()} data is stale",
                "affected_pages": [page],
                "reason": f"{ds_key.replace('_', ' ').title()} has rows but sync is older than threshold.",
                "next_action": "Check scheduler schedule and trigger a fresh sync.",
            })

    return blockers


# ── Source Health ──────────────────────────────────────────────────────────

def compute_source_health(
    dataset_statuses: dict[str, str],
    sync_info: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build source health cards from dataset statuses and sync info.

    PR-ADS-095: Source rollup uses refined logic:
    - If any child is failed_no_data → source error
    - Else if any child is data_available_latest_sync_failed/stale_with_data → source warning
    - Else if all required children fresh_with_data → source ok
    - Else neutral
    """
    sources: list[dict[str, Any]] = []

    for source_key, source_def in SOURCE_DEFINITIONS.items():
        ds_keys = source_def["datasets"]

        # PR-ADS-095: Refined source rollup
        has_failed_no_data = False
        has_degraded = False
        has_ok = False

        for dk in ds_keys:
            cs = dataset_statuses.get(dk, CanonicalFreshnessStatus.UNKNOWN)
            if cs in (CanonicalFreshnessStatus.FAILED_NO_DATA,
                      CanonicalFreshnessStatus.FAILED,
                      CanonicalFreshnessStatus.DB_UNAVAILABLE,
                      CanonicalFreshnessStatus.BLOCKED_BY_DEPENDENCY):
                has_failed_no_data = True
            elif cs in (CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
                        CanonicalFreshnessStatus.STALE_WITH_DATA,
                        CanonicalFreshnessStatus.FRESH_BUT_EMPTY,
                        CanonicalFreshnessStatus.DEPENDENCY_BLOCKED,
                        CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE):
                has_degraded = True
            elif cs == CanonicalFreshnessStatus.FRESH_WITH_DATA:
                has_ok = True

        if has_failed_no_data:
            source_status = "error"
        elif has_degraded:
            source_status = "warning"
        elif has_ok:
            source_status = "ok"
        else:
            source_status = "neutral"

        # Sync info lookup
        si = (sync_info or {}).get(source_key, {})

        sources.append({
            "source": source_key,
            "label": source_def["label"],
            "status": source_status,
            "datasets": ds_keys,
            "last_successful_sync_at": si.get("last_successful_sync_at"),
            "latest_batch_status": si.get("latest_batch_status"),
            "next_action": _source_next_action(source_status, source_key, dataset_statuses),
        })

    return sources


def _source_next_action(
    source_status: str, source_key: str, dataset_statuses: dict[str, str]
) -> str:
    """Determine next action for a source.

    PR-ADS-095: Provide specific, actionable messages for degraded sources.
    """
    if source_status == "ok":
        return "No action needed."
    if source_status == "error":
        return "Check sync logs and database connectivity."

    # Warning status — check for degraded datasets
    if source_key == "windsor":
        degraded = [
            dk for dk in SOURCE_DEFINITIONS["windsor"]["datasets"]
            if dataset_statuses.get(dk) == CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED
        ]
        if degraded:
            names = ", ".join(d.replace("_", " ").title() for d in degraded)
            return (
                f"Windsor has usable data, but latest sync failed for {names}. "
                "Review sync errors; existing pages may be degraded but not fully blocked."
            )
        st = dataset_statuses.get("search_terms")
        if st == CanonicalFreshnessStatus.FRESH_BUT_EMPTY:
            return "Check Search Terms if empty."
        return "Check Windsor sync status."
    if source_key == "hubspot":
        return "Check HubSpot connector logs."
    if source_key == "gclid":
        return "Check GCLID matching pipeline."
    if source_key == "analysis":
        derivable = [
            dk for dk in SOURCE_DEFINITIONS["analysis"]["datasets"]
            if dataset_statuses.get(dk) == CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE
        ]
        if derivable:
            return "Derived analysis can be generated from existing upstream data."
        return "Check analysis pipeline status."
    if source_key == "computed":
        return "Check computed pipeline status."
    return "Check pipeline status."


# ── Pipeline Details ───────────────────────────────────────────────────────

def compute_pipelines(
    dataset_statuses: dict[str, str],
    dataset_details: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build pipeline detail rows from statuses and dataset details."""
    pipelines: list[dict[str, Any]] = []
    details = dataset_details or {}

    for ds_key, pipe_def in PIPELINE_DEPENDENCIES.items():
        canonical_status = dataset_statuses.get(ds_key, CanonicalFreshnessStatus.UNKNOWN)
        severity = SEVERITY_MAP.get(canonical_status, "neutral")
        ds_detail = details.get(ds_key, {})

        pipelines.append({
            "key": ds_key,
            "label": f"{pipe_def['label']} Pipeline",
            "source": pipe_def["source"],
            "page": pipe_def["page"],
            "canonical_status": canonical_status,
            "severity": severity,
            "rows_in_window": ds_detail.get("rows_in_window"),
            "latest_source_date": ds_detail.get("latest_source_date"),
            "last_batch_row_count": ds_detail.get("last_batch_row_count"),
            "depends_on": pipe_def["depends_on"],
            "blocks": pipe_def["blocks"],
            "reason": ds_detail.get("reason", ""),
            "next_action": ds_detail.get("next_action", ""),
        })

    return pipelines


# ── Scheduler Summary ──────────────────────────────────────────────────────

def compute_scheduler_summary(
    runs_data: dict[str, Any] | None,
    has_source_sync_timestamps: bool = False,
) -> dict[str, Any]:
    """Build scheduler latest-run summary from runs table data.

    PR-ADS-095: If all scheduler runs are null but source sync timestamps
    exist, add diagnostic explanation instead of silent nulls.

    runs_data should be a dict with keys: daily, weekly, monthly, incremental
    each containing {status, started_at, finished_at} or None.
    """
    base = {
        "latest_daily": None,
        "latest_weekly": None,
        "latest_monthly": None,
        "latest_incremental": None,
    }

    if runs_data:
        base["latest_daily"] = runs_data.get("daily")
        base["latest_weekly"] = runs_data.get("weekly")
        base["latest_monthly"] = runs_data.get("monthly")
        base["latest_incremental"] = runs_data.get("incremental")

    # PR-ADS-095: Add diagnostics when all runs are null
    all_null = all(v is None for k, v in base.items() if k.startswith("latest_"))
    if all_null and has_source_sync_timestamps:
        base["diagnostic_status"] = "no_scheduler_run_recorded"
        base["message"] = (
            "Source sync timestamps exist, but no scheduler run records were found."
        )
        base["next_action"] = (
            "Check whether background/manual syncs write scheduler metadata."
        )
    elif all_null:
        base["diagnostic_status"] = "no_runs_found"
        base["message"] = "No scheduler run records found."
        base["next_action"] = "Verify scheduler is configured and has been triggered."

    return base


# ── Page Impact ────────────────────────────────────────────────────────────

def compute_page_impact(dataset_statuses: dict[str, str]) -> list[dict[str, Any]]:
    """Determine page impact from dataset issues.

    PR-ADS-095: Pages are no longer universally "blocked". New statuses:
    - blocked: Primary required data is truly unavailable (failed_no_data, db_unavailable)
    - degraded: Data exists but latest sync failed or data is stale
    - action_needed: Derived dataset not run but derivable from upstream
    - ok: No issues

    A page is only blocked when its primary required data has no usable rows.
    """
    impacts: list[dict[str, Any]] = []

    for page, deps in PAGE_PIPELINE_IMPACT.items():
        if "all" in deps or "admin" in deps:
            continue

        # Check for truly-blocked datasets (no data available)
        blocked_by = None
        degraded_by = None
        action_needed_by = None

        for d in deps:
            status = dataset_statuses.get(d)
            if status is None:
                continue

            if status in (
                CanonicalFreshnessStatus.FAILED_NO_DATA,
                CanonicalFreshnessStatus.FAILED,
                CanonicalFreshnessStatus.DB_UNAVAILABLE,
                CanonicalFreshnessStatus.STALE_AND_EMPTY,
                CanonicalFreshnessStatus.FRESH_BUT_EMPTY,
            ):
                if blocked_by is None:
                    blocked_by = d
            elif status in DEGRADED_STATES:
                if degraded_by is None:
                    degraded_by = d
            elif status in ACTION_NEEDED_STATES:
                if action_needed_by is None:
                    action_needed_by = d
            elif status == CanonicalFreshnessStatus.DEPENDENCY_BLOCKED:
                if blocked_by is None:
                    blocked_by = d
            elif status == CanonicalFreshnessStatus.NOT_RUN:
                if blocked_by is None:
                    blocked_by = d

        if blocked_by:
            pipeline = PIPELINE_DEPENDENCIES.get(blocked_by, {})
            label = pipeline.get("label", blocked_by.replace("_", " ").title())
            impacts.append({
                "page": page.replace("-", " ").title(),
                "status": "blocked",
                "blocked_by": blocked_by,
                "reason": f"{page.replace('-', ' ').title()} depends on {label} which has no usable data.",
            })
        elif degraded_by:
            pipeline = PIPELINE_DEPENDENCIES.get(degraded_by, {})
            label = pipeline.get("label", degraded_by.replace("_", " ").title())
            impacts.append({
                "page": page.replace("-", " ").title(),
                "status": "degraded",
                "blocked_by": degraded_by,
                "reason": f"{page.replace('-', ' ').title()} uses {label} which has usable data but latest sync failed or is stale.",
            })
        elif action_needed_by:
            pipeline = PIPELINE_DEPENDENCIES.get(action_needed_by, {})
            label = pipeline.get("label", action_needed_by.replace("_", " ").title())
            impacts.append({
                "page": page.replace("-", " ").title(),
                "status": "action_needed",
                "blocked_by": action_needed_by,
                "reason": f"{page.replace('-', ' ').title()} uses {label} which can be derived from existing upstream data.",
            })

    return impacts


# ── War Room Assembly ──────────────────────────────────────────────────────

def build_war_room_response(
    *,
    days: int,
    dataset_statuses: dict[str, str],
    dataset_details: dict[str, dict[str, Any]] | None = None,
    sync_info: dict[str, dict[str, Any]] | None = None,
    runs_data: dict[str, Any] | None = None,
    has_source_sync_timestamps: bool = False,
) -> dict[str, Any]:
    """Build the full war room response object.

    Pure function — no DB access, no side effects.
    Requires pre-fetched data as input.
    """
    overall_status, overall_label = compute_overall_status(dataset_statuses)
    summary = compute_summary_counts(dataset_statuses)
    blockers = compute_critical_blockers(dataset_statuses)
    sources = compute_source_health(dataset_statuses, sync_info)
    pipelines = compute_pipelines(dataset_statuses, dataset_details)
    scheduler = compute_scheduler_summary(runs_data, has_source_sync_timestamps)
    page_impact = compute_page_impact(dataset_statuses)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "overall_status": overall_status,
        "overall_label": overall_label,
        "summary": summary,
        "critical_blockers": blockers,
        "sources": sources,
        "pipelines": pipelines,
        "scheduler": scheduler,
        "page_impact": page_impact,
    }
