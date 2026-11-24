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
from tests.cohort_fixture import (  # noqa: E402
    cohort_patch, healthy_sync_row, withheld_sync_row,
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


def _audit(window="current_quarter", grain=None, pollution=None, rev=None,
           cohort_sync_row=None):
    # PR-ADS-177A-F1 (truth audit MINOR 5): lead-window safety is the
    # acquisition cohort's verdict, so the cohort is fed through the REAL
    # freshness `assess` (healthy unless the case passes another sync row).
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
         patch("db.revenue_repository.fetch_sync_state", return_value={"available": True, "datasets": {}}), \
         cohort_patch([], as_of=NOW, sync_row=cohort_sync_row or healthy_sync_row(NOW)):
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
    # PR-ADS-177A-F1 (MINOR 5): the leads every window publishes are the
    # acquisition cohort's; the legacy table's grain is reported, not judged.
    assert dg["lead_date_field_current"] == "acquisition cohort · contact_created_at"
    assert dg["lead_cohort_status"] == "published"
    assert dg["legacy_leads_table_window_safe"] is True
    # PR-ADS-153E-B: revenue is windowed by the CANONICAL deal close date.
    assert dg["deal_date_field"] == "hubspot_deal_ledger.deal_close_date"
    assert dg["spend_window_safe"] is True
    assert dg["revenue_window_safe"] is True


def test_audit_verdict_unsafe_when_lead_grain_unsafe():
    # PR-ADS-177A-F2 (truth audit round 2, MAJOR): F1 made a withheld cohort
    # an UNSAFE revenue verdict, which Revenue Health rendered as "Revenue is
    # not ready". A withheld cohort blocks no page (owner decision): it is
    # reported in its own field, with its reason and next step, and the
    # revenue verdict does not read it. (The name is kept for history: no
    # lead grain makes the revenue verdict unsafe any more.)
    a = _audit(cohort_sync_row=withheld_sync_row(NOW))
    assert a["verdict"] == "SAFE" and a["blockers"] == []
    lc = a["lead_cohort"]
    assert (lc["status"], lc["reason"], lc["published"], lc["blocks_revenue"]) == (
        "withheld", "source_bootstrap_incomplete", False, False)
    assert "lead_window_safe" not in a["date_grain_health"]
    assert a["date_grain_health"]["lead_cohort_status"] == "withheld"


def test_audit_legacy_leads_grain_alone_no_longer_decides_the_verdict():
    # PR-ADS-177A-F1 (MINOR 5): the legacy `leads` table feeds no window figure
    # since PR-ADS-177A. Its grain is reported for the record, judged on
    # nothing.
    a = _audit(grain=_grain(safe=False))
    assert a["date_grain_health"]["legacy_leads_table_window_safe"] is False
    assert a["lead_cohort"]["published"] is True
    assert a["verdict"] == "SAFE"


def test_audit_verdict_safe_when_all_safe_and_clean():
    a = _audit(grain=_grain(True), pollution=_pollution(clean=True))
    assert a["verdict"] == "SAFE"
    assert a["blockers"] == []


def test_audit_verdict_unsafe_when_pseudo_pollution_present():
    # PR-ADS-177A-F3 (truth audit round 3, MINOR 1): pseudo rows in the
    # LEGACY `leads` table are reported, never a revenue blocker — no figure
    # reads that table since PR-ADS-177A. (The name is kept for history.)
    a = _audit(grain=_grain(True), pollution=_pollution(clean=False))
    assert a["verdict"] == "SAFE" and a["blockers"] == []
    assert a["pseudo_campaign_rows"] == ["(direct)"]
    assert any("pseudo" in f.lower() for f in a["legacy_table_findings"])


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
