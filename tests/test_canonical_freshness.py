"""
tests/test_canonical_freshness.py

PR-ADS-067 — Unit tests for canonical freshness semantics.
Tests the pure compute_canonical_freshness() function with all state combinations.
"""

from datetime import date, datetime, timedelta, timezone

from services.freshness_service import (
    CanonicalFreshnessStatus,
    SEVERITY_MAP,
    BLOCKING_STATES,
    DATASET_FRESHNESS_CONFIG,
    compute_canonical_freshness,
    canonical_status_display_label,
)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _recent_sync() -> datetime:
    """Return a datetime 1 hour ago (fresh)."""
    return datetime.now(timezone.utc) - timedelta(hours=1)


def _old_sync(days: int = 10) -> datetime:
    """Return a datetime N days ago (stale with default 8-day threshold)."""
    return datetime.now(timezone.utc) - timedelta(days=days)


def _recent_date() -> date:
    """Return today's date."""
    return date.today()


def _old_date(days: int = 10) -> date:
    """Return a date N days ago."""
    return date.today() - timedelta(days=days)


# ── fresh_with_data ─────────────────────────────────────────────────────────

def test_fresh_with_data():
    """Sync succeeded recently and rows exist in the selected window."""
    result = compute_canonical_freshness(
        dataset="campaigns",
        rows_in_window=45292,
        latest_source_date=_recent_date(),
        sync_status="success",
        latest_batch_status="success",
        latest_batch_row_count=45292,
        last_successful_sync_at=_recent_sync(),
        stale_threshold_days=8,
    )
    assert result["canonical_status"] == CanonicalFreshnessStatus.FRESH_WITH_DATA
    assert result["severity"] == "ok"
    assert "Data present" in result["reason"]
    assert "No action" in result["next_action"]


# ── fresh_but_empty / empty_success ────────────────────────────────────────

def test_empty_success_when_sync_success_and_batch_row_count_zero():
    """PR-ADS-095: latest sync succeeded AND batch returned zero rows → empty_success.

    Previously this case emitted fresh_but_empty; the refined model
    distinguishes "source explicitly returned 0 rows" (empty_success) from
    "window query found 0 rows even though batch had rows" (fresh_but_empty).
    """
    result = compute_canonical_freshness(
        dataset="search_terms",
        rows_in_window=0,
        latest_source_date=_recent_date(),
        sync_status="success",
        latest_batch_status="success",
        latest_batch_row_count=0,
        last_successful_sync_at=_recent_sync(),
        stale_threshold_days=8,
    )
    assert result["canonical_status"] == CanonicalFreshnessStatus.EMPTY_SUCCESS
    assert result["severity"] == "warning"
    assert "zero rows" in result["reason"].lower()


def test_fresh_but_empty_when_window_zero_but_batch_had_rows():
    """Window query returns 0 but the latest batch had rows — data exists
    outside the window, so this stays as fresh_but_empty (not empty_success)."""
    result = compute_canonical_freshness(
        dataset="search_terms",
        rows_in_window=0,
        latest_source_date=_recent_date(),
        sync_status="success",
        latest_batch_status="success",
        latest_batch_row_count=13,
        last_successful_sync_at=_recent_sync(),
        stale_threshold_days=8,
    )
    assert result["canonical_status"] == CanonicalFreshnessStatus.FRESH_BUT_EMPTY
    assert result["severity"] == "warning"
    assert "no rows" in result["reason"].lower()


# ── stale_with_data ─────────────────────────────────────────────────────────

def test_stale_with_data():
    """Rows exist, but latest source/sync date is older than threshold."""
    result = compute_canonical_freshness(
        dataset="campaigns",
        rows_in_window=100,
        latest_source_date=_old_date(10),
        sync_status="success",
        latest_batch_status="success",
        latest_batch_row_count=100,
        last_successful_sync_at=_old_sync(10),
        stale_threshold_days=8,
    )
    assert result["canonical_status"] == CanonicalFreshnessStatus.STALE_WITH_DATA
    assert result["severity"] == "warning"
    assert "older than threshold" in result["reason"].lower()


# ── stale_and_empty ─────────────────────────────────────────────────────────

def test_stale_and_empty():
    """No rows and no recent sync."""
    result = compute_canonical_freshness(
        dataset="geo",
        rows_in_window=0,
        latest_source_date=_old_date(15),
        sync_status="success",
        latest_batch_status="success",
        latest_batch_row_count=0,
        last_successful_sync_at=_old_sync(15),
        stale_threshold_days=8,
    )
    assert result["canonical_status"] == CanonicalFreshnessStatus.STALE_AND_EMPTY
    assert result["severity"] == "error"


# ── failed ──────────────────────────────────────────────────────────────────

def test_failed_batch_status_with_rows_is_data_available_latest_sync_failed():
    """Latest sync batch failed but usable rows still exist in the window.

    PR-ADS-095: previously this would return FAILED; now it returns
    data_available_latest_sync_failed because the page can still render.
    """
    result = compute_canonical_freshness(
        dataset="campaigns",
        rows_in_window=50,
        latest_source_date=_recent_date(),
        sync_status="success",
        latest_batch_status="failed",
        latest_batch_row_count=0,
        last_successful_sync_at=_recent_sync(),
        stale_threshold_days=8,
    )
    assert result["canonical_status"] == CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED
    assert result["severity"] == "warning"
    assert "usable rows exist" in result["reason"].lower()


def test_failed_sync_status_no_rows_is_failed_no_data():
    """Sync state failed and no usable rows — PR-ADS-095 failed_no_data."""
    result = compute_canonical_freshness(
        dataset="search_terms",
        rows_in_window=0,
        latest_source_date=None,
        sync_status="failed",
        latest_batch_status=None,
        latest_batch_row_count=0,
        last_successful_sync_at=None,
        stale_threshold_days=8,
    )
    assert result["canonical_status"] == CanonicalFreshnessStatus.FAILED_NO_DATA
    assert result["severity"] == "error"


# ── running ─────────────────────────────────────────────────────────────────

def test_running():
    """Latest sync batch is currently running."""
    result = compute_canonical_freshness(
        dataset="campaigns",
        rows_in_window=None,
        latest_source_date=None,
        sync_status="running",
        latest_batch_status="running",
        latest_batch_row_count=0,
        last_successful_sync_at=None,
        stale_threshold_days=8,
    )
    assert result["canonical_status"] == CanonicalFreshnessStatus.RUNNING
    assert result["severity"] == "neutral"


# ── not_run ─────────────────────────────────────────────────────────────────

def test_not_run():
    """No sync_state or sync_batches exist for this dataset."""
    result = compute_canonical_freshness(
        dataset="historical_intelligence",
        rows_in_window=None,
        latest_source_date=None,
        sync_status=None,
        latest_batch_status=None,
        latest_batch_row_count=None,
        last_successful_sync_at=None,
        stale_threshold_days=14,
    )
    assert result["canonical_status"] == CanonicalFreshnessStatus.NOT_RUN
    assert result["severity"] == "neutral"


# ── db_unavailable ──────────────────────────────────────────────────────────

def test_db_unavailable():
    """Database connection unavailable."""
    result = compute_canonical_freshness(
        dataset="campaigns",
        rows_in_window=None,
        latest_source_date=None,
        sync_status="db_unavailable",
        latest_batch_status=None,
        latest_batch_row_count=None,
        last_successful_sync_at=None,
        stale_threshold_days=8,
    )
    assert result["canonical_status"] == CanonicalFreshnessStatus.DB_UNAVAILABLE
    assert result["severity"] == "error"


# ── blocked_by_dependency / not_run_no_upstream_data ───────────────────────

def test_blocked_by_dependency_waste_terms_when_search_terms_empty():
    """PR-ADS-095: Waste Terms emits BLOCKED_BY_DEPENDENCY when upstream is
    actively broken (Search Terms is fresh_but_empty)."""
    result = compute_canonical_freshness(
        dataset="waste_terms",
        rows_in_window=0,
        latest_source_date=None,
        sync_status="success",
        latest_batch_status="success",
        latest_batch_row_count=0,
        last_successful_sync_at=_recent_sync(),
        stale_threshold_days=8,
        dependency_status=CanonicalFreshnessStatus.FRESH_BUT_EMPTY,
    )
    assert result["canonical_status"] == CanonicalFreshnessStatus.BLOCKED_BY_DEPENDENCY
    assert result["severity"] == "error"
    assert "Search Terms" in result["reason"]


def test_blocked_by_dependency_ngrams_when_search_terms_failed():
    """PR-ADS-095: N-Grams emits BLOCKED_BY_DEPENDENCY when upstream failed."""
    result = compute_canonical_freshness(
        dataset="ngrams",
        rows_in_window=0,
        latest_source_date=None,
        sync_status=None,
        latest_batch_status=None,
        latest_batch_row_count=None,
        last_successful_sync_at=None,
        stale_threshold_days=8,
        dependency_status=CanonicalFreshnessStatus.FAILED,
    )
    assert result["canonical_status"] == CanonicalFreshnessStatus.BLOCKED_BY_DEPENDENCY
    assert result["severity"] == "error"


def test_dependency_not_triggered_when_dep_healthy():
    """Waste Terms NOT blocked when Search Terms is fresh_with_data."""
    result = compute_canonical_freshness(
        dataset="waste_terms",
        rows_in_window=100,
        latest_source_date=_recent_date(),
        sync_status="success",
        latest_batch_status="success",
        latest_batch_row_count=100,
        last_successful_sync_at=_recent_sync(),
        stale_threshold_days=8,
        dependency_status=CanonicalFreshnessStatus.FRESH_WITH_DATA,
    )
    assert result["canonical_status"] == CanonicalFreshnessStatus.FRESH_WITH_DATA


# ── unknown ─────────────────────────────────────────────────────────────────

def test_unknown_fallback():
    """Unknown status when sync_status is unrecognized but not None."""
    # With an unrecognized sync_status that isn't None/running/failed/db_unavailable,
    # and no batch status, but stale dates → stale_and_empty
    result = compute_canonical_freshness(
        dataset="campaigns",
        rows_in_window=0,
        latest_source_date=None,
        sync_status="unknown",
        latest_batch_status=None,
        latest_batch_row_count=None,
        last_successful_sync_at=None,
        stale_threshold_days=8,
    )
    # No last_successful_sync_at → _is_stale returns True → stale_and_empty
    assert result["canonical_status"] == CanonicalFreshnessStatus.STALE_AND_EMPTY


# ── Config validation ───────────────────────────────────────────────────────

def test_all_datasets_have_config():
    """All required datasets have configuration entries."""
    required = [
        "campaigns", "search_terms", "waste_terms", "ngrams",
        "keywords", "geo", "leads", "deals",
        "gclid_attribution", "gclid_coverage_snapshots", "historical_intelligence",
    ]
    for ds in required:
        assert ds in DATASET_FRESHNESS_CONFIG, f"Missing config for {ds}"


def test_config_has_required_fields():
    """Each config entry has required fields."""
    for key, cfg in DATASET_FRESHNESS_CONFIG.items():
        assert "table" in cfg, f"{key} missing 'table'"
        assert "date_column" in cfg, f"{key} missing 'date_column'"
        assert "source" in cfg, f"{key} missing 'source'"
        assert "dataset" in cfg, f"{key} missing 'dataset'"
        assert "stale_threshold_days" in cfg, f"{key} missing 'stale_threshold_days'"
        assert "depends_on" in cfg, f"{key} missing 'depends_on'"
        assert "page" in cfg, f"{key} missing 'page'"


def test_blocking_states():
    """BLOCKING_STATES contains the correct set."""
    assert CanonicalFreshnessStatus.FRESH_BUT_EMPTY in BLOCKING_STATES
    assert CanonicalFreshnessStatus.FAILED in BLOCKING_STATES
    assert CanonicalFreshnessStatus.DB_UNAVAILABLE in BLOCKING_STATES
    assert CanonicalFreshnessStatus.STALE_AND_EMPTY in BLOCKING_STATES
    assert CanonicalFreshnessStatus.NOT_RUN in BLOCKING_STATES
    # These should NOT block
    assert CanonicalFreshnessStatus.FRESH_WITH_DATA not in BLOCKING_STATES
    assert CanonicalFreshnessStatus.STALE_WITH_DATA not in BLOCKING_STATES


def test_severity_map_complete():
    """Every canonical status has a severity mapping."""
    for status in CanonicalFreshnessStatus.ALL:
        assert status in SEVERITY_MAP, f"Missing severity for {status}"


def test_display_labels():
    """Every canonical status has a display label."""
    for status in CanonicalFreshnessStatus.ALL:
        label = canonical_status_display_label(status)
        assert label != "Unknown" or status == CanonicalFreshnessStatus.UNKNOWN


# ── Edge cases ──────────────────────────────────────────────────────────────

def test_fresh_but_empty_with_none_rows():
    """rows_in_window=None should be unknown_row_count, not empty.

    PR-ADS-095: renamed from UNKNOWN to UNKNOWN_ROW_COUNT for clarity.
    """
    result = compute_canonical_freshness(
        dataset="campaigns",
        rows_in_window=None,
        latest_source_date=_recent_date(),
        sync_status="success",
        latest_batch_status="success",
        latest_batch_row_count=0,
        last_successful_sync_at=_recent_sync(),
        stale_threshold_days=8,
    )
    assert result["canonical_status"] == CanonicalFreshnessStatus.UNKNOWN_ROW_COUNT


def test_fresh_but_empty_reason_includes_latest_batch_count():
    """Reason text should surface latest_batch_row_count when useful."""
    result = compute_canonical_freshness(
        dataset="search_terms",
        rows_in_window=0,
        latest_source_date=_recent_date(),
        sync_status="success",
        latest_batch_status="success",
        latest_batch_row_count=13,
        last_successful_sync_at=_recent_sync(),
        stale_threshold_days=8,
    )
    # PR-ADS-095: positive latest_batch_row_count keeps this as FRESH_BUT_EMPTY
    # (not EMPTY_SUCCESS) since data exists outside the window.
    assert result["canonical_status"] == CanonicalFreshnessStatus.FRESH_BUT_EMPTY
    assert "13" in result["reason"]


def test_not_run_no_upstream_data_when_both_downstream_and_upstream_not_run():
    """PR-ADS-095: when both the derived dataset and its upstream are not_run,
    emit NOT_RUN_NO_UPSTREAM_DATA (not the legacy DEPENDENCY_BLOCKED)."""
    result = compute_canonical_freshness(
        dataset="waste_terms",
        rows_in_window=None,
        latest_source_date=None,
        sync_status=None,
        latest_batch_status=None,
        latest_batch_row_count=None,
        last_successful_sync_at=None,
        stale_threshold_days=8,
        dependency_status=CanonicalFreshnessStatus.NOT_RUN,
    )
    assert result["canonical_status"] == CanonicalFreshnessStatus.NOT_RUN_NO_UPSTREAM_DATA
    # PR-ADS-095: not_run_no_upstream_data is an error, not neutral.
    assert result["severity"] == "error"


def test_blocked_by_dependency_when_downstream_already_synced_but_upstream_not_run():
    """If downstream has run but upstream is still NOT_RUN we treat it as a
    real blocker (BLOCKED_BY_DEPENDENCY), not NOT_RUN_NO_UPSTREAM_DATA."""
    result = compute_canonical_freshness(
        dataset="waste_terms",
        rows_in_window=0,
        latest_source_date=_recent_date(),
        sync_status="success",
        latest_batch_status="success",
        latest_batch_row_count=0,
        last_successful_sync_at=_recent_sync(),
        stale_threshold_days=8,
        dependency_status=CanonicalFreshnessStatus.NOT_RUN,
    )
    assert result["canonical_status"] == CanonicalFreshnessStatus.BLOCKED_BY_DEPENDENCY
    assert result["severity"] == "error"


def test_row_count_not_enabled_when_supported_is_false():
    """PR-ADS-095: row_count_supported=False → ROW_COUNT_NOT_ENABLED."""
    result = compute_canonical_freshness(
        dataset="deals",
        rows_in_window=None,
        latest_source_date=_recent_date(),
        sync_status="success",
        latest_batch_status="success",
        latest_batch_row_count=0,
        last_successful_sync_at=_recent_sync(),
        stale_threshold_days=8,
        row_count_supported=False,
    )
    assert result["canonical_status"] == CanonicalFreshnessStatus.ROW_COUNT_NOT_ENABLED


def test_row_count_unknown_when_supported_true_but_query_failed():
    """PR-ADS-095: row_count_supported=True but rows_in_window=None →
    UNKNOWN_ROW_COUNT (query attempted but failed)."""
    result = compute_canonical_freshness(
        dataset="campaigns",
        rows_in_window=None,
        latest_source_date=_recent_date(),
        sync_status="success",
        latest_batch_status="success",
        latest_batch_row_count=0,
        last_successful_sync_at=_recent_sync(),
        stale_threshold_days=8,
        row_count_supported=True,
    )
    assert result["canonical_status"] == CanonicalFreshnessStatus.UNKNOWN_ROW_COUNT
