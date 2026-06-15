"""
tests/test_system_status_war_room.py

PR-ADS-068 — Unit tests for System Status War Room service logic.
Tests pure helper functions with all relevant state combinations.
"""

from services.freshness_service import CanonicalFreshnessStatus
from services.system_status_service import (
    CORE_DATASETS,
    DERIVED_DATASETS,
    PAGE_PIPELINE_IMPACT,
    PIPELINE_DEPENDENCIES,
    SOURCE_DEFINITIONS,
    build_war_room_response,
    compute_critical_blockers,
    compute_overall_status,
    compute_page_impact,
    compute_pipelines,
    compute_scheduler_summary,
    compute_source_health,
    compute_summary_counts,
)


# ── Overall Status Tests ───────────────────────────────────────────────────


def test_overall_status_ok_when_all_core_datasets_fresh_with_data():
    """All core datasets fresh_with_data → overall ok."""
    statuses = {ds: CanonicalFreshnessStatus.FRESH_WITH_DATA for ds in CORE_DATASETS}
    # Add derived as ok too
    for ds in DERIVED_DATASETS:
        statuses[ds] = CanonicalFreshnessStatus.FRESH_WITH_DATA
    status, label = compute_overall_status(statuses)
    assert status == "ok"
    assert "operational" in label.lower()


def test_overall_status_warning_when_search_terms_fresh_but_empty():
    """Search Terms fresh_but_empty → overall warning."""
    statuses = {ds: CanonicalFreshnessStatus.FRESH_WITH_DATA for ds in CORE_DATASETS}
    statuses["search_terms"] = CanonicalFreshnessStatus.FRESH_BUT_EMPTY
    for ds in DERIVED_DATASETS:
        statuses[ds] = CanonicalFreshnessStatus.FRESH_WITH_DATA
    status, label = compute_overall_status(statuses)
    assert status == "warning"


def test_overall_status_error_when_db_unavailable():
    """Any DB unavailable → overall error."""
    statuses = {ds: CanonicalFreshnessStatus.FRESH_WITH_DATA for ds in CORE_DATASETS}
    statuses["campaigns"] = CanonicalFreshnessStatus.DB_UNAVAILABLE
    for ds in DERIVED_DATASETS:
        statuses[ds] = CanonicalFreshnessStatus.FRESH_WITH_DATA
    status, label = compute_overall_status(statuses)
    assert status == "error"


def test_overall_status_error_when_core_dataset_failed():
    """Core dataset failed → overall error."""
    statuses = {ds: CanonicalFreshnessStatus.FRESH_WITH_DATA for ds in CORE_DATASETS}
    statuses["leads"] = CanonicalFreshnessStatus.FAILED
    for ds in DERIVED_DATASETS:
        statuses[ds] = CanonicalFreshnessStatus.FRESH_WITH_DATA
    status, label = compute_overall_status(statuses)
    assert status == "error"


def test_overall_status_neutral_when_empty():
    """No statuses → neutral."""
    status, label = compute_overall_status({})
    assert status == "neutral"


# ── Summary Counts ─────────────────────────────────────────────────────────


def test_summary_counts():
    """Summary counts correctly categorize datasets."""
    statuses = {
        "campaigns": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "search_terms": CanonicalFreshnessStatus.FRESH_BUT_EMPTY,
        "waste_terms": CanonicalFreshnessStatus.DEPENDENCY_BLOCKED,
        "leads": CanonicalFreshnessStatus.FAILED,
        "deals": CanonicalFreshnessStatus.RUNNING,
    }
    counts = compute_summary_counts(statuses)
    assert counts["ok"] == 1
    assert counts["warning"] == 1
    assert counts["error"] == 1
    assert counts["blocked"] == 1
    assert counts["neutral"] == 1


# ── Critical Blockers ──────────────────────────────────────────────────────


def test_search_terms_blocker_affects_waste_terms_and_ngrams():
    """Search Terms empty blocker mentions Waste Terms and N-Grams."""
    statuses = {
        "search_terms": CanonicalFreshnessStatus.FRESH_BUT_EMPTY,
        "waste_terms": CanonicalFreshnessStatus.DEPENDENCY_BLOCKED,
        "ngrams": CanonicalFreshnessStatus.DEPENDENCY_BLOCKED,
    }
    blockers = compute_critical_blockers(statuses)

    # Find the search_terms_empty blocker
    st_blocker = next((b for b in blockers if b["id"] == "search_terms_empty"), None)
    assert st_blocker is not None
    assert "Waste Terms" in st_blocker["affected_pages"]
    assert "N-Grams" in st_blocker["affected_pages"]


def test_dependency_blocked_creates_blocker():
    """Dependency blocked state creates a blocker entry."""
    statuses = {
        "waste_terms": CanonicalFreshnessStatus.DEPENDENCY_BLOCKED,
    }
    blockers = compute_critical_blockers(statuses)

    wt_blocker = next((b for b in blockers if b["id"] == "waste_terms_blocked"), None)
    assert wt_blocker is not None
    assert "Waste Terms" in wt_blocker["title"]
    assert wt_blocker["severity"] == "warning"


def test_db_unavailable_creates_error_blocker():
    """DB unavailable creates an error-severity blocker."""
    statuses = {
        "campaigns": CanonicalFreshnessStatus.DB_UNAVAILABLE,
    }
    blockers = compute_critical_blockers(statuses)

    db_blocker = next((b for b in blockers if b["id"] == "db_unavailable"), None)
    assert db_blocker is not None
    assert db_blocker["severity"] == "error"
    assert "All pages" in db_blocker["affected_pages"]


def test_failed_dataset_creates_blocker():
    """Failed dataset creates a blocker."""
    statuses = {
        "campaigns": CanonicalFreshnessStatus.FAILED,
    }
    blockers = compute_critical_blockers(statuses)

    failed_blocker = next((b for b in blockers if b["id"] == "campaigns_failed"), None)
    assert failed_blocker is not None
    assert failed_blocker["severity"] == "error"


# ── Source Health ──────────────────────────────────────────────────────────


def test_source_health_groups_google_ads_api_datasets():
    """Google Ads API source includes campaigns, search_terms, keywords, geo."""
    statuses = {
        "campaigns": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "search_terms": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "keywords": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "geo": CanonicalFreshnessStatus.FRESH_WITH_DATA,
    }
    sources = compute_source_health(statuses)
    google_ads = next((s for s in sources if s["source"] == "google_ads_api"), None)
    assert google_ads is not None
    assert google_ads["status"] == "ok"
    assert "campaigns" in google_ads["datasets"]
    assert "search_terms" in google_ads["datasets"]
    assert "keywords" in google_ads["datasets"]
    assert "geo" in google_ads["datasets"]


def test_source_health_groups_hubspot_datasets():
    """HubSpot source includes leads and deals."""
    statuses = {
        "leads": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "deals": CanonicalFreshnessStatus.FRESH_WITH_DATA,
    }
    sources = compute_source_health(statuses)
    hubspot = next((s for s in sources if s["source"] == "hubspot"), None)
    assert hubspot is not None
    assert hubspot["status"] == "ok"
    assert "leads" in hubspot["datasets"]
    assert "deals" in hubspot["datasets"]


def test_source_health_warning_when_dataset_empty():
    """Source shows warning when one of its datasets is empty."""
    statuses = {
        "campaigns": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "search_terms": CanonicalFreshnessStatus.FRESH_BUT_EMPTY,
        "keywords": CanonicalFreshnessStatus.FRESH_WITH_DATA,
        "geo": CanonicalFreshnessStatus.FRESH_WITH_DATA,
    }
    sources = compute_source_health(statuses)
    google_ads = next((s for s in sources if s["source"] == "google_ads_api"), None)
    assert google_ads is not None
    assert google_ads["status"] == "warning"


# ── Page Impact ────────────────────────────────────────────────────────────


def test_page_impact_maps_blocked_datasets_to_pages():
    """Blocked search_terms maps to waste and ngrams pages."""
    statuses = {
        "search_terms": CanonicalFreshnessStatus.FRESH_BUT_EMPTY,
        "waste_terms": CanonicalFreshnessStatus.DEPENDENCY_BLOCKED,
        "ngrams": CanonicalFreshnessStatus.DEPENDENCY_BLOCKED,
    }
    impacts = compute_page_impact(statuses)

    # waste page should be impacted (depends on waste_terms and search_terms)
    waste_impact = next((i for i in impacts if "Waste" in i["page"]), None)
    assert waste_impact is not None
    assert waste_impact["status"] == "blocked"

    # ngrams page too
    ngram_impact = next((i for i in impacts if "Ngram" in i["page"]), None)
    assert ngram_impact is not None
    assert ngram_impact["status"] == "blocked"


def test_page_impact_no_impact_when_all_ok():
    """No page impact when everything is ok."""
    statuses = {ds: CanonicalFreshnessStatus.FRESH_WITH_DATA for ds in CORE_DATASETS}
    for ds in DERIVED_DATASETS:
        statuses[ds] = CanonicalFreshnessStatus.FRESH_WITH_DATA
    impacts = compute_page_impact(statuses)
    assert len(impacts) == 0


# ── Scheduler Summary ──────────────────────────────────────────────────────


def test_scheduler_latest_run_summary_handles_missing_runs():
    """Scheduler summary handles None runs gracefully."""
    summary = compute_scheduler_summary(None)
    assert summary["latest_daily"] is None
    assert summary["latest_weekly"] is None
    assert summary["latest_monthly"] is None
    assert summary["latest_incremental"] is None


def test_scheduler_summary_with_data():
    """Scheduler summary passes through run data."""
    runs_data = {
        "daily": {"status": "success", "started_at": "2026-05-26T06:00:00Z", "finished_at": "2026-05-26T06:03:00Z"},
        "weekly": {"status": "success", "started_at": "2026-05-25T07:00:00Z", "finished_at": "2026-05-25T07:08:00Z"},
    }
    summary = compute_scheduler_summary(runs_data)
    assert summary["latest_daily"]["status"] == "success"
    assert summary["latest_weekly"]["status"] == "success"
    assert summary["latest_monthly"] is None
    assert summary["latest_incremental"] is None


# ── Pipeline Details ───────────────────────────────────────────────────────


def test_pipelines_include_dependency_info():
    """Pipeline entries include depends_on and blocks."""
    statuses = {"search_terms": CanonicalFreshnessStatus.FRESH_BUT_EMPTY}
    pipelines = compute_pipelines(statuses)
    st_pipe = next((p for p in pipelines if p["key"] == "search_terms"), None)
    assert st_pipe is not None
    assert "waste_terms" in st_pipe["blocks"]
    assert "ngrams" in st_pipe["blocks"]


def test_pipelines_show_dependent():
    """Dependent pipelines show their depends_on."""
    statuses = {"waste_terms": CanonicalFreshnessStatus.DEPENDENCY_BLOCKED}
    pipelines = compute_pipelines(statuses)
    wt_pipe = next((p for p in pipelines if p["key"] == "waste_terms"), None)
    assert wt_pipe is not None
    assert "search_terms" in wt_pipe["depends_on"]


# ── Full War Room Assembly ─────────────────────────────────────────────────


def test_build_war_room_response_shape():
    """Full response has all required fields."""
    statuses = {ds: CanonicalFreshnessStatus.FRESH_WITH_DATA for ds in CORE_DATASETS}
    for ds in DERIVED_DATASETS:
        statuses[ds] = CanonicalFreshnessStatus.FRESH_WITH_DATA
    result = build_war_room_response(
        days=60,
        dataset_statuses=statuses,
        dataset_details=None,
        sync_info=None,
        runs_data=None,
    )
    assert "generated_at" in result
    assert result["days"] == 60
    assert result["overall_status"] == "ok"
    assert "summary" in result
    assert "critical_blockers" in result
    assert "sources" in result
    assert "pipelines" in result
    assert "scheduler" in result
    assert "page_impact" in result
