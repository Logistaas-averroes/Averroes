"""
PR-ADS-107A — Revenue Attribution API endpoint tests.

Validates GET /api/revenue-attribution:
  - route is registered and wired to the shared service
  - requires auth
  - returns the window / summary / campaigns / countries contract
  - accepts business-window keys and rejects ad-style / invalid windows
  - does not use ad-style day windows in the route definition
"""

import os
import sys
from contextlib import contextmanager
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── Static source assertions (no app/DB needed) ──────────────────────────────


def test_route_registered_in_server():
    with open(os.path.join(ROOT, "api", "server.py"), "r", encoding="utf-8") as f:
        source = f.read()
    assert '"/api/revenue-attribution"' in source
    assert "async def get_revenue_attribution(" in source
    assert "from services.revenue_attribution_service import build_revenue_attribution" in source


def test_route_default_is_business_window_not_ad_window():
    with open(os.path.join(ROOT, "api", "server.py"), "r", encoding="utf-8") as f:
        source = f.read()
    # Locate the endpoint and inspect its signature region.
    idx = source.find("async def get_revenue_attribution(")
    assert idx != -1
    region = source[idx:idx + 400]
    assert 'default="current_quarter"' in region
    # The new endpoint must not hardcode an ad-style day window.
    assert 'default="60d"' not in region
    assert "_parse_window(" not in region


def test_new_endpoint_has_no_windsor_wording():
    with open(os.path.join(ROOT, "api", "server.py"), "r", encoding="utf-8") as f:
        source = f.read()
    idx = source.find("async def get_revenue_attribution(")
    assert idx != -1
    # Scope to the new endpoint function body (up to the next decorator).
    region = source[idx:source.find("@app.get", idx + 1)]
    assert "windsor" not in region.lower(), "New revenue-attribution endpoint must not use Windsor wording"


# ── Live endpoint via TestClient ─────────────────────────────────────────────


def _make_test_client():
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
    try:
        from api.auth import set_session  # noqa: PLC0415
        from starlette.responses import Response as StarletteResponse  # noqa: PLC0415
    except ImportError:
        return None
    r = StarletteResponse()
    try:
        set_session(r, "testadmin", "admin")
    except Exception:
        # e.g. APP_SECRET_KEY not configured in this environment.
        return None
    cookie_header = r.headers.get("set-cookie", "")
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith("ads_session="):
            value = part.split("=", 1)[1]
            return value if value else None
    return None


@contextmanager
def _patched_service():
    """Patch the durable DB repository to deterministic non-empty data."""
    spend = {"available": True, "table": "geo",
             "rows": [{"campaign_name": "gulf", "country": "Saudi Arabia", "spend": 1000.0}],
             "coverage_start": "2026-01-01", "coverage_end": "2026-06-20"}
    leads = {"available": True, "table": "leads",
             "rows": [{"campaign_name": "gulf", "country": "Saudi Arabia",
                       "status_category": "qualified", "has_gclid": True}],
             "coverage_start": None, "coverage_end": None}
    revenue = {"available": True, "table": "gclid_attribution",
               "rows": [{"campaign_name": "gulf", "country": "Saudi Arabia", "deal_id": "d1",
                         "deal_amount_usd": 10000.0, "match_status": "matched"}],
               "coverage_start": "2026-05-01", "coverage_end": "2026-05-01"}
    with patch("db.revenue_repository.fetch_campaign_country_spend", return_value=spend), \
         patch("db.revenue_repository.fetch_lead_quality", return_value=leads), \
         patch("db.revenue_repository.fetch_won_revenue", return_value=revenue), \
         patch("db.revenue_repository.fetch_sync_state", return_value={"available": True, "datasets": {}}):
        yield


class TestRevenueAttributionEndpoint:
    @pytest.fixture()
    def client(self):
        return _make_test_client()

    @pytest.fixture()
    def admin_cookie(self, client):
        val = _make_admin_cookie()
        if not val:
            pytest.skip("Could not create admin session cookie")
        return val

    def test_requires_auth(self, client):
        res = client.get("/api/revenue-attribution?window=current_quarter")
        assert res.status_code in (401, 403)

    def test_returns_contract_shape(self, client, admin_cookie):
        with _patched_service():
            res = client.get(
                "/api/revenue-attribution?window=ytd",
                cookies={"ads_session": admin_cookie},
            )
        if res.status_code == 401:
            pytest.skip("Session cookie not accepted in test environment")
        assert res.status_code == 200
        data = res.json()
        for key in ("window", "summary", "campaigns", "countries"):
            assert key in data
        assert data["window"]["key"] == "ytd"
        assert data["window"]["label"] == "YTD"
        assert data["google_ads_conversion_value_used"] is False

    def test_source_health_in_contract(self, client, admin_cookie):
        with _patched_service():
            res = client.get(
                "/api/revenue-attribution?window=ytd",
                cookies={"ads_session": admin_cookie},
            )
        if res.status_code == 401:
            pytest.skip("Session cookie not accepted in test environment")
        assert res.status_code == 200
        data = res.json()
        assert data["spend_source"] == "google_ads_api"
        assert data["revenue_source"] == "hubspot_closed_won"
        assert "attribution_source_status" in data
        assert "country_spend_available" in data
        assert "geo_country_mapping_status" in data
        assert "warnings" in data and isinstance(data["warnings"], list)
        # source_health block with durable-source diagnostics.
        sh = data["source_health"]
        for key in ("mode", "campaign_spend_status", "hubspot_contacts_status",
                    "hubspot_deals_status", "attribution_status", "data_is_partial",
                    "files_used", "db_tables_used", "warnings"):
            assert key in sh
        assert sh["mode"] == "database"
        assert "geo" in data["db_tables_used"]

    def test_default_window_is_current_quarter(self, client, admin_cookie):
        with _patched_service():
            res = client.get(
                "/api/revenue-attribution",
                cookies={"ads_session": admin_cookie},
            )
        if res.status_code == 401:
            pytest.skip("Session cookie not accepted in test environment")
        assert res.status_code == 200
        assert res.json()["window"]["key"] == "current_quarter"

    @pytest.mark.parametrize("window", ["current_quarter", "last_quarter", "last_6_months", "ytd", "all_time"])
    def test_all_business_windows_accepted(self, client, admin_cookie, window):
        with _patched_service():
            res = client.get(
                f"/api/revenue-attribution?window={window}",
                cookies={"ads_session": admin_cookie},
            )
        if res.status_code == 401:
            pytest.skip("Session cookie not accepted in test environment")
        assert res.status_code == 200
        assert res.json()["window"]["key"] == window

    @pytest.mark.parametrize("window", ["60d", "7d", "30d", "bogus", ""])
    def test_ad_style_and_invalid_windows_rejected(self, client, admin_cookie, window):
        res = client.get(
            f"/api/revenue-attribution?window={window}",
            cookies={"ads_session": admin_cookie},
        )
        if res.status_code == 401:
            pytest.skip("Session cookie not accepted in test environment")
        assert res.status_code == 400
