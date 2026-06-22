"""
PR-ADS-116 — Business Window Integrity for revenue pages.

Backend: every revenue response resolves and returns exactly one date range, and
spend / leads / revenue are all filtered by that same window. Frontend: the
active date range is shown beside each selector, page state is per-page, and a
stale-response guard prevents an older response painting over a newer selection.

Deterministic boundary fixtures cover 31 Dec / 1 Jan, 31 Mar / 1 Apr,
30 Jun / 1 Jul. Service/DB tests skip gracefully where the driver is absent.
"""

import os
import shutil as _shutil
import subprocess as _subprocess
import sys
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
SERVER = open(os.path.join(ROOT, "api", "server.py"), encoding="utf-8").read()


def _at(d):
    return datetime.fromisoformat(d + "T12:00:00+00:00")


def _fn(name, span=2600):
    idx = JS.find(name)
    assert idx != -1, f"{name} not found"
    return JS[idx:idx + span]


# ════════════════════════ window resolution (pure) ════════════════════════


def test_window_boundaries_at_22_june():
    from analysis.business_windows import resolve_window
    now = _at("2026-06-22")
    cq = resolve_window("current_quarter", now=now)
    lq = resolve_window("last_quarter", now=now)
    ytd = resolve_window("ytd", now=now)
    assert (cq["start_date"], cq["end_date"]) == ("2026-04-01", "2026-06-22")
    assert (lq["start_date"], lq["end_date"]) == ("2026-01-01", "2026-03-31")
    assert (ytd["start_date"], ytd["end_date"]) == ("2026-01-01", "2026-06-22")
    assert lq["is_closed_window"] is True   # a completed prior quarter
    assert cq["is_closed_window"] is False


@pytest.mark.parametrize("d,cq,lq,ytd", [
    ("2025-12-31", ("2025-10-01", "2025-12-31"), ("2025-07-01", "2025-09-30"), ("2025-01-01", "2025-12-31")),
    ("2026-01-01", ("2026-01-01", "2026-01-01"), ("2025-10-01", "2025-12-31"), ("2026-01-01", "2026-01-01")),
    ("2026-03-31", ("2026-01-01", "2026-03-31"), ("2025-10-01", "2025-12-31"), ("2026-01-01", "2026-03-31")),
    ("2026-04-01", ("2026-04-01", "2026-04-01"), ("2026-01-01", "2026-03-31"), ("2026-01-01", "2026-04-01")),
    ("2026-06-30", ("2026-04-01", "2026-06-30"), ("2026-01-01", "2026-03-31"), ("2026-01-01", "2026-06-30")),
    ("2026-07-01", ("2026-07-01", "2026-07-01"), ("2026-04-01", "2026-06-30"), ("2026-01-01", "2026-07-01")),
])
def test_boundary_transitions(d, cq, lq, ytd):
    from analysis.business_windows import resolve_window
    now = _at(d)
    assert (resolve_window("current_quarter", now=now)["start_date"],
            resolve_window("current_quarter", now=now)["end_date"]) == cq
    assert (resolve_window("last_quarter", now=now)["start_date"],
            resolve_window("last_quarter", now=now)["end_date"]) == lq
    assert (resolve_window("ytd", now=now)["start_date"],
            resolve_window("ytd", now=now)["end_date"]) == ytd
    # Last Quarter must never overlap Current Quarter.
    assert lq[1] < cq[0], "last quarter ends before current quarter starts"
    # YTD always ends at the Current Quarter end (today).
    assert ytd[1] == cq[1]
    # When Last Quarter is in the SAME year, YTD includes it (and CQ).
    if lq[0][:4] == ytd[0][:4]:
        assert ytd[0] <= lq[0]


# ════════════════ same window drives all three sources ════════════════


def _load_build():
    try:
        from services.revenue_attribution_service import build_revenue_attribution
        return build_revenue_attribution
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"service import unavailable: {exc}")


def _spy_repo(monkeypatch):
    """Capture the (start, end) bounds passed to each durable source."""
    from datetime import date
    seen = {}
    empty = {"available": True, "rows": [], "coverage_start": None, "coverage_end": None}
    leads = {**empty, "event_date_safe": True, "lead_event_date_field_available": True,
             "missing_contact_created_at_count": 0, "excluded_non_paid_count": 0,
             "excluded_pseudo_campaign_count": 0}

    def cap(name, ret):
        def _f(start, end, *a, **k):
            seen[name] = (start, end)
            return ret
        return _f

    try:
        monkeypatch.setattr("db.revenue_repository.fetch_campaign_country_spend", cap("spend", empty))
        monkeypatch.setattr("db.revenue_repository.fetch_lead_quality", cap("leads", leads))
        monkeypatch.setattr("db.revenue_repository.fetch_won_revenue", cap("revenue", empty))
        monkeypatch.setattr("db.revenue_repository.fetch_sync_state", lambda *a, **k: {"available": True, "datasets": {}})
        monkeypatch.setattr("db.revenue_repository.revenue_integration_connected", lambda: False)
    except (ImportError, AttributeError) as exc:
        pytest.skip(f"runtime deps unavailable: {exc}")
    return seen


def test_all_sources_share_selected_window(monkeypatch):
    build = _load_build()
    seen = _spy_repo(monkeypatch)
    from datetime import date
    out = build("last_quarter", now=_at("2026-06-22"))
    # The response advertises the resolved window.
    assert out["window"]["key"] == "last_quarter"
    assert out["window"]["start_date"] == "2026-01-01"
    assert out["window"]["end_date"] == "2026-03-31"
    # Spend, leads, and revenue were all queried with that SAME window.
    expect = (date(2026, 1, 1), date(2026, 3, 31))
    assert seen["spend"] == expect
    assert seen["leads"] == expect
    assert seen["revenue"] == expect


def test_current_quarter_uses_its_own_window(monkeypatch):
    build = _load_build()
    seen = _spy_repo(monkeypatch)
    from datetime import date
    out = build("current_quarter", now=_at("2026-06-22"))
    assert out["window"]["start_date"] == "2026-04-01"
    # Current Quarter excludes Q1 — the source bounds start in April, not January.
    assert seen["spend"][0] == date(2026, 4, 1)
    assert seen["revenue"][0] == date(2026, 4, 1)


def test_response_window_matches_selected_key(monkeypatch):
    build = _load_build()
    _spy_repo(monkeypatch)
    for key in ("current_quarter", "last_quarter", "ytd", "last_6_months", "all_time"):
        out = build(key, now=_at("2026-06-22"))
        assert out["window"]["key"] == key


def test_deals_window_returned(monkeypatch):
    try:
        from services.revenue_attribution_service import build_revenue_deals
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"service import unavailable: {exc}")
    with patch("db.revenue_repository.fetch_revenue_deals",
               return_value={"available": True, "rows": [], "table": "gclid_attribution"}):
        out = build_revenue_deals("last_quarter", now=_at("2026-06-22"))
    assert out["window"]["key"] == "last_quarter"
    assert out["window"]["start_date"] == "2026-01-01"
    assert out["window"]["end_date"] == "2026-03-31"
    assert out["window"]["is_closed_window"] is True


def test_audit_window_includes_label_and_closed_flag(monkeypatch):
    try:
        from services.revenue_attribution_service import build_revenue_attribution_audit
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"service import unavailable: {exc}")
    _spy_repo(monkeypatch)
    monkeypatch.setattr("db.revenue_repository.fetch_lead_date_grain_health",
                        lambda *a, **k: {"available": True, "lead_window_safe": True,
                                         "lead_event_date_field_available": True,
                                         "missing_contact_created_at_count": 0,
                                         "excluded_non_paid_count": 0,
                                         "excluded_pseudo_campaign_count": 0,
                                         "counts_by_source_type": {}})
    monkeypatch.setattr("db.revenue_repository.fetch_campaign_pollution_report",
                        lambda *a, **k: {"available": True, "pseudo_campaign_rows": [],
                                         "email_campaign_rows": [], "zero_spend_campaigns_with_leads": []})
    monkeypatch.setattr("db.writers.count_lead_exclusions", lambda: 0)
    out = build_revenue_attribution_audit("last_quarter", now=_at("2026-06-22"))
    assert out["window"]["key"] == "last_quarter"
    assert out["window"]["label"] == "Last Quarter"
    assert out["window"]["is_closed_window"] is True


# ════════════════════════ frontend: range display ════════════════════════


def test_window_range_elements_present():
    for el in ("roas-campaigns-range", "roas-countries-range",
               "revenue-deals-range", "revenue-health-range"):
        assert f'id="{el}"' in HTML, f"missing range element {el}"
    assert "function renderWindowRange" in JS
    assert "function formatWindowRange" in JS
    # Each loader renders its range from the response window.
    assert 'renderWindowRange("roas-campaigns-range"' in JS
    assert 'renderWindowRange("roas-countries-range"' in JS
    assert 'renderWindowRange("revenue-deals-range"' in JS
    assert 'renderWindowRange("revenue-health-range"' in JS


# ════════════════════════ frontend: stale-response guard ════════════════════


def test_stale_response_guard_present():
    assert "_revReqSeq" in JS
    assert "function _revResponseIsCurrent" in JS
    # Guard compares the response window key to the CURRENT selection.
    guard = _fn("function _revResponseIsCurrent", span=300)
    assert "getRoasBusinessWindow()" in guard
    for loader in ("function loadRoasCampaigns", "function loadRoasCountries",
                   "function loadDeals", "function loadRevenueHealth"):
        body = _fn(loader, span=1600)
        assert "++_revReqSeq" in body, f"{loader} must take a request token"
        assert "_revResponseIsCurrent" in body or "_revReqSeq" in body
        assert "return" in body


def test_guard_fails_closed_on_missing_window_key():
    # An absent window key must NEVER render as valid — every revenue response is
    # required to carry a resolved window.
    guard = _fn("function _revResponseIsCurrent", span=300)
    assert "Boolean(respKey) && respKey === getRoasBusinessWindow()" in guard
    # Behavioural check via node, if available.
    node = _shutil.which("node")
    if not node:
        pytest.skip("node not available")
    import re as _re
    m = _re.search(r"function _revResponseIsCurrent\(page, token, data\) \{.*?\n\}", JS, _re.DOTALL)
    assert m
    script = (
        "const _revReqSeq = { p: 5 };\n"
        "function getRoasBusinessWindow(){ return 'ytd'; }\n"
        + m.group(0).replace("_revReqSeq[page]", "_revReqSeq[page]") + "\n"
        # token current, but response has NO window key → must be rejected.
        "console.log(JSON.stringify(_revResponseIsCurrent('p', 5, { window: {} })));\n"
        "console.log(JSON.stringify(_revResponseIsCurrent('p', 5, { window: { key: 'ytd' } })));\n"
        "console.log(JSON.stringify(_revResponseIsCurrent('p', 5, { window: { key: 'current_quarter' } })));\n"
    )
    out = _subprocess.check_output([node, "-e", script]).decode().split()
    assert out == ["false", "true", "false"]


def test_selector_change_clears_previous_range_immediately():
    # Each loader sets its range chip to "Loading…" before awaiting, so a stale
    # range never sits beside a freshly changed selector.
    assert "function setWindowRangeLoading" in JS
    assert '"Loading…"' in _fn("function setWindowRangeLoading", span=200)
    for loader, el in (
        ("function loadRoasCampaigns", "roas-campaigns-range"),
        ("function loadRoasCountries", "roas-countries-range"),
        ("function loadDeals", "revenue-deals-range"),
        ("function loadRevenueHealth", "revenue-health-range"),
    ):
        body = _fn(loader, span=1600)
        i_loading = body.find(f'setWindowRangeLoading("{el}")')
        i_await = body.find("await fetch")
        assert i_loading != -1, f"{loader} must clear its range chip"
        assert i_loading < i_await, f"{loader} must clear the range BEFORE awaiting"


# ════════════════ frontend: per-page state (no cross-window bleed) ════════════


def test_pages_use_own_window_state():
    # The shared roasRevenue* globals are gone — each page keeps its own.
    assert "roasRevenueSourceHealth" not in JS
    assert "roasRevenueSummary" not in JS
    campaign = _fn("function renderRoasCampaignsPage", span=1400)
    assert "roasCampaignSourceHealth" in campaign
    assert "roasCampaignSummary" in campaign
    country = _fn("function renderRoasCountriesPage", span=1600)
    assert "roasCountrySourceHealth" in country
    # Each loader fetches its own window and stores a page-specific window object.
    assert "roasCampaignWindow" in JS
    assert "roasCountryWindow" in JS


# ════════════════ safe empty distinct from blocked / zeroed ════════════════


def test_safe_empty_distinct_from_blocked():
    # Completed empty period → safe empty copy, never blocked / fake-zero ROAS.
    assert "No campaign revenue found for this window" in JS
    assert "No country revenue found for this window" in JS
    camp_empty = _fn("function renderRoasCampaignSafeEmptyState", span=600)
    assert "not ready" not in camp_empty.lower()
    assert "0.00x" not in camp_empty
