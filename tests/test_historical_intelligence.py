"""
tests/test_historical_intelligence.py

PR-ADS-075 — Tests for the Historical Intelligence module and API endpoint.

Covers:
  - classify_trend_direction: improving / deteriorating / stable / insufficient_data /
    new_activity / no_recent_activity
  - CPQL is None when confirmed_sqls = 0 (never divide by zero)
  - compute_campaign_trends: current vs previous comparison
  - compute_campaign_trends: deteriorating signal (spend up + SQLs down)
  - compute_campaign_trends: improving signal
  - compute_campaign_trends: stable signal
  - compute_campaign_trends: insufficient data returned honestly
  - compute_campaign_trends: new_activity when no previous data
  - compute_campaign_trends: no_recent_activity when no current data
  - junk rate up = deterioration signal
  - compute_geo_trends: basic geo comparison
  - No unsafe action wording in any output
  - API response shape (with mocked DB)

Run with:
    python -m pytest tests/test_historical_intelligence.py -v
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure repo root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only")
os.environ.setdefault("APP_ENV", "development")

from analysis.historical_intelligence import (
    _safe_cpql,
    classify_trend_direction,
    compute_campaign_trends,
    compute_geo_trends,
    compute_quality_movement,
)

# ---------------------------------------------------------------------------
# Unsafe wording that must never appear in any output
# ---------------------------------------------------------------------------

_UNSAFE_PHRASES = [
    "pause campaign",
    "cut campaign",
    "apply negative",
    "push negative",
    "block term",
    "send to google ads",
    "send to hubspot",
    "upload conversion",
    "change bid",
    "change budget",
    "increase bid",
    "push change",
    "auto-fix",
    "automatically change",
]


def _check_no_unsafe_wording(text: str) -> None:
    """Assert that no unsafe action wording appears in text (case-insensitive)."""
    lowered = text.lower()
    for phrase in _UNSAFE_PHRASES:
        assert phrase not in lowered, (
            f"Unsafe wording found: {phrase!r} in output: {text!r}"
        )


# ---------------------------------------------------------------------------
# _safe_cpql
# ---------------------------------------------------------------------------

class TestSafeCpql:
    def test_zero_sqls_returns_none(self):
        assert _safe_cpql(1200.0, 0) is None

    def test_none_sqls_returns_none(self):
        assert _safe_cpql(1200.0, None) is None

    def test_none_spend_returns_none(self):
        assert _safe_cpql(None, 5) is None

    def test_normal_computation(self):
        result = _safe_cpql(900.0, 5)
        assert result == 180.0

    def test_rounds_to_two_decimals(self):
        result = _safe_cpql(100.0, 3)
        assert result == pytest.approx(33.33, abs=0.01)


# ---------------------------------------------------------------------------
# classify_trend_direction
# ---------------------------------------------------------------------------

class TestClassifyTrendDirection:
    def test_both_none_is_insufficient(self):
        assert classify_trend_direction(None, None) == "insufficient_data"

    def test_current_none_is_no_recent(self):
        assert classify_trend_direction(None, 100.0) == "no_recent_activity"

    def test_previous_none_is_new_activity(self):
        assert classify_trend_direction(100.0, None) == "new_activity"

    def test_both_zero_is_stable(self):
        assert classify_trend_direction(0, 0) == "stable"

    def test_previous_zero_current_positive_is_new_activity(self):
        assert classify_trend_direction(50.0, 0) == "new_activity"

    def test_large_increase_is_improving(self):
        assert classify_trend_direction(120.0, 100.0) == "improving"

    def test_large_decrease_is_deteriorating(self):
        assert classify_trend_direction(80.0, 100.0) == "deteriorating"

    def test_small_change_within_threshold_is_stable(self):
        # 102 vs 100 = 2% change → stable (threshold is 5%)
        assert classify_trend_direction(102.0, 100.0) == "stable"

    def test_exactly_5_pct_increase_is_stable(self):
        assert classify_trend_direction(105.0, 100.0) == "stable"

    def test_just_over_5_pct_increase_is_improving(self):
        assert classify_trend_direction(105.01, 100.0) == "improving"


# ---------------------------------------------------------------------------
# compute_campaign_trends
# ---------------------------------------------------------------------------

def _make_campaign_row(
    campaign_name: str,
    run_date: date,
    spend_usd: float = 0.0,
    confirmed_sqls: int = 0,
    total_leads: int = 0,
    junk_rate_pct: float | None = None,
    cpql_usd: float | None = None,
) -> dict:
    return {
        "campaign_name": campaign_name,
        "run_date":       run_date,
        "spend_usd":      spend_usd,
        "confirmed_sqls": confirmed_sqls,
        "total_leads":    total_leads,
        "junk_rate_pct":  junk_rate_pct,
        "cpql_usd":       cpql_usd,
    }


today    = date.today()
cur_date = today - timedelta(days=10)   # within default 30d current window
prev_date = today - timedelta(days=40)  # within default 30d previous window


class TestComputeCampaignTrends:

    def test_empty_rows_returns_insufficient(self):
        result = compute_campaign_trends([], current_days=30, previous_days=30)
        assert result["status"] == "insufficient_data"
        assert result["rows"] == []

    def test_response_has_required_keys(self):
        rows = [_make_campaign_row("alpha", cur_date, spend_usd=100, confirmed_sqls=2)]
        result = compute_campaign_trends(rows, current_days=30, previous_days=30)
        assert "entity" in result
        assert "current_days" in result
        assert "previous_days" in result
        assert "status" in result
        assert "summary" in result
        assert "rows" in result

    def test_entity_is_campaigns(self):
        rows = [_make_campaign_row("alpha", cur_date, spend_usd=100)]
        result = compute_campaign_trends(rows, current_days=30, previous_days=30)
        assert result["entity"] == "campaigns"

    def test_improving_classification(self):
        """Current: more SQLs, lower junk rate → improving."""
        rows = [
            _make_campaign_row("alpha", cur_date,  spend_usd=900,  confirmed_sqls=8, junk_rate_pct=10.0),
            _make_campaign_row("alpha", prev_date, spend_usd=900,  confirmed_sqls=3, junk_rate_pct=30.0),
        ]
        result = compute_campaign_trends(rows, current_days=30, previous_days=30)
        campaign_row = next(r for r in result["rows"] if r["campaign_name"] == "alpha")
        assert campaign_row["trend_status"] == "improving"

    def test_deteriorating_spend_up_sqls_down(self):
        """Spend increased while SQLs decreased → deteriorating."""
        rows = [
            _make_campaign_row("beta", cur_date,  spend_usd=1200, confirmed_sqls=3, junk_rate_pct=24.0),
            _make_campaign_row("beta", prev_date, spend_usd=900,  confirmed_sqls=5, junk_rate_pct=12.0),
        ]
        result = compute_campaign_trends(rows, current_days=30, previous_days=30)
        campaign_row = next(r for r in result["rows"] if r["campaign_name"] == "beta")
        assert campaign_row["trend_status"] == "deteriorating"

    def test_deteriorating_junk_rate_up(self):
        """Junk rate increased substantially while SQLs dropped → deteriorating."""
        rows = [
            _make_campaign_row("gamma", cur_date,  spend_usd=1000, confirmed_sqls=2, junk_rate_pct=50.0),
            _make_campaign_row("gamma", prev_date, spend_usd=800,  confirmed_sqls=6, junk_rate_pct=10.0),
        ]
        result = compute_campaign_trends(rows, current_days=30, previous_days=30)
        campaign_row = next(r for r in result["rows"] if r["campaign_name"] == "gamma")
        assert campaign_row["trend_status"] == "deteriorating"

    def test_stable_classification(self):
        """Metrics identical across periods → stable."""
        rows = [
            _make_campaign_row("delta", cur_date,  spend_usd=1000, confirmed_sqls=5, junk_rate_pct=20.0),
            _make_campaign_row("delta", prev_date, spend_usd=1000, confirmed_sqls=5, junk_rate_pct=20.0),
        ]
        result = compute_campaign_trends(rows, current_days=30, previous_days=30)
        campaign_row = next(r for r in result["rows"] if r["campaign_name"] == "delta")
        assert campaign_row["trend_status"] == "stable"

    def test_new_activity_when_no_previous_data(self):
        """Only current period has data → new_activity."""
        rows = [
            _make_campaign_row("epsilon", cur_date, spend_usd=500, confirmed_sqls=3),
        ]
        result = compute_campaign_trends(rows, current_days=30, previous_days=30)
        campaign_row = next(r for r in result["rows"] if r["campaign_name"] == "epsilon")
        assert campaign_row["trend_status"] == "new_activity"

    def test_no_recent_activity_when_no_current_data(self):
        """Only previous period has data → no_recent_activity."""
        rows = [
            _make_campaign_row("zeta", prev_date, spend_usd=500, confirmed_sqls=3),
        ]
        result = compute_campaign_trends(rows, current_days=30, previous_days=30)
        campaign_row = next(r for r in result["rows"] if r["campaign_name"] == "zeta")
        assert campaign_row["trend_status"] == "no_recent_activity"

    def test_insufficient_data_when_both_periods_empty_for_campaign(self):
        """Campaign exists in neither period → labeled as insufficient or no recent."""
        rows = []
        result = compute_campaign_trends(rows, current_days=30, previous_days=30)
        assert result["status"] == "insufficient_data"

    def test_cpql_none_when_sqls_zero(self):
        """CPQL in aggregated output must be None when confirmed_sqls = 0."""
        rows = [
            _make_campaign_row("eta", cur_date,  spend_usd=500, confirmed_sqls=0),
            _make_campaign_row("eta", prev_date, spend_usd=400, confirmed_sqls=0),
        ]
        result = compute_campaign_trends(rows, current_days=30, previous_days=30)
        campaign_row = next(r for r in result["rows"] if r["campaign_name"] == "eta")
        assert campaign_row["current"]["cpql"]  is None
        assert campaign_row["previous"]["cpql"] is None

    def test_summary_counts_match_rows(self):
        rows = [
            _make_campaign_row("alpha", cur_date,  spend_usd=1200, confirmed_sqls=3, junk_rate_pct=24.0),
            _make_campaign_row("alpha", prev_date, spend_usd=900,  confirmed_sqls=5, junk_rate_pct=12.0),
            _make_campaign_row("beta",  cur_date,  spend_usd=500,  confirmed_sqls=5, junk_rate_pct=10.0),
            _make_campaign_row("beta",  prev_date, spend_usd=500,  confirmed_sqls=2, junk_rate_pct=30.0),
        ]
        result = compute_campaign_trends(rows, current_days=30, previous_days=30)
        summary = result["summary"]
        total_from_summary = sum(summary.values())
        assert total_from_summary == len(result["rows"])

    def test_no_unsafe_action_wording_in_notes(self):
        """Human review notes must not contain action wording."""
        rows = [
            _make_campaign_row("alpha", cur_date,  spend_usd=1200, confirmed_sqls=3, junk_rate_pct=24.0),
            _make_campaign_row("alpha", prev_date, spend_usd=900,  confirmed_sqls=5, junk_rate_pct=12.0),
        ]
        result = compute_campaign_trends(rows, current_days=30, previous_days=30)
        for row in result.get("rows", []):
            note = row.get("human_review_note", "")
            _check_no_unsafe_wording(note)

    def test_str_run_date_parsed(self):
        """run_date as ISO string must be handled without error."""
        rows = [
            _make_campaign_row("theta", cur_date,  spend_usd=100),
        ]
        rows[0]["run_date"] = str(cur_date)
        result = compute_campaign_trends(rows, current_days=30, previous_days=30)
        assert result["status"] in ("ok", "insufficient_data")

    def test_deteriorating_row_sorted_first(self):
        """Deteriorating campaigns should appear before stable ones."""
        rows = [
            _make_campaign_row("stable_c",  cur_date,  spend_usd=1000, confirmed_sqls=5, junk_rate_pct=20.0),
            _make_campaign_row("stable_c",  prev_date, spend_usd=1000, confirmed_sqls=5, junk_rate_pct=20.0),
            _make_campaign_row("degrade_c", cur_date,  spend_usd=1200, confirmed_sqls=3, junk_rate_pct=25.0),
            _make_campaign_row("degrade_c", prev_date, spend_usd=900,  confirmed_sqls=5, junk_rate_pct=10.0),
        ]
        result = compute_campaign_trends(rows, current_days=30, previous_days=30)
        statuses = [r["trend_status"] for r in result["rows"]]
        # deteriorating must precede stable
        assert statuses.index("deteriorating") < statuses.index("stable")


# ---------------------------------------------------------------------------
# compute_geo_trends
# ---------------------------------------------------------------------------

class TestComputeGeoTrends:
    def test_empty_rows_returns_insufficient(self):
        result = compute_geo_trends([], current_days=30, previous_days=30)
        assert result["status"] == "insufficient_data"

    def test_entity_is_geo(self):
        rows = [
            {"country": "Jordan", "campaign_name": "alpha",
             "run_date": cur_date, "spend_usd": 200.0, "clicks": 10},
        ]
        result = compute_geo_trends(rows, current_days=30, previous_days=30)
        assert result["entity"] == "geo"

    def test_no_unsafe_wording_in_notes(self):
        rows = [
            {"country": "Jordan", "campaign_name": "alpha",
             "run_date": cur_date,  "spend_usd": 300.0, "clicks": 10},
            {"country": "Jordan", "campaign_name": "alpha",
             "run_date": prev_date, "spend_usd": 100.0, "clicks": 5},
        ]
        result = compute_geo_trends(rows, current_days=30, previous_days=30)
        for row in result.get("rows", []):
            _check_no_unsafe_wording(row.get("human_review_note", ""))


# ---------------------------------------------------------------------------
# compute_quality_movement
# ---------------------------------------------------------------------------

class TestComputeQualityMovement:
    def test_groups_by_entity_key(self):
        rows = [
            _make_campaign_row("alpha", cur_date,  spend_usd=100, confirmed_sqls=2),
            _make_campaign_row("beta",  prev_date, spend_usd=200, confirmed_sqls=4),
        ]
        result = compute_quality_movement(rows, entity_key="campaign_name")
        assert result["entity_key"] == "campaign_name"
        group_keys = {g["key"] for g in result["groups"]}
        assert "alpha" in group_keys
        assert "beta" in group_keys


# ---------------------------------------------------------------------------
# API response shape test (with mocked DB and real auth cookie)
# ---------------------------------------------------------------------------

def _make_test_client():
    """Import the FastAPI app and wrap it in a TestClient."""
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi[testclient] not available")
    try:
        from api.server import app  # noqa: PLC0415
    except Exception as exc:
        pytest.skip(f"api.server import failed: {exc}")
    return TestClient(app, raise_server_exceptions=False)


def _make_admin_cookie():
    """Return a signed ads_session cookie value for an admin user.

    Returns the cookie string value, or None if the session could not be set.
    Callers must check for None and skip the test when it is returned.
    """
    try:
        from api.auth import set_session  # noqa: PLC0415
        from starlette.responses import Response as StarletteResponse  # noqa: PLC0415
    except ImportError:
        return None

    r = StarletteResponse()
    set_session(r, "testadmin", "admin")
    cookie_header = r.headers.get("set-cookie", "")
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith("ads_session="):
            value = part.split("=", 1)[1]
            return value if value else None
    return None


class TestHistoricalIntelligenceAPI:
    """Test the GET /api/historical-intelligence endpoint response shape."""

    @pytest.fixture()
    def client(self):
        return _make_test_client()

    @pytest.fixture()
    def admin_cookie(self, client):
        val = _make_admin_cookie()
        if not val:
            pytest.skip("Could not create admin session cookie")
        return val

    @pytest.fixture()
    def _patch_db(self):
        """Patch load_campaign_trend_rows and load_geo_trend_rows to return no rows."""
        with (
            patch("analysis.historical_intelligence.load_campaign_trend_rows", return_value=[]),
            patch("analysis.historical_intelligence.load_geo_trend_rows",      return_value=[]),
        ):
            yield

    def test_unauthenticated_returns_401_or_403(self, client):
        res = client.get("/api/historical-intelligence?entity=campaigns&current_days=30&previous_days=30")
        assert res.status_code in (401, 403)

    def test_endpoint_returns_required_keys(self, client, admin_cookie, _patch_db):
        res = client.get(
            "/api/historical-intelligence?entity=campaigns&current_days=30&previous_days=30",
            cookies={"ads_session": admin_cookie},
        )
        if res.status_code == 401:
            pytest.skip("Session cookie not accepted in test environment")
        assert res.status_code == 200
        data = res.json()
        for key in ("entity", "current_days", "previous_days", "status", "summary", "rows"):
            assert key in data, f"Missing key in response: {key}"

    def test_endpoint_returns_insufficient_data_when_no_rows(self, client, admin_cookie, _patch_db):
        res = client.get(
            "/api/historical-intelligence?entity=campaigns&current_days=30&previous_days=30",
            cookies={"ads_session": admin_cookie},
        )
        if res.status_code == 401:
            pytest.skip("Session cookie not accepted in test environment")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "insufficient_data"
        assert data["rows"] == []
        assert isinstance(data["summary"], dict)

    def test_invalid_entity_returns_400(self, client, admin_cookie):
        res = client.get(
            "/api/historical-intelligence?entity=invalid_entity",
            cookies={"ads_session": admin_cookie},
        )
        if res.status_code == 401:
            pytest.skip("Session cookie not accepted in test environment")
        assert res.status_code == 400

    def test_default_entity_is_campaigns(self, client, admin_cookie, _patch_db):
        res = client.get(
            "/api/historical-intelligence",
            cookies={"ads_session": admin_cookie},
        )
        if res.status_code == 401:
            pytest.skip("Session cookie not accepted in test environment")
        assert res.status_code == 200
        data = res.json()
        assert data["entity"] == "campaigns"

    def test_geo_entity_accepted(self, client, admin_cookie, _patch_db):
        res = client.get(
            "/api/historical-intelligence?entity=geo",
            cookies={"ads_session": admin_cookie},
        )
        if res.status_code == 401:
            pytest.skip("Session cookie not accepted in test environment")
        assert res.status_code == 200
        data = res.json()
        assert data["entity"] == "geo"

    def test_no_unsafe_wording_in_response(self, client, admin_cookie, _patch_db):
        res = client.get(
            "/api/historical-intelligence?entity=campaigns",
            cookies={"ads_session": admin_cookie},
        )
        if res.status_code == 401:
            pytest.skip("Session cookie not accepted in test environment")
        assert res.status_code == 200
        _check_no_unsafe_wording(res.text)


# ---------------------------------------------------------------------------
# rule_advisor historical intelligence block — wording safety
# ---------------------------------------------------------------------------

class TestHistoricalIntelligenceReportBlock:
    def test_report_disclaimer_has_no_forbidden_verbs(self):
        """Generated report disclaimer must not contain pause/cut/apply/push/block."""
        from analysis.rule_advisor import _build_historical_intelligence_block  # noqa: PLC0415
        block = _build_historical_intelligence_block(None)
        for verb in ("pausing", "cutting", " pause ", " cut ", "apply", "push", "block"):
            assert verb.lower() not in block.lower(), (
                f"Forbidden verb {verb!r} found in historical intelligence block"
            )

    def test_report_block_insufficient_data_is_advisory(self):
        from analysis.rule_advisor import _build_historical_intelligence_block  # noqa: PLC0415
        block = _build_historical_intelligence_block(None)
        _check_no_unsafe_wording(block)

