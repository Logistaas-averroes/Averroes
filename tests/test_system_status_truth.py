"""
tests/test_system_status_truth.py

PR-ADS-095 — System Status Truth & Window/Data Diagnostics.

Verifies the refined System Status semantics:
  - Failed sync + rows > 0  → data_available_latest_sync_failed / warning
  - Failed sync + rows == 0 → failed_no_data / error
  - Derived not_run + upstream fresh → not_run_but_derivable / warning
  - Row count unavailable → unknown_row_count / neutral with explicit reason
  - Source rollup escalation rules
  - Page impact uses degraded / action_needed / blocked, not blanket "blocked"
  - Scheduler diagnostic when run records are missing but sources synced
"""

from datetime import date, datetime, timedelta, timezone

from services.freshness_service import (
    CanonicalFreshnessStatus,
    HAS_DATA_STATES,
    SEVERITY_MAP,
    compute_canonical_freshness,
)
from services.system_status_service import (
    PAGE_STATUS_ACTION_NEEDED,
    PAGE_STATUS_BLOCKED,
    PAGE_STATUS_DEGRADED,
    PAGE_STATUS_OK,
    compute_critical_blockers,
    compute_overall_status,
    compute_page_impact,
    compute_pipelines,
    compute_scheduler_summary,
    compute_source_health,
    compute_summary_counts,
    dataset_page_status,
)


def _recent_sync() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=1)


# ── Refined dataset status semantics ───────────────────────────────────────


def test_failed_sync_with_rows_is_data_available_latest_sync_failed():
    """Mirrors the Campaigns/Keywords/Geo case from the production dump."""
    verdict = compute_canonical_freshness(
        dataset="campaigns",
        rows_in_window=5707,
        latest_source_date=date.today() - timedelta(days=1),
        sync_status="failed",
        latest_batch_status="failed",
        latest_batch_row_count=0,
        last_successful_sync_at=_recent_sync(),
        stale_threshold_days=8,
    )
    assert verdict["canonical_status"] == CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED
    assert verdict["severity"] == "warning"
    assert "usable rows" in verdict["reason"].lower()
    assert "review" in verdict["next_action"].lower()


def test_failed_sync_no_rows_is_failed_no_data():
    verdict = compute_canonical_freshness(
        dataset="campaigns",
        rows_in_window=0,
        latest_source_date=None,
        sync_status="failed",
        latest_batch_status="failed",
        latest_batch_row_count=0,
        last_successful_sync_at=None,
        stale_threshold_days=8,
    )
    assert verdict["canonical_status"] == CanonicalFreshnessStatus.FAILED_NO_DATA
    assert verdict["severity"] == "error"


def test_not_run_derived_with_upstream_data_is_derivable():
    """Waste Terms not yet run, Search Terms fresh — derivable, not blocked."""
    verdict = compute_canonical_freshness(
        dataset="waste_terms",
        rows_in_window=None,
        latest_source_date=None,
        sync_status=None,
        latest_batch_status=None,
        latest_batch_row_count=None,
        last_successful_sync_at=None,
        stale_threshold_days=8,
        dependency_status=CanonicalFreshnessStatus.FRESH_WITH_DATA,
    )
    assert verdict["canonical_status"] == CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE
    assert verdict["severity"] == "warning"


def test_not_run_derived_with_degraded_upstream_is_still_derivable():
    """If upstream is data_available_latest_sync_failed, derived can still run."""
    verdict = compute_canonical_freshness(
        dataset="ngrams",
        rows_in_window=None,
        latest_source_date=None,
        sync_status=None,
        latest_batch_status=None,
        latest_batch_row_count=None,
        last_successful_sync_at=None,
        stale_threshold_days=8,
        dependency_status=CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
    )
    assert verdict["canonical_status"] == CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE


def test_row_count_unknown_is_explicit_state():
    verdict = compute_canonical_freshness(
        dataset="deals",
        rows_in_window=None,
        latest_source_date=date.today(),
        sync_status="success",
        latest_batch_status="success",
        latest_batch_row_count=None,
        last_successful_sync_at=_recent_sync(),
        stale_threshold_days=8,
    )
    assert verdict["canonical_status"] == CanonicalFreshnessStatus.UNKNOWN_ROW_COUNT
    assert verdict["severity"] == "neutral"
    assert "row count" in verdict["reason"].lower()


def test_has_data_states_membership():
    assert CanonicalFreshnessStatus.FRESH_WITH_DATA in HAS_DATA_STATES
    assert CanonicalFreshnessStatus.STALE_WITH_DATA in HAS_DATA_STATES
    assert CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED in HAS_DATA_STATES
    assert CanonicalFreshnessStatus.FRESH_BUT_EMPTY not in HAS_DATA_STATES
    assert CanonicalFreshnessStatus.FAILED_NO_DATA not in HAS_DATA_STATES


def test_severity_includes_new_states():
    for state in [
        CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
        CanonicalFreshnessStatus.FAILED_NO_DATA,
        CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE,
        CanonicalFreshnessStatus.NOT_RUN_NO_UPSTREAM_DATA,
        CanonicalFreshnessStatus.UNKNOWN_ROW_COUNT,
        CanonicalFreshnessStatus.ROW_COUNT_NOT_ENABLED,
        CanonicalFreshnessStatus.BLOCKED_BY_DEPENDENCY,
        CanonicalFreshnessStatus.EMPTY_SUCCESS,
    ]:
        assert state in SEVERITY_MAP, f"missing severity for {state}"


# ── Source rollup ──────────────────────────────────────────────────────────


def test_source_rollup_warning_when_only_degraded():
    """Windsor with Campaigns degraded (sync failed but rows exist) is warning."""
    statuses = {
        "campaigns": CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
        "search_terms": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "keywords": CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
        "geo": CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
    }
    sources = compute_source_health(statuses)
    windsor = next(s for s in sources if s["source"] == "windsor")
    assert windsor["status"] == "warning"
    action = windsor["next_action"].lower()
    assert "usable data" in action
    assert "degraded" in action


def test_source_rollup_error_when_failed_no_data_child():
    statuses = {
        "campaigns": CanonicalFreshnessStatus.FAILED_NO_DATA,
        "search_terms": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "keywords": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "geo": CanonicalFreshnessStatus.FRESH_WITH_DATA,
    }
    sources = compute_source_health(statuses)
    windsor = next(s for s in sources if s["source"] == "windsor")
    assert windsor["status"] == "error"
    assert "campaigns" in windsor["next_action"].lower()


def test_source_rollup_ok_when_all_fresh():
    statuses = {
        "campaigns": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "search_terms": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "keywords": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "geo": CanonicalFreshnessStatus.FRESH_WITH_DATA,
    }
    sources = compute_source_health(statuses)
    windsor = next(s for s in sources if s["source"] == "windsor")
    assert windsor["status"] == "ok"


# ── Page impact ────────────────────────────────────────────────────────────


def test_page_status_helper_for_each_state():
    assert dataset_page_status(CanonicalFreshnessStatus.FRESH_WITH_DATA) == PAGE_STATUS_OK
    assert dataset_page_status(
        CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED
    ) == PAGE_STATUS_DEGRADED
    assert dataset_page_status(CanonicalFreshnessStatus.STALE_WITH_DATA) == PAGE_STATUS_DEGRADED
    assert dataset_page_status(
        CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE
    ) == PAGE_STATUS_ACTION_NEEDED
    assert dataset_page_status(CanonicalFreshnessStatus.FAILED_NO_DATA) == PAGE_STATUS_BLOCKED
    assert dataset_page_status(CanonicalFreshnessStatus.DB_UNAVAILABLE) == PAGE_STATUS_BLOCKED


def test_campaigns_degraded_not_blocked_when_failed_with_rows():
    statuses = {
        "campaigns": CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
        "leads": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "waste_terms": CanonicalFreshnessStatus.FRESH_WITH_DATA,
    }
    impacts = compute_page_impact(statuses)
    campaigns_page = next(i for i in impacts if i["page"] == "Campaigns")
    assert campaigns_page["status"] == PAGE_STATUS_DEGRADED


def test_dashboard_not_blocked_when_campaigns_has_usable_rows():
    statuses = {
        "campaigns": CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
        "leads": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "waste_terms": CanonicalFreshnessStatus.FRESH_WITH_DATA,
    }
    impacts = compute_page_impact(statuses)
    dashboard = next((i for i in impacts if i["page"] == "Dashboard"), None)
    assert dashboard is not None
    assert dashboard["status"] != PAGE_STATUS_BLOCKED
    assert dashboard["status"] == PAGE_STATUS_DEGRADED


def test_waste_terms_derivable_when_search_terms_fresh():
    statuses = {
        "search_terms": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "waste_terms": CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE,
        "ngrams": CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE,
    }
    impacts = compute_page_impact(statuses)
    waste = next(i for i in impacts if i["page"] == "Waste")
    ngram = next(i for i in impacts if i["page"] == "Ngrams")
    assert waste["status"] == PAGE_STATUS_ACTION_NEEDED
    assert waste["status"] != PAGE_STATUS_BLOCKED
    assert ngram["status"] == PAGE_STATUS_ACTION_NEEDED


def test_gclid_attribution_not_blocked_when_only_coverage_not_run():
    """GCLID coverage snapshots not_run should not fully block the readiness page
    when gclid_attribution itself has rows."""
    statuses = {
        "gclid_attribution": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "gclid_coverage_snapshots": CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE,
    }
    impacts = compute_page_impact(statuses)
    gclid_page = next((i for i in impacts if "Gclid" in i["page"]), None)
    # gclid_attribution is ok, gclid_coverage_snapshots is action_needed → worst is action_needed
    assert gclid_page is not None
    assert gclid_page["status"] == PAGE_STATUS_ACTION_NEEDED


# ── Critical blockers ──────────────────────────────────────────────────────


def test_degraded_dataset_is_a_warning_not_error_blocker():
    statuses = {
        "campaigns": CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
    }
    blockers = compute_critical_blockers(statuses)
    degraded = next((b for b in blockers if b["id"] == "campaigns_degraded"), None)
    assert degraded is not None
    assert degraded["severity"] == "warning"


def test_failed_no_data_creates_error_blocker():
    statuses = {
        "campaigns": CanonicalFreshnessStatus.FAILED_NO_DATA,
    }
    blockers = compute_critical_blockers(statuses)
    failed = next((b for b in blockers if b["id"] == "campaigns_failed"), None)
    assert failed is not None
    assert failed["severity"] == "error"


def test_derivable_dataset_creates_warning_blocker():
    statuses = {
        "waste_terms": CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE,
    }
    blockers = compute_critical_blockers(statuses)
    derivable = next((b for b in blockers if b["id"] == "waste_terms_derivable"), None)
    assert derivable is not None
    assert derivable["severity"] == "warning"


# ── Overall status ─────────────────────────────────────────────────────────


def test_overall_status_warning_when_degraded_but_no_failure():
    """Mirrors the production dump: Campaigns/Keywords/Geo degraded, search_terms fresh."""
    statuses = {
        "campaigns": CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
        "keywords": CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
        "geo": CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
        "search_terms": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "leads": CanonicalFreshnessStatus.FRESH_WITH_DATA,
    }
    status, label = compute_overall_status(statuses)
    assert status == "warning"
    assert "degraded" in label.lower()


def test_overall_status_error_when_core_dataset_failed_no_data():
    statuses = {
        "campaigns": CanonicalFreshnessStatus.FAILED_NO_DATA,
        "search_terms": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "leads": CanonicalFreshnessStatus.FRESH_WITH_DATA,
    }
    status, _ = compute_overall_status(statuses)
    assert status == "error"


# ── Pipeline page_status field ─────────────────────────────────────────────


def test_pipelines_expose_page_status():
    statuses = {
        "campaigns": CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
        "waste_terms": CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE,
    }
    pipes = compute_pipelines(statuses)
    by_key = {p["key"]: p for p in pipes}
    assert by_key["campaigns"]["page_status"] == PAGE_STATUS_DEGRADED
    assert by_key["waste_terms"]["page_status"] == PAGE_STATUS_ACTION_NEEDED


# ── Scheduler diagnostics ──────────────────────────────────────────────────


def test_scheduler_diagnostic_when_runs_null_but_sources_synced():
    sync_info = {
        "windsor": {"last_successful_sync_at": "2026-05-25T07:04:00+00:00"},
        "hubspot": {"last_successful_sync_at": None},
    }
    summary = compute_scheduler_summary(None, sync_info)
    assert summary["latest_daily"] is None
    assert summary["latest_weekly"] is None
    assert summary["latest_monthly"] is None
    assert summary["latest_incremental"] is None
    assert summary["diagnostic_status"] == "no_scheduler_run_recorded"
    assert "Source sync" in summary["message"]
    assert "scheduler metadata" in summary["next_action"].lower()


def test_scheduler_no_diagnostic_when_runs_present():
    runs = {
        "daily": {"status": "success", "started_at": "2026-05-26T06:00:00Z"},
    }
    summary = compute_scheduler_summary(runs, sync_info=None)
    assert "diagnostic_status" not in summary
    assert summary["latest_daily"]["status"] == "success"


# ── Summary counts include new states ───────────────────────────────────────


def test_summary_counts_degraded_counted_as_warning():
    counts = compute_summary_counts({
        "campaigns": CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
        "leads": CanonicalFreshnessStatus.FAILED_NO_DATA,
        "search_terms": CanonicalFreshnessStatus.FRESH_WITH_DATA,
    })
    assert counts["warning"] == 1
    assert counts["error"] == 1
    assert counts["ok"] == 1
