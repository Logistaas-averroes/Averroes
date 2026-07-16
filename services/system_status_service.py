"""
services/system_status_service.py

PR-ADS-068 — System Status War Room & Pipeline Dependency Map
PR-ADS-095 — System Status Truth & Window/Data Diagnostics

Provides consolidated system status logic combining canonical freshness,
pipeline dependencies, source health, scheduler state, and blockers.

Phase 1 — Read Only. No external writes. No scheduler triggers.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from services.freshness_service import (
    BLOCKING_STATES,
    DATASET_FRESHNESS_CONFIG,
    CanonicalFreshnessStatus,
    HAS_DATA_STATES,
    SEVERITY_MAP,
)


_SAFE_SQL_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ── Page status (PR-ADS-095) ────────────────────────────────────────────────
# A dataset's canonical status maps to a page-impact status. Pages should be
# "blocked" only when primary data is unavailable; otherwise the page is at
# worst "degraded" or "action_needed".
PAGE_STATUS_OK = "ok"
PAGE_STATUS_DEGRADED = "degraded"
PAGE_STATUS_ACTION_NEEDED = "action_needed"
PAGE_STATUS_BLOCKED = "blocked"
PAGE_STATUS_UNKNOWN = "unknown"

# Canonical statuses that block a page — primary data is missing or unusable.
PAGE_BLOCKING_STATES = frozenset([
    CanonicalFreshnessStatus.FAILED_NO_DATA,
    CanonicalFreshnessStatus.STALE_AND_EMPTY,
    CanonicalFreshnessStatus.DB_UNAVAILABLE,
    CanonicalFreshnessStatus.BLOCKED_BY_DEPENDENCY,
    CanonicalFreshnessStatus.DEPENDENCY_BLOCKED,
    CanonicalFreshnessStatus.FRESH_BUT_EMPTY,
    CanonicalFreshnessStatus.NOT_RUN_NO_UPSTREAM_DATA,
    CanonicalFreshnessStatus.EMPTY_SUCCESS,
    # Legacy alias still seen in the wild
    CanonicalFreshnessStatus.FAILED,
])

# Canonical statuses that degrade a page (rows still available, but warning).
PAGE_DEGRADED_STATES = frozenset([
    CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
    CanonicalFreshnessStatus.STALE_WITH_DATA,
])

# Canonical statuses where the page should prompt user action (e.g. run derived).
PAGE_ACTION_NEEDED_STATES = frozenset([
    CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE,
])


# ── Pipeline Dependency Map ─────────────────────────────────────────────────

PIPELINE_DEPENDENCIES: dict[str, dict[str, Any]] = {
    "campaigns": {
        "label": "Campaigns",
        "source": "google_ads_api",
        "page": "Campaigns",
        "depends_on": [],
        "blocks": [],
    },
    "search_terms": {
        "label": "Search Terms",
        "source": "google_ads_api",
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
        "label": "Keywords (legacy snapshot)",
        "source": "google_ads_api",
        "page": None,
        "depends_on": [],
        "blocks": [],
    },
    "keyword_facts": {
        "label": "Keyword Evidence",
        "source": "google_ads_api",
        "page": "Keywords",
        "depends_on": [],
        "blocks": [],
    },
    "geo": {
        "label": "Geo",
        "source": "google_ads_api",
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
    # PR-ADS-151: Mailchimp read-only email-marketing evidence.
    "mailchimp_campaigns": {
        "label": "Mailchimp Campaigns",
        "source": "mailchimp",
        "page": "Email Marketing",
        "depends_on": [],
        "blocks": ["mailchimp_reports", "mailchimp_attribution"],
    },
    "mailchimp_reports": {
        "label": "Mailchimp Reports",
        "source": "mailchimp",
        "page": "Email Marketing",
        "depends_on": ["mailchimp_campaigns"],
        "blocks": [],
    },
    "mailchimp_audiences": {
        "label": "Mailchimp Audiences",
        "source": "mailchimp",
        "page": "Email Marketing",
        "depends_on": [],
        "blocks": [],
    },
    "mailchimp_attribution": {
        "label": "Mailchimp Attribution",
        "source": "mailchimp",
        "page": "Email Marketing",
        "depends_on": ["mailchimp_campaigns"],
        "blocks": [],
    },
}

# ── Source Definitions ──────────────────────────────────────────────────────

SOURCE_DEFINITIONS: dict[str, dict[str, Any]] = {
    # PR-ADS-105: Google Ads API is the active platform-evidence source for
    # campaigns/search_terms/keywords/geo (scheduler cutover landed in PR-ADS-104).
    # Windsor remains only as legacy/deprecated history, not the active source.
    "google_ads_api": {
        "label": "Google Ads API",
        "datasets": ["campaigns", "search_terms", "keywords", "keyword_facts", "geo"],
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
    # PR-ADS-151: Mailchimp read-only source.
    "mailchimp": {
        "label": "Mailchimp",
        "datasets": ["mailchimp_campaigns", "mailchimp_reports",
                     "mailchimp_audiences", "mailchimp_attribution"],
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
    "keywords": ["keyword_facts"],
    "leads": ["leads"],
    "deals": ["deals"],
    "gclid-attribution": ["gclid_attribution", "gclid_coverage_snapshots"],
    "opportunities": ["leads"],
    "health": ["all"],
    "backfill": ["admin"],
    "historical-intelligence": ["historical_intelligence"],
    "mailchimp": ["mailchimp_campaigns", "mailchimp_reports",
                  "mailchimp_audiences", "mailchimp_attribution"],
}

# ── Core datasets vs derived ───────────────────────────────────────────────

CORE_DATASETS = frozenset([
    "campaigns", "search_terms", "leads", "deals", "keywords", "keyword_facts", "geo",
])

DERIVED_DATASETS = frozenset([
    "waste_terms", "ngrams", "gclid_attribution",
    "gclid_coverage_snapshots", "historical_intelligence",
    "mailchimp_attribution",
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

    # Critical system failure — core dataset truly unusable (no data).
    # PR-ADS-095: data_available_latest_sync_failed is NOT critical because rows exist.
    for ds_key in CORE_DATASETS:
        status = dataset_statuses.get(ds_key)
        if status in (
            CanonicalFreshnessStatus.DB_UNAVAILABLE,
            CanonicalFreshnessStatus.FAILED_NO_DATA,
            CanonicalFreshnessStatus.FAILED,
            CanonicalFreshnessStatus.STALE_AND_EMPTY,
        ):
            return "error", "Critical system failure — core dataset unavailable or failed"

    if has_error:
        return "error", "System has critical failures"

    if has_warning:
        # Determine a more specific label
        blocked_datasets = [
            k for k, v in dataset_statuses.items()
            if v in (
                CanonicalFreshnessStatus.DEPENDENCY_BLOCKED,
                CanonicalFreshnessStatus.BLOCKED_BY_DEPENDENCY,
            )
        ]
        empty_datasets = [
            k for k, v in dataset_statuses.items()
            if v == CanonicalFreshnessStatus.FRESH_BUT_EMPTY
        ]
        stale_datasets = [
            k for k, v in dataset_statuses.items()
            if v == CanonicalFreshnessStatus.STALE_WITH_DATA
        ]
        degraded_datasets = [
            k for k, v in dataset_statuses.items()
            if v == CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED
        ]
        derivable_datasets = [
            k for k, v in dataset_statuses.items()
            if v == CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE
        ]

        parts = []
        if empty_datasets:
            names = ", ".join(d.replace("_", " ").title() for d in empty_datasets[:2])
            parts.append(f"{names} empty")
        if degraded_datasets:
            parts.append(f"{len(degraded_datasets)} degraded (sync failed, rows exist)")
        if blocked_datasets:
            parts.append(f"{len(blocked_datasets)} blocked")
        if stale_datasets:
            parts.append(f"{len(stale_datasets)} stale")
        if derivable_datasets:
            parts.append(f"{len(derivable_datasets)} derivable")

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
    blocked_states = (
        CanonicalFreshnessStatus.DEPENDENCY_BLOCKED,
        CanonicalFreshnessStatus.BLOCKED_BY_DEPENDENCY,
    )
    for canonical_status in dataset_statuses.values():
        if canonical_status in blocked_states:
            counts["blocked"] += 1
        else:
            severity = SEVERITY_MAP.get(canonical_status, "neutral")
            counts[severity] = counts.get(severity, 0) + 1
    return counts


# ── Critical Blockers ──────────────────────────────────────────────────────

def compute_critical_blockers(
    dataset_statuses: dict[str, str],
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

    # Failed datasets with no usable data (true blockers)
    for ds_key, status in dataset_statuses.items():
        if status in (
            CanonicalFreshnessStatus.FAILED,
            CanonicalFreshnessStatus.FAILED_NO_DATA,
        ):
            pipeline = PIPELINE_DEPENDENCIES.get(ds_key, {})
            page = pipeline.get("page", ds_key.replace("_", " ").title())
            blockers.append({
                "id": f"{ds_key}_failed",
                "severity": "error",
                "title": f"{ds_key.replace('_', ' ').title()} sync failed",
                "affected_pages": [page],
                "reason": f"{ds_key.replace('_', ' ').title()} latest sync failed and no usable rows are available.",
                "next_action": "Check sync_batches and scheduler logs.",
            })

    # PR-ADS-095: failed sync but rows exist — surfaced as a warning, NOT a critical blocker
    for ds_key, status in dataset_statuses.items():
        if status == CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED:
            pipeline = PIPELINE_DEPENDENCIES.get(ds_key, {})
            page = pipeline.get("page", ds_key.replace("_", " ").title())
            blockers.append({
                "id": f"{ds_key}_degraded",
                "severity": "warning",
                "title": f"{ds_key.replace('_', ' ').title()} sync failed (data still available)",
                "affected_pages": [page],
                "reason": "Latest sync failed, but usable rows exist in the selected window.",
                "next_action": "Review latest sync error, but page can still render using existing data.",
            })

    # PR-ADS-095: derived datasets that haven't run but can be derived from fresh upstream
    for ds_key, status in dataset_statuses.items():
        if status == CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE:
            pipeline = PIPELINE_DEPENDENCIES.get(ds_key, {})
            page = pipeline.get("page", ds_key.replace("_", " ").title())
            depends_on = pipeline.get("depends_on", [])
            dep_names = ", ".join(d.replace("_", " ").title() for d in depends_on) if depends_on else "upstream data"
            blockers.append({
                "id": f"{ds_key}_derivable",
                "severity": "warning",
                "title": f"{ds_key.replace('_', ' ').title()} not run yet (derivable)",
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
            "next_action": "Check Search Terms verdict and Google Ads API search-term sync.",
        })

    # Dependency blocked datasets
    for ds_key, status in dataset_statuses.items():
        if status in (
            CanonicalFreshnessStatus.DEPENDENCY_BLOCKED,
            CanonicalFreshnessStatus.BLOCKED_BY_DEPENDENCY,
        ):
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
    """Determine next action for a source.

    PR-ADS-095: Specific guidance when datasets are degraded (failed sync but
    rows still exist) rather than a generic "Check sync logs" message.
    """
    if source_status == "ok":
        return "No action needed."

    # PR-ADS-095: collect the datasets in each notable state for this source
    source_def = SOURCE_DEFINITIONS.get(source_key, {})
    ds_keys = source_def.get("datasets", [])
    degraded = [k for k in ds_keys
                if dataset_statuses.get(k) == CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED]
    failed_no_data = [k for k in ds_keys
                      if dataset_statuses.get(k) in (
                          CanonicalFreshnessStatus.FAILED_NO_DATA,
                          CanonicalFreshnessStatus.FAILED,
                      )]
    derivable = [k for k in ds_keys
                 if dataset_statuses.get(k) == CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE]

    if source_status == "error":
        if failed_no_data:
            names = ", ".join(d.replace("_", " ").title() for d in failed_no_data)
            return f"{names} sync failed with no usable data. Check sync logs and retry source sync."
        return "Check sync logs and database connectivity."

    # Warning state — pick the most informative message
    if degraded:
        names = ", ".join(d.replace("_", " ").title() for d in degraded)
        return (
            f"{source_def.get('label', source_key)} has usable data, but latest sync failed for "
            f"{names}. Review sync errors; existing pages may be degraded but not fully blocked."
        )
    if derivable:
        names = ", ".join(d.replace("_", " ").title() for d in derivable)
        return f"{names} not run yet. Run derived analysis from existing upstream data."
    if source_key == "google_ads_api":
        st = dataset_statuses.get("search_terms")
        if st == CanonicalFreshnessStatus.FRESH_BUT_EMPTY:
            return "Check Search Terms if empty."
        return "Check Google Ads API sync status."
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
        page_status = dataset_page_status(canonical_status)

        pipelines.append({
            "key": ds_key,
            "label": f"{pipe_def['label']} Pipeline",
            "source": pipe_def["source"],
            "page": pipe_def["page"],
            "canonical_status": canonical_status,
            "severity": severity,
            "page_status": page_status,
            "rows_in_window": ds_detail.get("rows_in_window"),
            "latest_source_date": ds_detail.get("latest_source_date"),
            "last_batch_row_count": ds_detail.get("last_batch_row_count"),
            "depends_on": pipe_def["depends_on"],
            "blocks": pipe_def["blocks"],
            "reason": ds_detail.get("reason", ""),
            "next_action": ds_detail.get("next_action", ""),
        })

    return pipelines


# ── Page status helper (PR-ADS-095) ────────────────────────────────────────

def dataset_page_status(canonical_status: str) -> str:
    """Map a dataset canonical status to its page-impact contribution.

    PR-ADS-095: pages are blocked only when primary data is unavailable. A
    failed sync with rows still in the window is degraded, not blocked.
    """
    if canonical_status in PAGE_BLOCKING_STATES:
        return PAGE_STATUS_BLOCKED
    if canonical_status in PAGE_DEGRADED_STATES:
        return PAGE_STATUS_DEGRADED
    if canonical_status in PAGE_ACTION_NEEDED_STATES:
        return PAGE_STATUS_ACTION_NEEDED
    if canonical_status == CanonicalFreshnessStatus.FRESH_WITH_DATA:
        return PAGE_STATUS_OK
    if canonical_status in (
        CanonicalFreshnessStatus.RUNNING,
        CanonicalFreshnessStatus.NOT_RUN,
        CanonicalFreshnessStatus.UNKNOWN,
        CanonicalFreshnessStatus.UNKNOWN_ROW_COUNT,
        CanonicalFreshnessStatus.ROW_COUNT_NOT_ENABLED,
    ):
        return PAGE_STATUS_UNKNOWN
    return PAGE_STATUS_UNKNOWN


# ── Scheduler Summary ──────────────────────────────────────────────────────

def compute_scheduler_summary(
    runs_data: dict[str, Any] | None,
    sync_info: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build scheduler latest-run summary from runs table data.

    runs_data should be a dict with keys: daily, weekly, monthly, incremental
    each containing {status, started_at, finished_at} or None.

    PR-ADS-095: when all four run-type entries are null but source-level
    last_successful_sync_at timestamps exist, surface a diagnostic message so
    operators understand the runs table is empty rather than the source being
    broken.
    """
    runs_data = runs_data or {}
    summary: dict[str, Any] = {
        "latest_daily": runs_data.get("daily"),
        "latest_weekly": runs_data.get("weekly"),
        "latest_monthly": runs_data.get("monthly"),
        "latest_incremental": runs_data.get("incremental"),
    }

    all_null = all(summary[k] is None for k in (
        "latest_daily", "latest_weekly", "latest_monthly", "latest_incremental",
    ))
    if not all_null:
        return summary

    # PR-ADS-095: check whether any source has a recorded successful sync
    any_source_synced = False
    if sync_info:
        for info in sync_info.values():
            if info and info.get("last_successful_sync_at"):
                any_source_synced = True
                break

    if any_source_synced:
        summary["diagnostic_status"] = "no_scheduler_run_recorded"
        summary["message"] = (
            "Source sync timestamps exist, but no scheduler run records were found."
        )
        summary["next_action"] = (
            "Check whether background/manual syncs write scheduler metadata."
        )
    else:
        summary["diagnostic_status"] = "no_scheduler_run_recorded"
        summary["message"] = "No scheduler run records were found."
        summary["next_action"] = "Trigger a daily/weekly/monthly run."
    return summary


# ── Page Impact (PR-ADS-095) ───────────────────────────────────────────────

# Page status priority — most severe first. Used to roll up multiple dataset
# page statuses into a single page status.
_PAGE_STATUS_PRIORITY = [
    PAGE_STATUS_BLOCKED,
    PAGE_STATUS_DEGRADED,
    PAGE_STATUS_ACTION_NEEDED,
    PAGE_STATUS_UNKNOWN,
    PAGE_STATUS_OK,
]


def _page_status_rank(status: str) -> int:
    try:
        return _PAGE_STATUS_PRIORITY.index(status)
    except ValueError:
        return len(_PAGE_STATUS_PRIORITY)


def compute_page_impact(dataset_statuses: dict[str, str]) -> list[dict[str, Any]]:
    """Determine each page's impact based on the worst contributing dataset.

    PR-ADS-095: returns ok / degraded / action_needed / blocked / unknown
    instead of only emitting fully-blocked pages. Pages are only blocked when
    primary data is unavailable; failed sync with rows is degraded.
    """
    impacts: list[dict[str, Any]] = []

    for page, deps in PAGE_PIPELINE_IMPACT.items():
        if "all" in deps or "admin" in deps:
            continue

        # Filter to deps that actually have a status reported.
        scoped_deps = [d for d in deps if d in dataset_statuses]
        if not scoped_deps:
            continue

        # Find the worst page status contribution across this page's deps.
        worst_status = PAGE_STATUS_OK
        worst_dep: str | None = None
        contributing: list[tuple[str, str]] = []
        for ds_key in scoped_deps:
            canonical = dataset_statuses.get(ds_key, CanonicalFreshnessStatus.UNKNOWN)
            ps = dataset_page_status(canonical)
            contributing.append((ds_key, ps))
            if _page_status_rank(ps) < _page_status_rank(worst_status):
                worst_status = ps
                worst_dep = ds_key

        if worst_status == PAGE_STATUS_OK:
            # No need to surface healthy pages in the impact list.
            continue

        # Build a reason from the contributing dataset(s) at the worst status.
        worst_contribs = [d for d, s in contributing if s == worst_status]
        reasons: list[str] = []
        for bds in worst_contribs:
            pipeline = PIPELINE_DEPENDENCIES.get(bds, {})
            label = pipeline.get("label", bds.replace("_", " ").title())
            canonical = dataset_statuses.get(bds, "")
            phrase = _impact_reason_phrase(canonical, label)
            reasons.append(
                f"{page.replace('-', ' ').title()} depends on {label}; {phrase}"
            )

        impacts.append({
            "page": page.replace("-", " ").title(),
            "status": worst_status,
            "blocked_by": worst_dep,
            "reason": " ".join(reasons),
        })

    return impacts


def _impact_reason_phrase(canonical_status: str, label: str) -> str:
    """Per-state explanation for page_impact reason strings."""
    if canonical_status in (
        CanonicalFreshnessStatus.FAILED_NO_DATA,
        CanonicalFreshnessStatus.FAILED,
    ):
        return f"{label} sync failed and has no usable rows."
    if canonical_status == CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED:
        return f"{label} latest sync failed but usable rows still exist; page is degraded, not blocked."
    if canonical_status == CanonicalFreshnessStatus.STALE_WITH_DATA:
        return f"{label} data is stale; page is degraded."
    if canonical_status == CanonicalFreshnessStatus.STALE_AND_EMPTY:
        return f"{label} is stale and empty."
    if canonical_status == CanonicalFreshnessStatus.FRESH_BUT_EMPTY:
        return f"{label} synced but is empty."
    if canonical_status == CanonicalFreshnessStatus.DB_UNAVAILABLE:
        return "database is unavailable."
    if canonical_status in (
        CanonicalFreshnessStatus.DEPENDENCY_BLOCKED,
        CanonicalFreshnessStatus.BLOCKED_BY_DEPENDENCY,
    ):
        return f"{label} is blocked by its upstream dependency."
    if canonical_status == CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE:
        return f"{label} has not run yet, but upstream data exists; action needed."
    return f"{label} is in state {canonical_status.replace('_', ' ') or 'unknown'}."


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
    blockers = compute_critical_blockers(dataset_statuses)
    sources = compute_source_health(dataset_statuses, sync_info)
    pipelines = compute_pipelines(dataset_statuses, dataset_details)
    scheduler = compute_scheduler_summary(runs_data, sync_info)
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


# ── Window/Data Diagnostics (PR-ADS-095) ───────────────────────────────────


def diagnose_dataset_window(
    *,
    rows_by_window: dict[str, int | None],
    latest_sync_status: str | None,
    row_count_available: bool,
    row_count_unavailable_reason: str | None,
) -> dict[str, str | bool]:
    """Classify a single dataset's window diagnostic.

    Returns ``diagnostic_status`` (one of the PR-ADS-095 canonical strings)
    and ``usable_for_page`` indicating whether a page can render using
    existing rows even if the latest sync failed.
    """
    largest_count: int | None = None
    for v in rows_by_window.values():
        if v is None:
            continue
        if largest_count is None or v > largest_count:
            largest_count = v

    has_rows = (largest_count is not None) and (largest_count > 0)
    sync_failed = latest_sync_status == "failed"

    if not row_count_available:
        # Differentiate "intentionally not enabled" from "tried and failed"
        if row_count_unavailable_reason and "not enabled" in row_count_unavailable_reason:
            return {
                "diagnostic_status": CanonicalFreshnessStatus.ROW_COUNT_NOT_ENABLED,
                "usable_for_page": False,
                "reason": row_count_unavailable_reason,
                "next_action": (
                    "Implement row-count diagnostic if this page depends on freshness."
                ),
            }
        return {
            "diagnostic_status": CanonicalFreshnessStatus.UNKNOWN_ROW_COUNT,
            "usable_for_page": False,
            "reason": row_count_unavailable_reason or (
                "Row count query unavailable or not implemented for this dataset."
            ),
            "next_action": "Check row-count query support for this dataset.",
        }

    if sync_failed and has_rows:
        return {
            "diagnostic_status": CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
            "usable_for_page": True,
            "reason": "Latest sync failed, but usable rows exist in the selected window.",
            "next_action": (
                "Review latest sync error, but page can still render using existing data."
            ),
        }
    if sync_failed and not has_rows:
        return {
            "diagnostic_status": CanonicalFreshnessStatus.FAILED_NO_DATA,
            "usable_for_page": False,
            "reason": "Latest sync failed and no usable rows are available.",
            "next_action": "Check sync logs and retry source sync.",
        }
    if not has_rows:
        return {
            "diagnostic_status": CanonicalFreshnessStatus.FRESH_BUT_EMPTY,
            "usable_for_page": False,
            "reason": "No rows in any of the requested windows.",
            "next_action": "Trigger sync and verify data source is producing rows.",
        }
    return {
        "diagnostic_status": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "usable_for_page": True,
        "reason": "Rows present in the requested windows.",
        "next_action": "No action needed.",
    }


def gather_dataset_window_counts(
    cur: Any,
    *,
    windows: list[str],
    only_dataset: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Gather per-dataset window row counts and sync info from the DB.

    Pure DB read against the provided psycopg2 cursor — callers manage the
    cursor/transaction. Used by both the ``/api/diagnostics/window-semantics``
    endpoint and ``scripts/diagnose_window_semantics.py`` so the script and
    the API stay in lockstep.
    """
    from psycopg2 import sql as _psql  # noqa: PLC0415

    window_days: dict[str, int] = {w: int(w.rstrip("d")) for w in windows}

    cur.execute(
        "SELECT source, dataset, status, last_successful_sync_at, last_source_date "
        "FROM sync_state"
    )
    sync_rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    sync_map: dict[tuple[str, str], dict[str, Any]] = {}
    for row in sync_rows:
        r = dict(zip(cols, row))
        sync_map[(r["source"], r["dataset"])] = r

    diagnostics: dict[str, dict[str, Any]] = {}
    for cfg_key, cfg in DATASET_FRESHNESS_CONFIG.items():
        if only_dataset and cfg_key != only_dataset:
            continue
        table_name = str(cfg.get("table") or "")
        date_column = str(cfg.get("date_column") or "")
        diag: dict[str, Any] = {
            "source": cfg.get("source"),
            "table": table_name or None,
            "date_column": date_column or None,
            "window_counts": {},
            "row_count_available": False,
        }

        if not table_name or not date_column or not (
            _SAFE_SQL_IDENT_RE.match(table_name)
            and _SAFE_SQL_IDENT_RE.match(date_column)
        ):
            diag["row_count_unavailable_reason"] = (
                "row-count query not enabled for this dataset "
                "(missing or non-identifier table/date_column)"
            )
        else:
            diag["row_count_available"] = True
            for label, ndays in window_days.items():
                ws = date.today() - timedelta(days=ndays)
                try:
                    q = _psql.SQL(
                        "SELECT COUNT(*) FROM {} WHERE {} >= %s"
                    ).format(
                        _psql.Identifier(table_name),
                        _psql.Identifier(date_column),
                    )
                    cur.execute(q, (ws,))
                    r2 = cur.fetchone()
                    count = int(r2[0]) if r2 and r2[0] is not None else 0
                    diag["window_counts"][label] = count
                except Exception:  # noqa: BLE001
                    diag["window_counts"][label] = None
                    diag["row_count_unavailable_reason"] = (
                        "row-count query failed at execution time"
                    )

            try:
                q = _psql.SQL(
                    "SELECT COUNT(*) FROM {} WHERE {} IS NULL"
                ).format(
                    _psql.Identifier(table_name),
                    _psql.Identifier(date_column),
                )
                cur.execute(q)
                r3 = cur.fetchone()
                diag["missing_date_rows"] = (
                    int(r3[0]) if r3 and r3[0] is not None else 0
                )
            except Exception:  # noqa: BLE001
                diag["missing_date_rows"] = None

        sync_row = sync_map.get((cfg.get("source"), cfg.get("dataset")), {})
        diag["latest_source_date"] = (
            str(sync_row["last_source_date"])
            if sync_row.get("last_source_date") else None
        )
        diag["latest_sync_status"] = sync_row.get("status")
        last_sync_at = sync_row.get("last_successful_sync_at")
        diag["last_successful_sync_at"] = (
            last_sync_at.isoformat() if last_sync_at else None
        )
        diagnostics[cfg_key] = diag

    return diagnostics


def db_unavailable_window_payload(
    only_dataset: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Return the dataset_diagnostics payload for a DB-unavailable scenario."""
    keys = [
        k for k in DATASET_FRESHNESS_CONFIG
        if not only_dataset or k == only_dataset
    ]
    return {k: {"db_unavailable": True} for k in keys}


def build_window_diagnostics(
    *,
    windows: list[str],
    dataset_diagnostics: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Assemble the window/data diagnostics response (PR-ADS-095).

    Pure function — no DB access, no side effects.
    """
    out_datasets: list[dict[str, Any]] = []
    for ds_key, raw in dataset_diagnostics.items():
        if raw.get("db_unavailable"):
            out_datasets.append({
                "key": ds_key,
                "source": None,
                "db_unavailable": True,
                "diagnostic_status": CanonicalFreshnessStatus.DB_UNAVAILABLE,
                "usable_for_page": False,
                "reason": "Database connection unavailable.",
                "next_action": "Check database connectivity and restart if needed.",
            })
            continue

        window_counts = raw.get("window_counts") or {}
        verdict = diagnose_dataset_window(
            rows_by_window={k: window_counts.get(k) for k in windows},
            latest_sync_status=raw.get("latest_sync_status"),
            row_count_available=bool(raw.get("row_count_available", False)),
            row_count_unavailable_reason=raw.get("row_count_unavailable_reason"),
        )
        out_datasets.append({
            "key": ds_key,
            "source": raw.get("source"),
            "table": raw.get("table"),
            "date_column": raw.get("date_column"),
            "window_counts": {w: window_counts.get(w) for w in windows},
            "latest_source_date": raw.get("latest_source_date"),
            "latest_sync_status": raw.get("latest_sync_status"),
            "last_successful_sync_at": raw.get("last_successful_sync_at"),
            "missing_date_rows": raw.get("missing_date_rows"),
            "invalid_date_rows": raw.get("invalid_date_rows"),
            "diagnostic_status": verdict["diagnostic_status"],
            "usable_for_page": verdict["usable_for_page"],
            "reason": verdict.get("reason", ""),
            "next_action": verdict.get("next_action", ""),
        })

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "windows": windows,
        "datasets": out_datasets,
    }
