"""
Tests for PR-ADS-099: Google Ads API vs Windsor Dataset Parity Audit

Validates:
- Parity math functions
- Percent delta handles zero safely
- Status classification logic
- No Google Ads mutate services referenced
- No database writes in audit script
- Docs mention read-only audit and no production cutover
"""

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from scripts.google_ads_parity_audit import (
    percent_delta,
    classify_status,
    format_delta,
    aggregate_metrics,
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
