"""
services/system_status_service.py

PR-ADS-068 — System Status War Room & Pipeline Dependency Map

Provides consolidated system status logic combining canonical freshness,
pipeline dependencies, source health, scheduler state, and blockers.

Phase 1 — Read Only. No external writes. No scheduler triggers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from services.freshness_service import (
    BLOCKING_STATES,
    DATASET_FRESHNESS_CONFIG,
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

    # Check specifically for db_unavailable or failed in core datasets
    for ds_key in CORE_DATASETS:
        status = dataset_statuses.get(ds_key)
        if status in (
            CanonicalFreshnessStatus.DB_UNAVAILABLE,
            CanonicalFreshnessStatus.FAILED,
        ):
            return "error", "Critical system failure — core dataset unavailable or failed"

    if has_error:
        return "error", "System has critical failures"

    if has_warning:
        # Determine a more specific label
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

        parts = []
        if empty_datasets:
            names = ", ".join(d.replace("_", " ").title() for d in empty_datasets[:2])
            parts.append(f"{names} empty")
        if blocked_datasets:
            parts.append(f"{len(blocked_datasets)} blocked")
        if stale_datasets:
            parts.append(f"{len(stale_datasets)} stale")

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
    dataset_details: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Generate critical blocker objects from canonical statuses."""
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

    # Failed datasets
    for ds_key, status in dataset_statuses.items():
        if status == CanonicalFreshnessStatus.FAILED:
            pipeline = PIPELINE_DEPENDENCIES.get(ds_key, {})
            page = pipeline.get("page", ds_key.replace("_", " ").title())
            blockers.append({
                "id": f"{ds_key}_failed",
                "severity": "error",
                "title": f"{ds_key.replace('_', ' ').title()} sync failed",
                "affected_pages": [page],
                "reason": f"{ds_key.replace('_', ' ').title()} latest sync batch or sync state is failed.",
                "next_action": "Check sync_batches and scheduler logs.",
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
    """Build source health cards from dataset statuses and sync info."""
    sources: list[dict[str, Any]] = []

    for source_key, source_def in SOURCE_DEFINITIONS.items():
        ds_keys = source_def["datasets"]
        ds_severities = []
        for dk in ds_keys:
            cs = dataset_statuses.get(dk, CanonicalFreshnessStatus.UNKNOWN)
            ds_severities.append(SEVERITY_MAP.get(cs, "neutral"))

        # Source status = worst severity of its datasets
        if "error" in ds_severities:
            source_status = "error"
        elif "warning" in ds_severities:
            source_status = "warning"
        elif "ok" in ds_severities:
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
    """Determine next action for a source."""
    if source_status == "ok":
        return "No action needed."
    if source_status == "error":
        return "Check sync logs and database connectivity."
    if source_key == "windsor":
        st = dataset_statuses.get("search_terms")
        if st == CanonicalFreshnessStatus.FRESH_BUT_EMPTY:
            return "Check Search Terms if empty."
        return "Check Windsor sync status."
    if source_key == "hubspot":
        return "Check HubSpot connector logs."
    if source_key == "gclid":
        return "Check GCLID matching pipeline."
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

def compute_scheduler_summary(runs_data: dict[str, Any] | None) -> dict[str, Any]:
    """Build scheduler latest-run summary from runs table data.

    runs_data should be a dict with keys: daily, weekly, monthly, incremental
    each containing {status, started_at, finished_at} or None.
    """
    if not runs_data:
        return {
            "latest_daily": None,
            "latest_weekly": None,
            "latest_monthly": None,
            "latest_incremental": None,
        }
    return {
        "latest_daily": runs_data.get("daily"),
        "latest_weekly": runs_data.get("weekly"),
        "latest_monthly": runs_data.get("monthly"),
        "latest_incremental": runs_data.get("incremental"),
    }


# ── Page Impact ────────────────────────────────────────────────────────────

def compute_page_impact(dataset_statuses: dict[str, str]) -> list[dict[str, Any]]:
    """Determine which pages are blocked by dataset issues."""
    impacts: list[dict[str, Any]] = []

    # Find blocked/unusable datasets
    blocked_datasets: set[str] = set()
    for ds_key, status in dataset_statuses.items():
        if status in BLOCKING_STATES:
            blocked_datasets.add(ds_key)

    for page, deps in PAGE_PIPELINE_IMPACT.items():
        if "all" in deps or "admin" in deps:
            continue

        blocked_by_list = [d for d in deps if d in blocked_datasets]
        if blocked_by_list:
            reasons = []
            for bds in blocked_by_list:
                pipeline = PIPELINE_DEPENDENCIES.get(bds, {})
                label = pipeline.get("label", bds.replace("_", " ").title())
                reasons.append(f"{page.replace('-', ' ').title()} depends on {label}.")

            impacts.append({
                "page": page.replace("-", " ").title(),
                "status": "blocked",
                "blocked_by": blocked_by_list[0],
                "reason": " ".join(reasons),
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
) -> dict[str, Any]:
    """Build the full war room response object.

    Pure function — no DB access, no side effects.
    Requires pre-fetched data as input.
    """
    overall_status, overall_label = compute_overall_status(dataset_statuses)
    summary = compute_summary_counts(dataset_statuses)
    blockers = compute_critical_blockers(dataset_statuses, dataset_details)
    sources = compute_source_health(dataset_statuses, sync_info)
    pipelines = compute_pipelines(dataset_statuses, dataset_details)
    scheduler = compute_scheduler_summary(runs_data)
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
