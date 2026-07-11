"""
PR-ADS-141 — Platform Evidence + Lead Intelligence UX Foundation.

Proves the shared foundation for the non-revenue evidence sections:
  - a single evidence-window vocabulary (7d/14d/30d/60d/180d/all_time), distinct
    from the business windows used by Dashboard / Revenue pages;
  - evidence endpoints accept the new windows (incl. 180d + all_time), reject an
    unknown window with 400 (never silently coerced), and echo the resolved
    window — all_time uses no lower date bound and never fabricates zeros;
  - the frontend replaces the old day-window button bar with an Evidence window
    dropdown on the 7 evidence pages only, keeps the business-window selector for
    revenue/dashboard pages, and modernizes the page chrome + freshness;
  - the touched backend is read-only (no external writes).

Read-only. No writes to Google Ads or HubSpot anywhere in this foundation.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APP_JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
INDEX_HTML = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
STYLES = open(os.path.join(ROOT, "static", "styles.css"), encoding="utf-8").read()
SERVER = open(os.path.join(ROOT, "api", "server.py"), encoding="utf-8").read()
EVIDENCE_MODULE = open(os.path.join(ROOT, "analysis", "evidence_windows.py"),
                      encoding="utf-8").read()

EVIDENCE_ENDPOINTS = [
    "/api/campaigns",
    "/api/keywords",
    "/api/geo",
    "/api/leads",
    "/api/waste",
    "/api/leads/country-summary",
    "/api/search-terms",
    "/api/search-terms/summary",
    "/api/search-terms/ngrams",
]


# ════════════════ 1. Evidence-window vocabulary (pure) ════════════════


def test_evidence_windows_vocabulary():
    from analysis.evidence_windows import (
        EVIDENCE_WINDOWS, EVIDENCE_WINDOW_DAYS, resolve_evidence_window,
        EvidenceWindowError, DEFAULT_EVIDENCE_WINDOW,
    )
    assert EVIDENCE_WINDOWS == ("7d", "14d", "30d", "60d", "180d", "all_time")
    assert EVIDENCE_WINDOW_DAYS["180d"] == 180
    assert EVIDENCE_WINDOW_DAYS["all_time"] is None
    assert DEFAULT_EVIDENCE_WINDOW == "30d"

    # 180d resolves to a real lookback; all_time resolves to no lower bound.
    assert resolve_evidence_window("180d") == {"key": "180d", "days": 180, "is_all_time": False}
    assert resolve_evidence_window("all_time") == {"key": "all_time", "days": None, "is_all_time": True}

    # Unknown window is an error — never silently coerced to a smaller window.
    with pytest.raises(EvidenceWindowError):
        resolve_evidence_window("90d")
    with pytest.raises(EvidenceWindowError):
        resolve_evidence_window("60")
    # all_time can be refused by a source that cannot serve it (never coerced).
    with pytest.raises(EvidenceWindowError):
        resolve_evidence_window("all_time", allow_all_time=False)


def test_evidence_date_clause_all_time_has_no_bound():
    from api.server import _evidence_date_clause
    clause, params = _evidence_date_clause("run_date", None)
    assert clause == "TRUE" and params == []          # all_time = no lower bound
    clause, params = _evidence_date_clause("run_date", 180)
    assert "run_date >=" in clause and params == [180]


# ════════════════ 2. Backend endpoints accept windows / reject invalid ═══════


def _make_client():
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi[testclient] not available")
    try:
        from api.server import app
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"api.server import failed: {exc}")
    return TestClient(app, raise_server_exceptions=False)


def _viewer_cookie():
    os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only")
    from api.auth import set_session
    from starlette.responses import Response as StarletteResponse
    r = StarletteResponse()
    set_session(r, "testviewer", "viewer")
    cookie_header = r.headers.get("set-cookie", "")
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith("ads_session="):
            return part.split("=", 1)[1]
    return None


@pytest.fixture()
def client_cookie():
    os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-for-unit-tests-only")
    client = _make_client()
    cookie = _viewer_cookie()
    if not cookie:
        pytest.skip("could not mint viewer session cookie")
    return client, cookie


@pytest.mark.parametrize("endpoint", EVIDENCE_ENDPOINTS)
@pytest.mark.parametrize("window", ["7d", "14d", "30d", "60d", "180d", "all_time"])
def test_endpoint_accepts_all_evidence_windows(client_cookie, endpoint, window):
    client, cookie = client_cookie
    res = client.get(f"{endpoint}?window={window}", cookies={"ads_session": cookie})
    # DB is unavailable in the test env, so a valid window yields a 200 empty
    # response — the point is it is NOT rejected and echoes the resolved window.
    assert res.status_code == 200, res.text
    body = res.json()
    assert body.get("window") == window, f"{endpoint} must echo window {window}"


@pytest.mark.parametrize("endpoint", EVIDENCE_ENDPOINTS)
def test_endpoint_rejects_invalid_window_with_400(client_cookie, endpoint):
    client, cookie = client_cookie
    res = client.get(f"{endpoint}?window=99d", cookies={"ads_session": cookie})
    assert res.status_code == 400, f"{endpoint} must reject an unknown window"
    # The 400 names the bad window — it is never silently coerced.
    assert "99d" in res.text


@pytest.mark.parametrize("endpoint", EVIDENCE_ENDPOINTS)
def test_all_time_does_not_fabricate_rows(client_cookie, endpoint):
    client, cookie = client_cookie
    res = client.get(f"{endpoint}?window=all_time", cookies={"ads_session": cookie})
    assert res.status_code == 200, res.text
    body = res.json()
    # all_time must not fabricate rows: with the DB unavailable it is an explicit
    # empty + db_unavailable, never a faked non-empty/zeroed dataset.
    assert body.get("window") == "all_time"
    for key in ("rows", "campaigns", "leads", "waste"):
        if key in body:
            assert body[key] == [], f"{endpoint} all_time must not fabricate {key}"


def test_legacy_days_param_still_works(client_cookie):
    # Backward compatibility: the old ?days= param is still accepted (no window).
    client, cookie = client_cookie
    res = client.get("/api/campaigns?days=30", cookies={"ads_session": cookie})
    assert res.status_code == 200, res.text
    assert res.json().get("window") == "30d"


# ════════════════ 3. Read-only doctrine (touched backend) ════════════════


FORBIDDEN_WRITES = (
    "requests.post", "requests.put", "requests.patch", "requests.delete",
    ".mutate(", "upload_offline", "upload_conversions", "create_deal",
    "update_deal", "create_contact", "update_contact", "set_budget",
    "set_bid", "pause_campaign", "enable_campaign", "add_negative",
    "negative_keyword",
)


def test_evidence_module_is_read_only():
    text = EVIDENCE_MODULE.lower()
    for token in FORBIDDEN_WRITES:
        assert token.lower() not in text, f"evidence_windows.py contains '{token}'"


def test_evidence_endpoint_handlers_are_select_only():
    # Extract each evidence handler body and assert it never issues a write.
    handlers = [
        "def api_campaigns(", "def api_keywords(", "def api_geo(",
        "def api_leads(", "def api_waste(", "def api_leads_country_summary(",
        "def api_search_terms(", "def api_search_terms_summary(",
        "def api_search_terms_ngrams(",
    ]
    for marker in handlers:
        start = SERVER.find(marker)
        assert start != -1, f"handler {marker} not found"
        # Body runs until the next top-level decorator/def.
        nxt = SERVER.find("\n@app.", start + 1)
        body = SERVER[start:nxt if nxt != -1 else start + 6000]
        upper = body.upper()
        for verb in (" INSERT ", " UPDATE ", " DELETE ", " DROP ", " TRUNCATE "):
            assert verb not in upper, f"{marker} contains a write ({verb.strip()})"
        for token in FORBIDDEN_WRITES:
            assert token.lower() not in body.lower(), f"{marker} contains '{token}'"


# ════════════════ 4. Frontend — evidence window dropdown ════════════════


def test_evidence_windows_defined_with_all_six_options():
    block = APP_JS[APP_JS.find("const EVIDENCE_WINDOWS"):
                   APP_JS.find("const EVIDENCE_WINDOWS") + 500]
    for value in ("7d", "14d", "30d", "60d", "180d", "all_time"):
        assert f'"{value}"' in block, f"EVIDENCE_WINDOWS missing {value}"
    for label in ("7 days", "180 days", "All time"):
        assert label in block


def test_evidence_pages_list_and_helper():
    block = APP_JS[APP_JS.find("const EVIDENCE_PAGES"):
                   APP_JS.find("const EVIDENCE_PAGES") + 300]
    # BLOCKER 2: ngrams is derived Search Term evidence and uses the dropdown too.
    for route in ("campaigns", "search-terms", "ngrams", "keywords", "geo",
                  "leads", "opportunities", "waste"):
        assert f'"{route}"' in block, f"EVIDENCE_PAGES missing {route}"
    assert "function isEvidencePage(" in APP_JS


def test_evidence_helpers_present():
    for fn in ("function getEvidenceWindow(", "function setEvidenceWindow(",
               "function renderEvidenceWindowDropdown(", "function evidenceWindowQuery(",
               "function renderEvidencePageShell("):
        assert fn in APP_JS, f"missing helper {fn}"


def test_evidence_dropdown_in_html_and_wired():
    assert 'id="evidence-window-select"' in INDEX_HTML
    assert 'id="evidence-window-control"' in INDEX_HTML
    assert "Evidence window" in INDEX_HTML
    # The select's change handler drives setEvidenceWindow.
    assert "handleEvidenceWindowSelectChange" in APP_JS
    handler = APP_JS[APP_JS.find("function handleEvidenceWindowSelectChange"):
                     APP_JS.find("function handleEvidenceWindowSelectChange") + 160]
    assert "setEvidenceWindow" in handler


def test_changing_window_reloads_current_page():
    fn = APP_JS[APP_JS.find("function setEvidenceWindow("):
                APP_JS.find("function setEvidenceWindow(") + 700]
    assert "loadPage(_currentPage)" in fn
    assert "sessionStorage.setItem(\"evidence_window\"" in fn


def test_old_day_buttons_hidden_on_evidence_pages():
    # applyPageChrome delegates evidence-page chrome to applyEvidenceChrome.
    chrome = APP_JS[APP_JS.find("function applyPageChrome("):
                    APP_JS.find("function applyPageChrome(") + 1300]
    assert "applyEvidenceChrome(page)" in chrome
    # Evidence pages hide the legacy day buttons and show the evidence dropdown.
    fn = APP_JS[APP_JS.find("function applyEvidenceChrome("):
                APP_JS.find("function applyEvidenceChrome(") + 700]
    assert "isEvidencePage(page)" in fn
    assert ".time-range-btn" in fn and "btn.hidden = evidencePage" in fn
    assert "evidence-window-control" in fn
    assert "is-evidence-page" in fn


def test_evidence_loaders_use_window_param():
    # Each of the 7 evidence page loaders fetches with the evidence window,
    # never the legacy day integer.
    assert "/api/campaigns?${evidenceWindowQuery()}" in APP_JS
    assert "/api/keywords?${evidenceWindowQuery()}" in APP_JS
    assert "/api/waste?${evidenceWindowQuery()}" in APP_JS
    assert "/api/leads?${evidenceWindowQuery()}" in APP_JS
    assert "/api/geo?${evWindow}" in APP_JS
    assert "/api/leads/country-summary?${evWindow}" in APP_JS
    # Search-terms family sends window= in its param builders.
    assert 'params.set("window", getEvidenceWindow())' in APP_JS
    # evidenceWindowQuery serializes to a window= param.
    q = APP_JS[APP_JS.find("function evidenceWindowQuery("):
               APP_JS.find("function evidenceWindowQuery(") + 160]
    assert "window=" in q


def test_revenue_and_dashboard_keep_business_window_selector():
    # Revenue pages are untouched — still a business-window selector, and NOT in
    # the evidence-page list.
    assert "data-business-window-select" in INDEX_HTML
    rev_block = APP_JS[APP_JS.find("const REVENUE_PAGES"):
                       APP_JS.find("const REVENUE_PAGES") + 200]
    for rp in ("roas-campaigns", "roas-countries", "deals", "revenue-by-source", "dashboard"):
        assert rp in rev_block
    ev_block = APP_JS[APP_JS.find("const EVIDENCE_PAGES"):
                      APP_JS.find("const EVIDENCE_PAGES") + 300]
    # No revenue/dashboard route leaks into the evidence-page list.
    for rp in ("roas-campaigns", "roas-countries", "revenue-by-source", "dashboard", "deals"):
        assert f'"{rp}"' not in ev_block


# ════════════════ 5. Modern chrome — freshness chip + truth strip ════════════


def test_freshness_rendered_as_modern_chip():
    # The per-page freshness line is restyled into a compact pill (not a raw
    # text wall) — a rounded chip with a status dot.
    idx = STYLES.find(".is-evidence-page .run-meta {")
    assert idx != -1, "evidence-scoped run-meta chip not found"
    block = STYLES[idx:idx + 400]
    assert "border-radius: 999px" in block
    assert ".is-evidence-page .run-meta.is-stale::before" in STYLES
    assert ".is-evidence-page .run-meta.is-fresh::before" in STYLES


def test_read_only_chip_available():
    # A read-only chip is part of the evidence truth strip + styled.
    fn = APP_JS[APP_JS.find("function renderEvidenceTruthStrip("):
                APP_JS.find("function renderEvidenceTruthStrip(") + 500]
    assert '"Read-only"' in fn
    assert ".evidence-status-chip--readonly" in STYLES


def test_big_explainer_collapsed_on_evidence_pages():
    # Evidence pages collapse the big explainer card to a compact strip.
    assert "is-evidence-page" in APP_JS
    assert ".is-evidence-page .page-explanation__grid" in STYLES


def test_evidence_shell_css_classes_present():
    for cls in (".evidence-shell", ".evidence-command-bar", ".evidence-status-chip",
                ".evidence-kpi-grid", ".evidence-table-shell", ".evidence-table-scroll",
                ".evidence-empty-state", ".evidence-drawer-shell"):
        assert cls in STYLES, f"missing evidence shell class {cls}"


def test_no_null_undefined_nan_in_new_frontend_helpers():
    # The new evidence helpers must never leak null/undefined/NaN into the UI.
    start = APP_JS.find("// ── Evidence window selector (PR-ADS-141)")
    end = APP_JS.find("// ── Fetch helpers")
    region = APP_JS[start:end]
    assert start != -1 and end != -1 and region
    for bad in (">null<", ">undefined<", ">NaN<", "$0.00 fake"):
        assert bad not in region
    # The shell renders escaped, defined content only.
    assert "renderEvidencePageShell" in region


# ════════════════ BLOCKER 1 — complete totals, explicit truncation ═══════════
#
# A tiny fake DB so we can exercise the aggregate/truncation SQL paths without a
# real Postgres. Each cursor returns canned results chosen by matching the SQL.


class _FakeCursor:
    def __init__(self, responder):
        self._responder = responder
        self._rows = []
        self.description = []
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append(sql)
        rows, cols = self._responder(sql, params)
        self._rows = rows
        self.description = [(c,) for c in cols]

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, responder):
        self._responder = responder
        self.cursors = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        cur = _FakeCursor(self._responder)
        self.cursors.append(cur)
        return cur


def _patch_conn(monkeypatch, responder):
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "get_conn", lambda: _FakeConn(responder))


def test_leads_all_time_aggregates_complete_beyond_row_cap(monkeypatch):
    # 1500 leads in the window; the row page is capped at 1000, but the aggregate
    # totals (and per-campaign breakdown) cover ALL 1500 — never truncated.
    LEAD_COLS = ["contact_id", "company", "campaign_name", "keyword", "country",
                 "mql_status", "status_category", "gclid", "source_type", "run_date"]

    def responder(sql, params):
        if "GROUP BY 1, 2" in sql:  # complete aggregates
            return ([("Alpha", "qualified", 800),
                     ("Alpha", "junk", 200),
                     ("Beta", "in_progress", 500)],
                    ["campaign_name", "status_category", "n"])
        # row page (capped at 1000)
        rows = [("c%d" % i, None, "Alpha", None, None, None, "qualified",
                 None, None, "2026-07-01") for i in range(1000)]
        return (rows, LEAD_COLS)

    _patch_conn(monkeypatch, responder)
    import importlib
    server = importlib.import_module("api.server")
    out = server.api_leads(user={"e": "x"}, days=30, window="all_time")

    assert out["window"] == "all_time"
    # Row list is capped, but totals are COMPLETE (1500), and disclosed as partial.
    assert out["returned_count"] == 1000
    assert out["total_count"] == 1500
    assert out["has_more"] is True
    assert out["aggregates"]["totals"]["total"] == 1500
    assert out["aggregates"]["totals"]["qualified"] == 800
    assert out["aggregates"]["totals"]["junk"] == 200
    assert out["aggregates"]["totals"]["in_progress"] == 500
    # Per-campaign breakdown is complete and sums back to the totals.
    by_camp = {c["campaign_name"]: c for c in out["aggregates"]["by_campaign"]}
    assert by_camp["Alpha"]["total"] == 1000 and by_camp["Alpha"]["qualified"] == 800
    assert by_camp["Beta"]["in_progress"] == 500


def test_waste_all_time_discloses_truncation_beyond_500(monkeypatch):
    WASTE_COLS = ["search_term", "campaign_name", "spend_usd",
                  "junk_category", "matched_pattern", "crm_junk_confirmed", "run_date"]

    def responder(sql, params):
        if "COUNT(*)" in sql:                 # complete count
            return ([(1200,)], ["count"])
        rows = [("term%d" % i, "Alpha", 1.0, None, None, 0, "2026-07-01")
                for i in range(500)]          # capped page
        return (rows, WASTE_COLS)

    _patch_conn(monkeypatch, responder)
    import importlib
    server = importlib.import_module("api.server")
    out = server.api_waste(user={"e": "x"}, days=30, window="all_time")

    assert out["window"] == "all_time"
    assert out["returned_count"] == 500
    assert out["total_count"] == 1200          # full window count, not the page
    assert out["has_more"] is True
    assert out["truncated"] is True


def test_leads_aggregates_shape_empty_when_db_unavailable(monkeypatch):
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "get_conn", lambda: _NoneConn())
    import importlib
    server = importlib.import_module("api.server")
    out = server.api_leads(user={"e": "x"}, days=30, window="all_time")
    assert out["db_unavailable"] is True
    assert out["total_count"] == 0 and out["has_more"] is False
    assert out["aggregates"]["totals"]["total"] == 0   # never a fabricated number


class _NoneConn:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


def test_frontend_lead_quality_uses_complete_aggregates():
    fn = APP_JS[APP_JS.find("async function loadLeads"):
                APP_JS.find("async function loadLeads") + 2600]
    # KPIs + breakdown read the server aggregates, not a client-side row tally.
    assert "data.aggregates" in fn
    assert "agg.totals" in fn and "agg.by_campaign" in fn
    # It must NOT recompute totals by iterating a truncated leads array.
    assert "leads.forEach" not in fn


def test_frontend_in_progress_discloses_truncation():
    fn = APP_JS[APP_JS.find("async function loadOpportunities"):
                APP_JS.find("async function loadOpportunities") + 3800]
    assert "data.has_more" in fn
    assert "data.total_count" in fn and "data.returned_count" in fn
    assert "Showing" in fn


def test_frontend_waste_discloses_showing_x_of_y_and_partial_export():
    render = APP_JS[APP_JS.find("function renderWasteTable"):
                    APP_JS.find("function renderWasteTable") + 1600]
    assert "_wasteMeta.truncated" in render
    assert "Showing" in render and "of" in render
    exp = APP_JS[APP_JS.find("function downloadWasteCSV"):
                 APP_JS.find("function downloadWasteCSV") + 900]
    assert "_partial_first" in exp   # export never implies a complete library


# ════════════════ BLOCKER 3 — campaign drawer honours the window ═════════════


def test_campaign_detail_all_time_returns_window(monkeypatch):
    # PR-ADS-143: the drawer no longer runs run_date-bounded SQL — lead evidence is
    # event-date (contact_created_at) via the campaign-evidence service, keyword/
    # waste are latest-snapshot. all_time must not hidden-downgrade (window stays
    # all_time). With no DB, the safe shape still carries the window.
    captured = {"sql": []}

    def responder(sql, params):
        captured["sql"].append(sql)
        return ([], ["x"])

    _patch_conn(monkeypatch, responder)
    import importlib
    server = importlib.import_module("api.server")
    out = server.api_campaign_detail_query(
        user={"e": "x"}, campaign_name="alpha", days=30, window="all_time")
    assert out["window"] == "all_time"
    # No drawer query bounds on run_date any more (event-date / latest-snapshot only).
    for sql in captured["sql"]:
        assert "run_date >=" not in sql


def test_campaign_drawer_no_longer_bounds_on_run_date():
    # PR-ADS-143 contract: the drawer builds lead evidence via the event-date
    # service and never bounds any query on run_date; keyword/waste are latest
    # snapshots scoped by the approved label set.
    import os as _os
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    src = open(_os.path.join(root, "api", "server.py"), encoding="utf-8").read()
    fn = src[src.find("def _build_campaign_detail("):src.find("def api_campaign_detail_query(")]
    assert "run_date >= NOW() - INTERVAL" not in fn
    assert "build_campaign_drawer_evidence" in fn
    repo = open(_os.path.join(root, "db", "revenue_repository.py"), encoding="utf-8").read()
    detail = repo[repo.find("def fetch_campaign_lead_detail("):
                  repo.find("def fetch_sql_lead_details(")]
    assert "contact_created_at >=" in detail and "run_date >=" not in detail


def test_campaign_detail_rejects_invalid_window(monkeypatch):
    import importlib
    server = importlib.import_module("api.server")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        server.api_campaign_detail_query(
            user={"e": "x"}, campaign_name="alpha", days=30, window="99d")
    assert exc.value.status_code == 400


def test_frontend_drawer_uses_evidence_window_not_365():
    # Drawer sub-fetches pass window=getEvidenceWindow(), never a days downgrade.
    # (PR-ADS-143 inserts an optional &campaign_key=… between name and window.)
    assert "/api/campaign-detail?campaign_name=${encodeURIComponent(campaignName)}" in APP_JS
    assert "&window=${encodeURIComponent(getEvidenceWindow())}" in APP_JS
    # The hidden all_time->365 helper is gone entirely.
    assert "evidenceWindowDays" not in APP_JS
    # Attribution preview + quality drawer calls also send window=.
    preview = APP_JS[APP_JS.find("async function loadCampaignAttributionPreview"):
                     APP_JS.find("async function loadCampaignAttributionPreview") + 900]
    assert 'params.set("window", getEvidenceWindow())' in preview
    quality = APP_JS[APP_JS.find("async function loadCampaignAttributionQuality"):
                     APP_JS.find("async function loadCampaignAttributionQuality") + 900]
    assert 'params.set("window", getEvidenceWindow())' in quality


def test_campaign_detail_and_attribution_accept_window_endpoint(client_cookie):
    # End-to-end: the drawer endpoints accept the evidence window (200, not 422/400)
    # and reject an unknown one.
    client, cookie = client_cookie
    for url in ("/api/campaign-detail?campaign_name=x&window=all_time",
                "/api/attribution/quality?window=all_time&campaign=x",
                "/api/gclid-attribution?window=all_time&campaign=x"):
        assert client.get(url, cookies={"ads_session": cookie}).status_code == 200, url
    for url in ("/api/campaign-detail?campaign_name=x&window=99d",
                "/api/attribution/quality?window=99d",
                "/api/gclid-attribution?window=99d"):
        assert client.get(url, cookies={"ads_session": cookie}).status_code == 400, url
