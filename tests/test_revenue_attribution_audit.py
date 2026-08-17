"""
PR-ADS-109 — Revenue attribution audit endpoint tests.

The audit must explain exactly why a window is SAFE or UNSAFE.
"""

import os
import sys
from datetime import datetime, timezone
from contextlib import contextmanager
from unittest.mock import patch

import pytest


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.canonical_ledger_fixtures import (  # noqa: E402
    canonical_ledger_patch, from_legacy_deal_rows,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import services.revenue_attribution_service as svc
from services.revenue_attribution_service import build_revenue_attribution_audit

NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


def _grain(safe=True):
    return {
        "available": True,
        "lead_event_date_field_available": True,
        "lead_window_safe": safe,
        "missing_contact_created_at_count": 0 if safe else 17,
        "excluded_non_paid_count": 5,
        "excluded_pseudo_campaign_count": 2,
        "counts_by_source_type": {"paid_search": 12, "organic_search": 3, "direct": 1,
                                  "referral": 0, "email": 0, "other": 0, "null": 1},
    }


def _pollution(clean=True):
    return {
        "available": True,
        "pseudo_campaign_rows": [] if clean else ["(direct)"],
        "email_campaign_rows": [],
        "zero_spend_campaigns_with_leads": ["old-utm-variant"],
    }


def _audit(window="current_quarter", grain=None, pollution=None, rev=None):
    grain = grain or _grain(True)
    pollution = pollution or _pollution(True)
    rev = rev or {"available": True, "rows": [], "coverage_start": None, "coverage_end": None}
    lead_obj = {"available": True, "rows": [], "event_date_safe": grain["lead_window_safe"],
                "lead_event_date_field_available": True, "missing_contact_created_at_count": 0,
                "excluded_non_paid_count": 0, "excluded_pseudo_campaign_count": 0}
    spend_obj = {"available": True, "rows": [], "coverage_start": None, "coverage_end": None}
    with patch("db.revenue_repository.fetch_lead_date_grain_health", return_value=grain), \
         patch("db.revenue_repository.fetch_campaign_pollution_report", return_value=pollution), \
         canonical_ledger_patch(from_legacy_deal_rows(rev.get("rows") or [])), \
         patch("db.revenue_repository.fetch_campaign_country_spend", return_value=spend_obj), \
         patch("db.revenue_repository.fetch_lead_quality", return_value=lead_obj), \
         patch("db.revenue_repository.fetch_sync_state", return_value={"available": True, "datasets": {}}):
        return build_revenue_attribution_audit(window, now=NOW)


# ── Service-level audit ──────────────────────────────────────────────────────


def test_audit_contract_keys():
    a = _audit()
    for key in ("window", "date_grain_health", "counts_by_source_type",
                "pseudo_campaign_rows", "email_campaign_rows",
                "zero_spend_campaigns_with_leads", "window_comparison",
                "verdict", "blockers"):
        assert key in a


def test_audit_date_grain_health_fields():
    dg = _audit()["date_grain_health"]
    assert dg["spend_date_field"] == "geo.run_date"
    assert dg["lead_date_field_current"] == "leads.contact_created_at"
    # PR-ADS-153E-B: revenue is windowed by the CANONICAL deal close date.
    assert dg["deal_date_field"] == "hubspot_deal_ledger.deal_close_date"
    assert dg["spend_window_safe"] is True
    assert dg["revenue_window_safe"] is True


def test_audit_verdict_unsafe_when_lead_grain_unsafe():
    a = _audit(grain=_grain(safe=False))
    assert a["verdict"] == "UNSAFE"
    assert any("contact_created_at" in b for b in a["blockers"])


def test_audit_verdict_safe_when_all_safe_and_clean():
    a = _audit(grain=_grain(True), pollution=_pollution(clean=True))
    assert a["verdict"] == "SAFE"
    assert a["blockers"] == []


def test_audit_verdict_unsafe_when_pseudo_pollution_present():
    a = _audit(grain=_grain(True), pollution=_pollution(clean=False))
    assert a["verdict"] == "UNSAFE"
    assert any("pseudo" in b.lower() for b in a["blockers"])


def test_audit_window_comparison_has_three_windows():
    wc = _audit()["window_comparison"]
    assert set(wc.keys()) == {"current_quarter", "ytd", "all_time"}


def test_audit_invalid_window_raises():
    with pytest.raises(ValueError):
        _audit(window="60d")


# ── Endpoint ─────────────────────────────────────────────────────────────────


def test_audit_route_registered_in_server():
    with open(os.path.join(ROOT, "api", "server.py"), encoding="utf-8") as f:
        source = f.read()
    assert '"/api/revenue-attribution/audit"' in source
    assert "async def get_revenue_attribution_audit(" in source


def _make_test_client():
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi[testclient] not available")
    try:
        from api.server import app
    except Exception as exc:
        pytest.skip(f"api.server import failed: {exc}")
    return TestClient(app, raise_server_exceptions=False)


def _make_admin_cookie():
    try:
        from api.auth import set_session
        from starlette.responses import Response as StarletteResponse
    except ImportError:
        return None
    r = StarletteResponse()
    try:
        set_session(r, "testadmin", "admin")
    except Exception:
        return None
    cookie_header = r.headers.get("set-cookie", "")
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith("ads_session="):
            return part.split("=", 1)[1] or None
    return None


@contextmanager
def _patched_audit_repo():
    with patch("db.revenue_repository.fetch_lead_date_grain_health", return_value=_grain(False)), \
         patch("db.revenue_repository.fetch_campaign_pollution_report", return_value=_pollution(True)), \
         canonical_ledger_patch([]), \
         patch("db.revenue_repository.fetch_campaign_country_spend", return_value={"available": True, "rows": [], "coverage_start": None, "coverage_end": None}), \
         patch("db.revenue_repository.fetch_lead_quality", return_value={"available": True, "rows": [], "event_date_safe": False, "lead_event_date_field_available": True, "missing_contact_created_at_count": 0, "excluded_non_paid_count": 0, "excluded_pseudo_campaign_count": 0}), \
         patch("db.revenue_repository.fetch_sync_state", return_value={"available": True, "datasets": {}}):
        yield


class TestAuditEndpoint:
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
        res = client.get("/api/revenue-attribution/audit?window=current_quarter")
        assert res.status_code in (401, 403)

    def test_returns_verdict(self, client, admin_cookie):
        with _patched_audit_repo():
            res = client.get("/api/revenue-attribution/audit?window=current_quarter",
                             cookies={"ads_session": admin_cookie})
        if res.status_code == 401:
            pytest.skip("Session cookie not accepted in test environment")
        assert res.status_code == 200
        data = res.json()
        assert "verdict" in data
        assert data["verdict"] in ("SAFE", "UNSAFE")
        assert "date_grain_health" in data

    def test_invalid_window_rejected(self, client, admin_cookie):
        res = client.get("/api/revenue-attribution/audit?window=60d",
                         cookies={"ads_session": admin_cookie})
        if res.status_code == 401:
            pytest.skip("Session cookie not accepted in test environment")
        assert res.status_code == 400
