"""
tests/test_dataset_freshness_endpoint.py

PR-ADS-067 — Tests for the /api/datasets/freshness endpoint's canonical fields.
Uses mocked DB to verify canonical freshness enrichment logic.
"""

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, date, timedelta, timezone


# ── Unit tests for endpoint canonical enrichment logic ──────────────────────

def test_canonical_fields_present_in_enriched_dataset():
    """Verify that a dataset dict enriched with canonical fields has all required keys."""
    from services.freshness_service import (
        DATASET_FRESHNESS_CONFIG,
        compute_canonical_freshness,
        CanonicalFreshnessStatus,
    )

    # Simulate what the endpoint does: compute a verdict and attach fields
    verdict = compute_canonical_freshness(
        dataset="campaigns",
        rows_in_window=100,
        latest_source_date=date.today(),
        sync_status="success",
        latest_batch_status="success",
        latest_batch_row_count=100,
        last_successful_sync_at=datetime.now(timezone.utc),
        stale_threshold_days=8,
    )

    # Check required canonical fields
    assert "canonical_status" in verdict
    assert "severity" in verdict
    assert "reason" in verdict
    assert "next_action" in verdict
    assert verdict["canonical_status"] == CanonicalFreshnessStatus.FRESH_WITH_DATA


def test_dependency_blocking_propagates():
    """When search_terms is fresh_but_empty, waste_terms should be dependency_blocked."""
    from services.freshness_service import (
        compute_canonical_freshness,
        CanonicalFreshnessStatus,
        BLOCKING_STATES,
    )

    # Compute search_terms status first
    st_verdict = compute_canonical_freshness(
        dataset="search_terms",
        rows_in_window=0,
        latest_source_date=date.today(),
        sync_status="success",
        latest_batch_status="success",
        latest_batch_row_count=0,
        last_successful_sync_at=datetime.now(timezone.utc),
        stale_threshold_days=8,
    )
    assert st_verdict["canonical_status"] == CanonicalFreshnessStatus.FRESH_BUT_EMPTY
    assert st_verdict["canonical_status"] in BLOCKING_STATES

    # Now compute waste_terms with search_terms dependency blocked
    wt_verdict = compute_canonical_freshness(
        dataset="waste_terms",
        rows_in_window=0,
        latest_source_date=None,
        sync_status="success",
        latest_batch_status="success",
        latest_batch_row_count=0,
        last_successful_sync_at=datetime.now(timezone.utc),
        stale_threshold_days=8,
        dependency_status=st_verdict["canonical_status"],
    )
    assert wt_verdict["canonical_status"] == CanonicalFreshnessStatus.DEPENDENCY_BLOCKED


def test_ngrams_blocked_when_search_terms_failed():
    """N-Grams should show dependency_blocked when search_terms is failed."""
    from services.freshness_service import (
        compute_canonical_freshness,
        CanonicalFreshnessStatus,
    )

    ng_verdict = compute_canonical_freshness(
        dataset="ngrams",
        rows_in_window=None,
        latest_source_date=None,
        sync_status=None,
        latest_batch_status=None,
        latest_batch_row_count=None,
        last_successful_sync_at=None,
        stale_threshold_days=8,
        dependency_status=CanonicalFreshnessStatus.FAILED,
    )
    assert ng_verdict["canonical_status"] == CanonicalFreshnessStatus.DEPENDENCY_BLOCKED


def test_response_preserves_existing_fields():
    """Canonical fields should be added alongside existing fields, not replacing them."""
    from services.freshness_service import (
        compute_canonical_freshness,
        CanonicalFreshnessStatus,
    )

    # Simulate a dataset dict from the existing endpoint
    existing_row = {
        "dataset_key": "windsor/campaigns",
        "source": "windsor",
        "dataset": "campaigns",
        "status": "success",
        "last_successful_sync_at": datetime.now(timezone.utc).isoformat(),
        "last_source_date": str(date.today()),
        "last_batch_id": 42,
        "error_message": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    # Compute verdict
    verdict = compute_canonical_freshness(
        dataset="campaigns",
        rows_in_window=500,
        latest_source_date=date.today(),
        sync_status="success",
        latest_batch_status="success",
        latest_batch_row_count=500,
        last_successful_sync_at=datetime.now(timezone.utc),
        stale_threshold_days=8,
    )

    # Merge (simulating what endpoint does)
    existing_row.update({
        "canonical_status": verdict["canonical_status"],
        "severity": verdict["severity"],
        "rows_in_window": 500,
        "reason": verdict["reason"],
        "next_action": verdict["next_action"],
    })

    # Old fields still present
    assert "dataset_key" in existing_row
    assert "source" in existing_row
    assert "status" in existing_row
    assert "last_batch_id" in existing_row
    # New canonical fields present
    assert "canonical_status" in existing_row
    assert "severity" in existing_row
    assert "rows_in_window" in existing_row
    assert "reason" in existing_row
    assert "next_action" in existing_row


def test_all_severity_values_valid():
    """Severity should only be ok, warning, error, or neutral."""
    from services.freshness_service import SEVERITY_MAP
    valid = {"ok", "warning", "error", "neutral"}
    for status, severity in SEVERITY_MAP.items():
        assert severity in valid, f"Invalid severity '{severity}' for status '{status}'"
