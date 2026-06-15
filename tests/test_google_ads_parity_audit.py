"""
Tests for PR-ADS-099: Google Ads API vs Windsor Dataset Parity Audit
PR-ADS-106: Closed-Window Hardening

Validates:
- Parity math functions
- Percent delta handles zero safely
- Status classification logic
- No Google Ads mutate services referenced
- No database writes in audit script
- Docs mention read-only audit and no production cutover
- Closed (yesterday-back, UTC) date windows
- conversion_value aggregation and cost_micros normalization
- Windsor closed-window range fetch vs preset search-terms fallback
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import scripts.google_ads_parity_audit as audit
from scripts.google_ads_parity_audit import (
    percent_delta,
    classify_status,
    format_delta,
    aggregate_metrics,
    fetch_windsor_data,
    run_audit,
    _row_spend,
    _row_conversion_value,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_source(rel_path: str) -> str:
    full_path = os.path.join(REPO_ROOT, rel_path)
    with open(full_path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# percent_delta tests
# ---------------------------------------------------------------------------

class TestPercentDelta:
    """Test percent_delta function."""

    def test_both_zero(self):
        """Both values zero should return 0.0."""
        assert percent_delta(0, 0) == 0.0

    def test_windsor_zero_google_nonzero(self):
        """Windsor zero, Google nonzero should return None (cannot divide by zero)."""
        assert percent_delta(100, 0) is None

    def test_equal_values(self):
        """Equal non-zero values should return 0.0."""
        assert percent_delta(100, 100) == 0.0

    def test_positive_delta(self):
        """Google higher than Windsor should return positive percentage."""
        result = percent_delta(110, 100)
        assert result == pytest.approx(10.0)

    def test_negative_delta(self):
        """Google lower than Windsor should return negative percentage."""
        result = percent_delta(90, 100)
        assert result == pytest.approx(-10.0)

    def test_small_delta(self):
        """Small delta should be precise."""
        result = percent_delta(101.8, 100)
        assert result == pytest.approx(1.8)

    def test_large_delta(self):
        """Large delta should compute correctly."""
        result = percent_delta(200, 100)
        assert result == pytest.approx(100.0)

    def test_float_values(self):
        """Should handle float values."""
        result = percent_delta(50.5, 50.0)
        assert result == pytest.approx(1.0)

    def test_negative_baseline(self):
        """Should handle negative baseline using abs."""
        # This edge case uses absolute value of windsor
        result = percent_delta(0, -100)
        assert result == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# classify_status tests
# ---------------------------------------------------------------------------

class TestClassifyStatus:
    """Test classify_status function."""

    def test_pass_all_within_threshold(self):
        """All deltas within threshold should return PASS."""
        status = classify_status(
            spend_delta_pct=1.0,
            clicks_delta_pct=2.0,
            impressions_delta_pct=3.0,
            row_count_google=100,
            row_count_windsor=105,
        )
        assert status == "PASS"

    def test_pass_zero_both(self):
        """Both sources zero rows should return PASS."""
        status = classify_status(
            spend_delta_pct=0.0,
            clicks_delta_pct=0.0,
            impressions_delta_pct=0.0,
            row_count_google=0,
            row_count_windsor=0,
        )
        assert status == "PASS"

    def test_warning_spend_under_10(self):
        """Spend delta 5-10% should return WARNING."""
        status = classify_status(
            spend_delta_pct=7.0,
            clicks_delta_pct=8.0,
            impressions_delta_pct=9.0,
            row_count_google=100,
            row_count_windsor=100,
        )
        assert status == "WARNING"

    def test_warning_row_count_differs_materially(self):
        """Spend within threshold but row count differs >50% should return WARNING."""
        status = classify_status(
            spend_delta_pct=1.0,
            clicks_delta_pct=1.0,
            impressions_delta_pct=1.0,
            row_count_google=6769,
            row_count_windsor=3391,
        )
        assert status == "WARNING"

    def test_fail_large_spend_delta(self):
        """Large spend delta should return FAIL."""
        status = classify_status(
            spend_delta_pct=15.0,
            clicks_delta_pct=2.0,
            impressions_delta_pct=2.0,
            row_count_google=100,
            row_count_windsor=100,
        )
        assert status == "FAIL"

    def test_fail_source_error(self):
        """Source error should return FAIL."""
        status = classify_status(
            spend_delta_pct=0.0,
            clicks_delta_pct=0.0,
            impressions_delta_pct=0.0,
            row_count_google=100,
            row_count_windsor=100,
            source_error=True,
        )
        assert status == "FAIL"

    def test_not_available_no_windsor(self):
        """Google has data but Windsor doesn't should return NOT_AVAILABLE."""
        status = classify_status(
            spend_delta_pct=None,
            clicks_delta_pct=None,
            impressions_delta_pct=None,
            row_count_google=100,
            row_count_windsor=0,
        )
        assert status == "NOT_AVAILABLE"

    def test_not_available_none_deltas(self):
        """None spend delta should return NOT_AVAILABLE."""
        status = classify_status(
            spend_delta_pct=None,
            clicks_delta_pct=None,
            impressions_delta_pct=None,
            row_count_google=100,
            row_count_windsor=50,
        )
        assert status == "NOT_AVAILABLE"

    def test_not_available_when_clicks_or_impressions_delta_is_none(self):
        """If any required delta is None, parity cannot be assessed."""
        status = classify_status(
            spend_delta_pct=1.0,
            clicks_delta_pct=None,
            impressions_delta_pct=1.0,
            row_count_google=100,
            row_count_windsor=100,
        )
        assert status == "NOT_AVAILABLE"

# ---------------------------------------------------------------------------
# format_delta tests
# ---------------------------------------------------------------------------

class TestFormatDelta:
    """Test format_delta function."""

    def test_none_value(self):
        assert format_delta(None) == "N/A"

    def test_positive(self):
        assert format_delta(1.8) == "+1.8%"

    def test_negative(self):
        assert format_delta(-0.4) == "-0.4%"

    def test_zero(self):
        assert format_delta(0.0) == "+0.0%"


# ---------------------------------------------------------------------------
# aggregate_metrics tests
# ---------------------------------------------------------------------------

class TestAggregateMetrics:
    """Test aggregate_metrics function."""

    def test_empty_list(self):
        result = aggregate_metrics([])
        assert result["row_count"] == 0
        assert result["spend"] == 0.0
        assert result["clicks"] == 0
        assert result["impressions"] == 0

    def test_basic_aggregation(self):
        rows = [
            {"spend": 10.0, "clicks": 5, "impressions": 100, "conversions": 1.0,
             "campaign_name": "C1", "ad_group_id": "AG1"},
            {"spend": 20.0, "clicks": 10, "impressions": 200, "conversions": 2.0,
             "campaign_name": "C1", "ad_group_id": "AG2"},
        ]
        result = aggregate_metrics(rows)
        assert result["row_count"] == 2
        assert result["spend"] == 30.0
        assert result["clicks"] == 15
        assert result["impressions"] == 300
        assert result["conversions"] == 3.0
        assert result["campaign_count"] == 1  # Same campaign
        assert result["ad_group_count"] == 2

    def test_handles_none_values(self):
        rows = [
            {"spend": None, "clicks": None, "impressions": None, "conversions": None},
        ]
        result = aggregate_metrics(rows)
        assert result["spend"] == 0.0
        assert result["clicks"] == 0
        assert result["impressions"] == 0

    def test_windsor_field_names(self):
        """Windsor uses 'campaign' and 'keyword' field names."""
        rows = [
            {"spend": 5.0, "clicks": 2, "impressions": 50, "conversions": 0.5,
             "campaign": "Camp A", "keyword": "kw1"},
        ]
        result = aggregate_metrics(rows)
        assert result["keyword_count"] == 1

    def test_campaign_count_does_not_double_count_name_and_id(self):
        rows = [
            {"spend": 1.0, "clicks": 1, "impressions": 10, "conversions": 0.0,
             "campaign_id": 123, "campaign_name": "C1"},
            {"spend": 2.0, "clicks": 2, "impressions": 20, "conversions": 0.0,
             "campaign_id": 123, "campaign_name": "C1"},
        ]
        result = aggregate_metrics(rows)
        assert result["campaign_count"] == 1

# ---------------------------------------------------------------------------
# Structural safety: No Google Ads mutate services
# ---------------------------------------------------------------------------

class TestStructuralSafety:
    """Verify the audit script contains no mutate/write operations."""

    def test_no_mutate_services_in_audit_script(self):
        """Audit script must not reference any Google Ads mutate services."""
        source = _read_source("scripts/google_ads_parity_audit.py")
        mutate_keywords = [
            "mutate",
            "MutateOperation",
            "CampaignOperation",
            "AdGroupOperation",
            "AdGroupCriterionOperation",
            "KeywordPlanOperation",
            "BiddingStrategyOperation",
            "BudgetOperation",
            "ConversionActionOperation",
            "OfflineUserDataJobOperation",
            "upload_click_conversions",
            "upload_conversion_adjustments",
        ]
        for keyword in mutate_keywords:
            assert keyword not in source, (
                f"Audit script must not reference mutate service: {keyword}"
            )

    def test_no_database_writes_in_audit_script(self):
        """Audit script must not write to the database."""
        source = _read_source("scripts/google_ads_parity_audit.py")
        db_write_patterns = [
            "INSERT INTO",
            "UPDATE ",
            "DELETE FROM",
            "CREATE TABLE",
            "DROP TABLE",
            "db.execute",
            "cursor.execute",
            "conn.execute",
            "get_conn()",
            "psycopg2",
            "sqlalchemy",
        ]
        for pattern in db_write_patterns:
            assert pattern not in source, (
                f"Audit script must not contain DB write pattern: {pattern}"
            )

    def test_no_google_ads_writes_in_connector(self):
        """Google Ads direct connector must not have mutate services."""
        source = _read_source("connectors/google_ads_direct.py")
        mutate_keywords = [
            "MutateOperation",
            "CampaignOperation",
            "AdGroupOperation",
        ]
        for keyword in mutate_keywords:
            assert keyword not in source, (
                f"Direct connector must not reference mutate: {keyword}"
            )


# ---------------------------------------------------------------------------
# Documentation validation
# ---------------------------------------------------------------------------

class TestDocumentation:
    """Validate documentation completeness."""

    def test_docs_exist(self):
        """Parity audit documentation file must exist."""
        doc_path = os.path.join(REPO_ROOT, "docs", "29_GOOGLE_ADS_WINDSOR_PARITY_AUDIT.md")
        assert os.path.exists(doc_path), "docs/29_GOOGLE_ADS_WINDSOR_PARITY_AUDIT.md must exist"

    def test_docs_mention_read_only(self):
        """Docs must mention read-only audit."""
        doc = _read_source("docs/29_GOOGLE_ADS_WINDSOR_PARITY_AUDIT.md")
        assert "read-only" in doc.lower() or "read only" in doc.lower(), (
            "Docs must mention read-only audit"
        )

    def test_docs_mention_no_production_cutover(self):
        """Docs must mention no production cutover."""
        doc = _read_source("docs/29_GOOGLE_ADS_WINDSOR_PARITY_AUDIT.md")
        assert "production" in doc.lower(), "Docs must mention production cutover"
        # Check for explicit non-production-switch language
        has_no_switch = (
            "no production source switch" in doc.lower()
            or "does not switch production" in doc.lower()
            or "not switch production" in doc.lower()
            or "no production cutover" in doc.lower()
        )
        assert has_no_switch, "Docs must explicitly state no production cutover"

    def test_docs_mention_how_to_run(self):
        """Docs must explain how to run the audit."""
        doc = _read_source("docs/29_GOOGLE_ADS_WINDSOR_PARITY_AUDIT.md")
        assert "google_ads_parity_audit" in doc, "Docs must mention how to run the script"

    def test_docs_mention_thresholds(self):
        """Docs must explain PASS/WARNING/FAIL thresholds."""
        doc = _read_source("docs/29_GOOGLE_ADS_WINDSOR_PARITY_AUDIT.md")
        assert "PASS" in doc
        assert "WARNING" in doc
        assert "FAIL" in doc

    def test_docs_mention_closed_windows(self):
        """PR-ADS-106: docs must explain closed yesterday-back windows."""
        doc = _read_source("docs/29_GOOGLE_ADS_WINDSOR_PARITY_AUDIT.md").lower()
        assert "closed" in doc
        assert "yesterday" in doc

    def test_docs_mention_conversion_value(self):
        """PR-ADS-106: docs must mention conversion value parity."""
        doc = _read_source("docs/29_GOOGLE_ADS_WINDSOR_PARITY_AUDIT.md").lower()
        assert "conversion value" in doc

    def test_docs_mention_cost_micros_normalization(self):
        """PR-ADS-106: docs must explain cost_micros normalization."""
        doc = _read_source("docs/29_GOOGLE_ADS_WINDSOR_PARITY_AUDIT.md").lower()
        assert "cost_micros" in doc
        assert "1_000_000" in doc or "1,000,000" in doc

    def test_docs_mention_acceptable_difference_categories(self):
        """PR-ADS-106: docs must explain each acceptable-difference category."""
        doc = _read_source("docs/29_GOOGLE_ADS_WINDSOR_PARITY_AUDIT.md").lower()
        for term in (
            "privacy",
            "timezone",
            "status filtering",
            "attribution",
            "search-term visibility",
        ):
            assert term in doc, f"Docs must explain acceptable difference: {term}"


# ---------------------------------------------------------------------------
# PR-ADS-106: cost_micros normalization
# ---------------------------------------------------------------------------

class TestCostMicrosNormalization:
    """Spend must be normalized from cost_micros when no spend field exists."""

    def test_row_spend_prefers_spend_field(self):
        row = {"spend": 2.0, "cost_micros": 999_000_000}
        assert _row_spend(row, "spend") == 2.0

    def test_row_spend_falls_back_to_cost_micros(self):
        row = {"cost_micros": 1_500_000}
        assert _row_spend(row, "spend") == pytest.approx(1.5)

    def test_row_spend_zero_when_no_signal(self):
        assert _row_spend({"clicks": 3}, "spend") == 0.0

    def test_aggregate_normalizes_cost_micros(self):
        rows = [
            {"cost_micros": 1_500_000, "clicks": 1, "impressions": 10},
            {"cost_micros": 2_500_000, "clicks": 2, "impressions": 20},
        ]
        result = aggregate_metrics(rows)
        assert result["spend"] == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# PR-ADS-106: conversion value aggregation
# ---------------------------------------------------------------------------

class TestConversionValueAggregation:
    """Conversion value must aggregate from either field name, where available."""

    def test_conversions_value_field(self):
        result = aggregate_metrics([{"conversions_value": 100.0}, {"conversions_value": 50.0}])
        assert result["conversion_value"] == pytest.approx(150.0)
        assert result["has_conversion_value"] is True

    def test_conversion_value_alt_field(self):
        result = aggregate_metrics([{"conversion_value": 25.0}])
        assert result["conversion_value"] == pytest.approx(25.0)
        assert result["has_conversion_value"] is True

    def test_absent_conversion_value(self):
        result = aggregate_metrics([{"spend": 1.0, "clicks": 1}])
        assert result["conversion_value"] == 0.0
        assert result["has_conversion_value"] is False

    def test_row_conversion_value_helper(self):
        assert _row_conversion_value({"conversions_value": 9.0}) == 9.0
        assert _row_conversion_value({"conversion_value": 4.0}) == 4.0
        assert _row_conversion_value({"spend": 1.0}) == 0.0


# ---------------------------------------------------------------------------
# PR-ADS-106: closed (yesterday-back, UTC) date windows
# ---------------------------------------------------------------------------

class TestClosedWindows:
    """run_audit must use closed windows that exclude today."""

    def _patch_sources(self, monkeypatch):
        monkeypatch.setattr(
            audit, "fetch_google_ads_data",
            lambda dataset, start, end: {"rows": [], "error": None},
        )
        monkeypatch.setattr(
            audit, "fetch_windsor_data",
            lambda dataset, start, end, days_back: {
                "rows": [], "error": None, "window_mode": "closed_range",
            },
        )

    def test_window_end_is_yesterday_utc(self, monkeypatch):
        self._patch_sources(monkeypatch)
        results = run_audit(windows=[7], datasets=["campaigns"])
        today = datetime.now(timezone.utc).date()
        assert results[0]["window_end"] == str(today - timedelta(days=1))

    def test_window_does_not_include_today(self, monkeypatch):
        self._patch_sources(monkeypatch)
        results = run_audit(windows=[30], datasets=["campaigns"])
        today = datetime.now(timezone.utc).date()
        assert results[0]["window_end"] != str(today)

    def test_window_span_matches_days_back(self, monkeypatch):
        self._patch_sources(monkeypatch)
        for days_back in (7, 30, 60):
            results = run_audit(windows=[days_back], datasets=["campaigns"])
            start = datetime.fromisoformat(results[0]["window_start"]).date()
            end = datetime.fromisoformat(results[0]["window_end"]).date()
            # Inclusive span: end - start + 1 == days_back
            assert (end - start).days + 1 == days_back


# ---------------------------------------------------------------------------
# PR-ADS-106: Windsor closed-window range fetch vs preset search terms
# ---------------------------------------------------------------------------

class TestWindsorClosedWindowFetch:
    """Campaigns/keywords/geo use explicit-range pulls; search terms use preset."""

    def test_campaigns_use_explicit_range(self, monkeypatch):
        import connectors.windsor_pull as wp
        captured = {}
        monkeypatch.setattr(
            wp, "pull_campaign_performance_range",
            lambda date_from, date_to: captured.update(args=(date_from, date_to)) or [],
        )
        result = fetch_windsor_data("campaigns", "2026-06-08", "2026-06-14", 7)
        assert result["window_mode"] == "closed_range"
        assert captured["args"] == ("2026-06-08", "2026-06-14")

    def test_geo_and_keywords_use_explicit_range(self, monkeypatch):
        import connectors.windsor_pull as wp
        monkeypatch.setattr(wp, "pull_keyword_performance_range", lambda f, t: [])
        monkeypatch.setattr(wp, "pull_geo_performance_range", lambda f, t: [])
        assert fetch_windsor_data("keywords", "2026-06-08", "2026-06-14", 7)["window_mode"] == "closed_range"
        assert fetch_windsor_data("geo", "2026-06-08", "2026-06-14", 7)["window_mode"] == "closed_range"

    def test_search_terms_use_preset(self, monkeypatch):
        import connectors.windsor_pull as wp
        captured = {}
        monkeypatch.setattr(
            wp, "pull_search_terms",
            lambda days_back=0: captured.update(days_back=days_back) or [],
        )
        result = fetch_windsor_data("search_terms", "2026-06-08", "2026-06-14", 7)
        assert result["window_mode"] == "preset"
        assert captured["days_back"] == 7


# ---------------------------------------------------------------------------
# PR-ADS-106: no datetime.utcnow() (deprecation) in audit or smoke test
# ---------------------------------------------------------------------------

class TestNoDeprecatedUtcnow:
    def test_audit_script_has_no_utcnow(self):
        source = _read_source("scripts/google_ads_parity_audit.py")
        assert "utcnow" not in source, "Use datetime.now(timezone.utc), not utcnow()"

    def test_smoke_test_has_no_utcnow(self):
        source = _read_source("scripts/google_ads_smoke_test.py")
        assert "utcnow" not in source, "Use datetime.now(timezone.utc), not utcnow()"
