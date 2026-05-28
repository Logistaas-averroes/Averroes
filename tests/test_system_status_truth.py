"""
tests/test_system_status_truth.py

PR-ADS-095 — Tests for System Status Truth & refined canonical states.
Validates that the system correctly separates data availability from sync status.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.freshness_service import (
    CanonicalFreshnessStatus,
    SEVERITY_MAP,
    BLOCKING_STATES,
    DEGRADED_STATES,
    ACTION_NEEDED_STATES,
    compute_canonical_freshness,
)
from services.system_status_service import (
    CORE_DATASETS,
    DERIVED_DATASETS,
    build_war_room_response,
    compute_critical_blockers,
    compute_overall_status,
    compute_page_impact,
    compute_source_health,
    compute_scheduler_summary,
)


# ── Dataset Status Tests ───────────────────────────────────────────────────


def test_failed_with_rows_returns_data_available_latest_sync_failed():
    """failed + rows > 0 => data_available_latest_sync_failed / warning."""
    result = compute_canonical_freshness(
        dataset="campaigns",
        rows_in_window=5707,
        latest_source_date=None,
        sync_status="failed",
        latest_batch_status="failed",
        latest_batch_row_count=100,
        last_successful_sync_at=None,
        stale_threshold_days=8,
    )
    assert result["canonical_status"] == "data_available_latest_sync_failed"
    assert result["severity"] == "warning"
    assert "usable rows exist" in result["reason"]


def test_failed_with_no_rows_returns_failed_no_data():
    """failed + rows == 0 => failed_no_data / error."""
    result = compute_canonical_freshness(
        dataset="campaigns",
        rows_in_window=0,
        latest_source_date=None,
        sync_status="failed",
        latest_batch_status="failed",
        latest_batch_row_count=0,
        last_successful_sync_at=None,
        stale_threshold_days=8,
    )
    assert result["canonical_status"] == "failed_no_data"
    assert result["severity"] == "error"


def test_failed_with_none_rows_returns_failed_no_data():
    """failed + rows is None => failed_no_data / error (cannot confirm data exists)."""
    result = compute_canonical_freshness(
        dataset="deals",
        rows_in_window=None,
        latest_source_date=None,
        sync_status="failed",
        latest_batch_status=None,
        latest_batch_row_count=None,
        last_successful_sync_at=None,
        stale_threshold_days=8,
    )
    assert result["canonical_status"] == "failed_no_data"
    assert result["severity"] == "error"


def test_not_run_with_upstream_fresh_returns_not_run_but_derivable():
    """not_run + upstream rows > 0 => not_run_but_derivable / warning."""
    result = compute_canonical_freshness(
        dataset="waste_terms",
        rows_in_window=None,
        latest_source_date=None,
        sync_status=None,
        latest_batch_status=None,
        latest_batch_row_count=None,
        last_successful_sync_at=None,
        stale_threshold_days=8,
        upstream_has_data=True,
    )
    assert result["canonical_status"] == "not_run_but_derivable"
    assert result["severity"] == "warning"
    assert "upstream data exists" in result["reason"].lower()


def test_not_run_no_upstream_returns_not_run_no_upstream_data():
    """not_run + upstream has no data => not_run_no_upstream_data / neutral."""
    result = compute_canonical_freshness(
        dataset="waste_terms",
        rows_in_window=None,
        latest_source_date=None,
        sync_status=None,
        latest_batch_status=None,
        latest_batch_row_count=None,
        last_successful_sync_at=None,
        stale_threshold_days=8,
        upstream_has_data=False,
    )
    assert result["canonical_status"] == "not_run_no_upstream_data"
    assert result["severity"] == "neutral"


def test_unknown_row_count_returns_explicit_status():
    """unknown row count => unknown_row_count / neutral."""
    result = compute_canonical_freshness(
        dataset="deals",
        rows_in_window=None,
        latest_source_date=None,
        sync_status="success",
        latest_batch_status="success",
        latest_batch_row_count=None,
        last_successful_sync_at=None,
        stale_threshold_days=8,
    )
    assert result["canonical_status"] == "unknown_row_count"
    assert result["severity"] == "neutral"
    assert "row count" in result["reason"].lower()


def test_row_count_not_enabled_status_exists():
    """row_count_not_enabled is a valid status with neutral severity."""
    assert CanonicalFreshnessStatus.ROW_COUNT_NOT_ENABLED in CanonicalFreshnessStatus.ALL
    assert SEVERITY_MAP[CanonicalFreshnessStatus.ROW_COUNT_NOT_ENABLED] == "neutral"


# ── Source Rollup Tests ───────────────────────────────────────────────────


def test_source_with_failed_no_data_child_is_error():
    """source with failed_no_data child => error."""
    statuses = {
        "campaigns": CanonicalFreshnessStatus.FAILED_NO_DATA,
        "search_terms": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "keywords": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "geo": CanonicalFreshnessStatus.FRESH_WITH_DATA,
    }
    sources = compute_source_health(statuses)
    windsor = next(s for s in sources if s["source"] == "windsor")
    assert windsor["status"] == "error"


def test_source_with_data_available_latest_sync_failed_child_is_warning():
    """source with data_available_latest_sync_failed child only => warning."""
    statuses = {
        "campaigns": CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
        "search_terms": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "keywords": CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
        "geo": CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
    }
    sources = compute_source_health(statuses)
    windsor = next(s for s in sources if s["source"] == "windsor")
    assert windsor["status"] == "warning"
    assert "usable data" in windsor["next_action"].lower()


def test_source_with_all_fresh_with_data_is_ok():
    """source with all fresh_with_data children => ok."""
    statuses = {
        "campaigns": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "search_terms": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "keywords": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "geo": CanonicalFreshnessStatus.FRESH_WITH_DATA,
    }
    sources = compute_source_health(statuses)
    windsor = next(s for s in sources if s["source"] == "windsor")
    assert windsor["status"] == "ok"


def test_source_with_only_unknown_states_is_neutral():
    """source with only unknown/not_run unsupported states => neutral."""
    statuses = {
        "campaigns": CanonicalFreshnessStatus.UNKNOWN,
        "search_terms": CanonicalFreshnessStatus.UNKNOWN,
        "keywords": CanonicalFreshnessStatus.UNKNOWN,
        "geo": CanonicalFreshnessStatus.UNKNOWN,
    }
    sources = compute_source_health(statuses)
    windsor = next(s for s in sources if s["source"] == "windsor")
    assert windsor["status"] == "neutral"


# ── Page Impact Tests ─────────────────────────────────────────────────────


def test_campaigns_failed_sync_with_rows_is_degraded_not_blocked():
    """Campaigns with failed sync but rows > 0 => degraded, not blocked."""
    statuses = {
        "campaigns": CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
        "search_terms": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "leads": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "waste_terms": CanonicalFreshnessStatus.FRESH_WITH_DATA,
    }
    impacts = compute_page_impact(statuses)
    campaigns_impact = next((i for i in impacts if "Campaign" in i["page"]), None)
    assert campaigns_impact is not None
    assert campaigns_impact["status"] == "degraded"
    assert campaigns_impact["status"] != "blocked"


def test_waste_terms_not_run_but_derivable_is_action_needed():
    """Waste Terms not_run but search_terms fresh => action_needed, not blocked."""
    statuses = {
        "search_terms": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "waste_terms": CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE,
    }
    impacts = compute_page_impact(statuses)
    waste_impact = next((i for i in impacts if "Waste" in i["page"]), None)
    assert waste_impact is not None
    assert waste_impact["status"] == "action_needed"
    assert waste_impact["status"] != "blocked"


def test_ngrams_not_run_but_derivable_is_action_needed():
    """N-Grams not_run_but_derivable => action_needed, not blocked."""
    statuses = {
        "search_terms": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "ngrams": CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE,
    }
    impacts = compute_page_impact(statuses)
    ngrams_impact = next((i for i in impacts if "Ngram" in i["page"]), None)
    assert ngrams_impact is not None
    assert ngrams_impact["status"] == "action_needed"


def test_dashboard_not_blocked_if_campaigns_has_usable_rows():
    """Dashboard should not be blocked if Campaigns has usable rows."""
    statuses = {
        "campaigns": CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
        "leads": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "waste_terms": CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE,
    }
    impacts = compute_page_impact(statuses)
    dashboard_impact = next((i for i in impacts if "Dashboard" in i["page"]), None)
    # Dashboard should be degraded at most, not blocked
    if dashboard_impact:
        assert dashboard_impact["status"] != "blocked"


def test_gclid_coverage_not_run_does_not_fully_block_attribution_page():
    """GCLID coverage not_run should not fully block GCLID Attribution readiness page
    if gclid_attribution itself is usable."""
    statuses = {
        "gclid_attribution": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "gclid_coverage_snapshots": CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE,
    }
    impacts = compute_page_impact(statuses)
    gclid_impact = next((i for i in impacts if "Gclid" in i["page"]), None)
    # Should be action_needed, not blocked (since attribution data exists)
    if gclid_impact:
        assert gclid_impact["status"] != "blocked"


# ── Overall Status Tests ──────────────────────────────────────────────────


def test_overall_status_warning_when_core_dataset_has_data_but_sync_failed():
    """Core dataset with data_available_latest_sync_failed => overall warning, not error."""
    statuses = {ds: CanonicalFreshnessStatus.FRESH_WITH_DATA for ds in CORE_DATASETS}
    statuses["campaigns"] = CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED
    statuses["keywords"] = CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED
    statuses["geo"] = CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED
    for ds in DERIVED_DATASETS:
        statuses[ds] = CanonicalFreshnessStatus.FRESH_WITH_DATA
    status, label = compute_overall_status(statuses)
    assert status == "warning"
    assert "degraded" in label.lower()


def test_overall_status_error_when_core_dataset_has_failed_no_data():
    """Core dataset with failed_no_data => overall error."""
    statuses = {ds: CanonicalFreshnessStatus.FRESH_WITH_DATA for ds in CORE_DATASETS}
    statuses["campaigns"] = CanonicalFreshnessStatus.FAILED_NO_DATA
    for ds in DERIVED_DATASETS:
        statuses[ds] = CanonicalFreshnessStatus.FRESH_WITH_DATA
    status, label = compute_overall_status(statuses)
    assert status == "error"


# ── Scheduler Diagnostics Tests ───────────────────────────────────────────


def test_scheduler_null_with_source_timestamps_adds_diagnostic():
    """Scheduler all null + source sync timestamps exist => diagnostic explanation."""
    summary = compute_scheduler_summary(None, has_source_sync_timestamps=True)
    assert summary["latest_daily"] is None
    assert summary["diagnostic_status"] == "no_scheduler_run_recorded"
    assert "source sync timestamps exist" in summary["message"].lower()
    assert summary["next_action"] is not None


def test_scheduler_null_without_source_timestamps():
    """Scheduler all null + no source timestamps => no_runs_found."""
    summary = compute_scheduler_summary(None, has_source_sync_timestamps=False)
    assert summary["diagnostic_status"] == "no_runs_found"


def test_scheduler_with_data_has_no_diagnostic():
    """Scheduler with run data has no diagnostic_status field."""
    runs = {
        "daily": {"status": "success", "started_at": "2026-05-26T06:00:00Z", "finished_at": "2026-05-26T06:03:00Z"},
    }
    summary = compute_scheduler_summary(runs, has_source_sync_timestamps=True)
    assert summary["latest_daily"]["status"] == "success"
    assert "diagnostic_status" not in summary


# ── Severity Mapping Tests ────────────────────────────────────────────────


def test_data_available_latest_sync_failed_is_not_blocking():
    """data_available_latest_sync_failed should NOT be in BLOCKING_STATES."""
    assert CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED not in BLOCKING_STATES


def test_data_available_latest_sync_failed_is_degraded():
    """data_available_latest_sync_failed should be in DEGRADED_STATES."""
    assert CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED in DEGRADED_STATES


def test_not_run_but_derivable_is_action_needed():
    """not_run_but_derivable should be in ACTION_NEEDED_STATES."""
    assert CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE in ACTION_NEEDED_STATES


def test_not_run_but_derivable_is_not_blocking():
    """not_run_but_derivable should NOT be in BLOCKING_STATES."""
    assert CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE not in BLOCKING_STATES


# ── War Room Response Integration ─────────────────────────────────────────


def test_war_room_response_with_degraded_datasets():
    """War room response correctly handles mixed degraded/ok datasets."""
    statuses = {
        "campaigns": CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
        "search_terms": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "keywords": CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
        "geo": CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
        "leads": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "deals": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "waste_terms": CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE,
        "ngrams": CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE,
        "gclid_attribution": CanonicalFreshnessStatus.UNKNOWN_ROW_COUNT,
        "gclid_coverage_snapshots": CanonicalFreshnessStatus.UNKNOWN_ROW_COUNT,
        "historical_intelligence": CanonicalFreshnessStatus.FRESH_WITH_DATA,
    }
    result = build_war_room_response(
        days=60,
        dataset_statuses=statuses,
        has_source_sync_timestamps=True,
    )
    # Overall should be warning, not error
    assert result["overall_status"] == "warning"
    # Page impact should not show campaigns as blocked
    campaigns_blocked = [
        i for i in result["page_impact"]
        if "Campaign" in i["page"] and i["status"] == "blocked"
    ]
    assert len(campaigns_blocked) == 0
