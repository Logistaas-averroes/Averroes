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

SEE_PAYLOAD_FILE: bool = True
