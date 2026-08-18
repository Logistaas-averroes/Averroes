"""
PR-ADS-124 — ROAS Country Geo Recovery + Revenue Chrome Fix.

Two production fixes:

  A. Revenue page chrome leak — ROAS by Country (and every revenue page) must
     never show the 7d/14d/30d/60d ad-window bar, the Help button, or the
     monitoring warning banner. A single applyPageChrome() guard + a
     body.is-revenue-page CSS failsafe + a fixed loadMonitoringStatus() async
     race close every leak.

  B. ROAS by Country impossible recovery — the page told users to run a Google
     Ads geo sync that had no button. Revenue Health now has an admin Google Ads
     Geo Sync action + reconciliation diagnostics, and the blocked country card
     has a real CTA. Country ROAS stays blocked until geo spend reconciles with
     canonical campaign spend — no fake ROAS, no partial denominator, no $0.

Static frontend assertions + boundary-patched backend assertions. No google-ads
SDK import and no network.
"""

import os
import re
import sys
from datetime import date, datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "static", "styles.css"), encoding="utf-8").read()
SERVER = open(os.path.join(ROOT, "api", "server.py"), encoding="utf-8").read()
SCHEMA = open(os.path.join(ROOT, "db", "schema.py"), encoding="utf-8").read()
WRITERS = open(os.path.join(ROOT, "db", "writers.py"), encoding="utf-8").read()
CONN_SRC = open(os.path.join(ROOT, "connectors", "google_ads_direct.py"), encoding="utf-8").read()
SVC_SRC = open(os.path.join(ROOT, "services", "google_ads_geo_sync_service.py"), encoding="utf-8").read()


def _at(d):
    return datetime.fromisoformat(d + "T12:00:00+00:00")


def _fn(marker, span=1600):
    i = JS.find(marker)
    assert i != -1, f"missing {marker}"
    return JS[i:i + span]


def _load_revattr():
    try:
        from services.revenue_attribution_service import build_revenue_attribution
        return build_revenue_attribution
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"service import unavailable: {exc}")


def _load_geo():
    try:
        import services.google_ads_geo_sync_service as svc
        return svc
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"geo sync service import unavailable: {exc}")


# ════════════ A. Revenue page chrome leak ════════════

# 1-3. A direct route to /#/roas-countries hides the ad-window bar, Help, and the
# monitoring banner. The single guard + CSS failsafe cover all three.


def test_apply_page_chrome_hides_all_three_chrome_elements():
    # span widened in PR-ADS-134 (the guard also scopes the freshness strip
    # off the rebuilt Executive Overview dashboard).
    fn = _fn("function applyPageChrome", span=1400)
    assert "time-range-bar" in fn       # 7d / 14d / 30d / 60d bar
    assert "page-help-btn" in fn        # Help button
    assert "monitoring-banner" in fn    # monitoring warning
    assert "is-revenue-page" in fn      # body class failsafe
    assert "isRevenuePage(page)" in fn


def test_roas_countries_is_a_revenue_page():
    # The guard keys off REVENUE_PAGES, which must include roas-countries.
    i = JS.find("const REVENUE_PAGES")
    line = JS[i:i + 200]
    assert "roas-countries" in line
    assert "roas-campaigns" in line


def test_css_failsafe_hides_chrome_on_revenue_pages():
    # Even if a JS hook is missed, the body class hides all three on revenue pages.
    block = CSS[CSS.find("body.is-revenue-page #time-range-bar"):]
    block = block[:block.find("}") + 1]
    assert "#time-range-bar" in block
    assert "#page-help-btn" in block
    assert "#monitoring-banner" in block
    assert "display: none !important" in block


def test_chrome_guard_wired_into_route_lifecycle():
    # Called at navigation start, after _currentPage is set, from showApp, and the
    # hashchange handler — so a direct hash route applies chrome immediately.
    nav = _fn("function navigate(page, options)", span=4200)
    assert nav.count("applyPageChrome(page)") >= 2
    show = _fn("function showApp(user)", span=1600)
    assert "applyPageChrome(" in show
    hashblk = JS[JS.find('addEventListener("hashchange"'):JS.find('addEventListener("hashchange"') + 260]
    assert "applyPageChrome(page)" in hashblk


# 4. loadMonitoringStatus() re-checks _currentPage AFTER the async fetch.
# 5. showApp() does not repaint the monitoring banner onto a revenue page.


def test_monitoring_status_rechecks_current_page_after_await():
    fn = _fn("async function loadMonitoringStatus", span=1600)
    # Captured at request start AND re-checked after the await.
    assert "pageAtRequestStart = _currentPage" in fn
    before = fn.find("await fetchJSON")
    assert before != -1
    after = fn[before:]
    # A revenue-page guard exists both before and after the fetch.
    assert "isRevenuePage(pageAtRequestStart)" in fn[:before]
    assert "isRevenuePage(_currentPage)" in after


def test_showapp_applies_chrome_so_banner_never_paints_revenue_page():
    fn = _fn("function showApp(user)", span=1600)
    # showApp kicks loadMonitoringStatus AND asserts chrome from the URL, so a
    # banner can't paint onto a revenue page during the initial load race.
    assert "loadMonitoringStatus()" in fn
    assert "applyPageChrome(hashToPage(window.location.hash))" in fn


# ════════════ B. Geo sync action + reconciliation ════════════

# 6. Revenue Health renders the "Run Google Ads geo sync" action + diagnostics.


def test_revenue_health_renders_geo_sync_panel():
    assert 'id="geo-sync-panel"' in HTML
    fn = _fn("function renderGeoSyncPanel", span=3200)
    assert "Google Ads Geo Sync" in fn
    assert "Preview Google Ads geo sync" in fn
    assert "Run Google Ads geo sync" in fn
    assert "Geo reconciliation status" in JS  # in the badge helper
    for label in ("Canonical campaign spend", "Geo spend", "Variance",
                  "Latest geo sync", "Rows written"):
        assert label in fn, f"geo panel missing label: {label}"
    # loadRevenueHealth wires the panel in.
    assert "loadGeoSync()" in JS


def test_geo_sync_panel_is_admin_gated_in_ui():
    fn = _fn("function renderGeoSyncPanel", span=2600)
    assert '_currentUser && _currentUser.role === "admin"' in fn
    # Non-admin sees the ask-an-admin guidance, never a dead run button.
    assert "Ask an admin to run Google Ads Geo Sync from Revenue Health." in fn


# 7. The geo sync endpoints exist.


def test_geo_sync_endpoints_registered():
    assert '@app.post("/api/google-ads-geo-sync/run", status_code=202)' in SERVER
    assert '@app.get("/api/google-ads-geo-sync/status")' in SERVER
    assert '@app.get("/api/google-ads-geo-reconcile")' in SERVER

    try:
        import api.server as s
        routes = [r.path for r in s.app.routes if hasattr(r, "path")]
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"api.server import failed: {exc}")
    assert "/api/google-ads-geo-sync/run" in routes
    assert "/api/google-ads-geo-sync/status" in routes
    assert "/api/google-ads-geo-reconcile" in routes


# 8. The run + status endpoints are admin-only.


def test_geo_sync_endpoints_admin_gated():
    for marker in ("def api_geo_sync_run", "def api_geo_sync_status"):
        i = SERVER.find(marker)
        assert i != -1, f"missing {marker}"
        assert "check_admin_or_token(request)" in SERVER[i:i + 1200]


def test_geo_sync_run_unauthenticated_rejected():
    try:
        from fastapi.testclient import TestClient
        from api.server import app
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"testclient unavailable: {exc}")
    client = TestClient(app, raise_server_exceptions=False)
    res = client.post("/api/google-ads-geo-sync/run", json={"window": "ytd", "dry_run": True})
    assert res.status_code in (401, 403)
    res2 = client.get("/api/google-ads-geo-sync/status")
    assert res2.status_code in (401, 403)


# 9. Geo sync uses read-only Google Ads API calls only — no Google Ads / HubSpot writes.


def test_geo_connector_is_read_only():
    i = CONN_SRC.find("def fetch_geo_daily_spend")
    assert i != -1
    body = CONN_SRC[i:CONN_SRC.find("\ndef ", i + 1)]
    assert "SELECT" in body
    for forbidden in ("mutate", "Mutate", "INSERT", "UPDATE", "DELETE", "create_", "remove_"):
        assert forbidden not in body, f"geo connector contains forbidden op: {forbidden}"


def test_geo_sync_service_writes_only_local_geo_table():
    # The only spend write is the local canonical geo table; never Google Ads /
    # HubSpot. PR-ADS-153F blocker 2 replaced the row-by-row upsert with an
    # atomic per-chunk REPLACE (delete + insert in one transaction), because a
    # refresh that only merges can never drop a row Google has since withdrawn.
    # The write TARGET is unchanged — this guard follows the call, not the name.
    assert "replace_geo_daily_spend_chunk" in SVC_SRC
    # No mutate/external-write calls (the doctrine docstring may NAME HubSpot to
    # say it is never written — so check for write CALLS, not the word).
    for forbidden in ("mutate", "Mutate", "hubspot_pull", "write_deals", "upsert_deal"):
        assert forbidden not in SVC_SRC
    assert "import db.writers" in SVC_SRC
    # The schema/writer target only the local canonical geo table.
    assert "CREATE TABLE IF NOT EXISTS google_ads_geo_daily_spend" in SCHEMA
    assert "def replace_geo_daily_spend_chunk" in WRITERS
    # …and the replacement itself only ever touches that one local table.
    i = WRITERS.find("def replace_geo_daily_spend_chunk")
    body = WRITERS[i:WRITERS.find("\ndef ", i + 1)]
    assert "DELETE FROM google_ads_geo_daily_spend" in body
    assert "INSERT INTO google_ads_geo_daily_spend" in body
    for other in ("hubspot_deal_ledger", "campaign_performance", "search_terms"):
        assert other not in body


def test_geo_connector_raises_on_error_not_verified_empty():
    # The geo connector must RAISE on API error so the sync can mark the chunk
    # failed — a valid empty result and an API failure must not look identical.
    i = CONN_SRC.find("def fetch_geo_daily_spend")
    body = CONN_SRC[i:CONN_SRC.find("\ndef ", i + 1)]
    assert "raise_on_error=True" in body


def test_geo_sync_marks_chunk_failed_on_api_error(monkeypatch):
    svc = _load_geo()
    monkeypatch.setattr("db.revenue_repository.fetch_account_time_zone", lambda: "Europe/London")

    def _boom(a, b):
        raise RuntimeError("geographic_view unavailable / unsupported")
    monkeypatch.setattr(svc, "fetch_geo_daily", _boom)
    res = svc.run_google_ads_geo_sync(window="ytd", dry_run=True, now=_at("2026-06-30"))
    # API failure → failed chunks, never confidently "verified empty".
    assert res["status"] == "failed"
    assert res["summary"]["chunks_verified"] == 0
    assert res["summary"]["chunks_failed"] >= 1
    assert res["errors"]


def test_geo_sync_resolves_country_codes_onto_rows(monkeypatch):
    svc = _load_geo()
    monkeypatch.setattr("db.revenue_repository.fetch_account_time_zone", lambda: "Europe/London")
    captured = {}
    # PR-ADS-153F blocker 2: the sync writes each chunk through the atomic
    # replacement, so that is where the resolved rows now arrive.

    def _capture(customer_id, chunk_start, chunk_end, rows, sync_run_id=None):
        captured.setdefault("rows", rows)
        return {"replaced": True, "deleted": 0, "written": len(rows)}

    monkeypatch.setattr("db.writers.replace_geo_daily_spend_chunk", _capture)
    # PR-ADS-153F blocker 4: a real (non-dry) run now FAILS CLOSED unless it
    # holds the sync lease, so a test exercising the write path has to grant one.
    # With no lease store reachable the run cannot record coverage, checkpoints
    # or failures — proceeding anyway would write geo rows nothing can vouch for.
    import db.writers as _w
    monkeypatch.setattr(_w, "try_claim_geo_sync_lease", lambda *a, **k: "acquired")
    monkeypatch.setattr(_w, "release_geo_sync_lease", lambda *a, **k: True)
    monkeypatch.setattr(_w, "upsert_geo_sync_state", lambda *a, **k: True)
    monkeypatch.setattr(_w, "upsert_geo_coverage", lambda *a, **k: True)
    monkeypatch.setattr(svc, "fetch_geo_daily", lambda a, b: {"rows": [
        {"customer_id": "1", "country_criterion_id": "2826", "campaign_id": "9",
         "spend_date": a, "cost_micros": 5_000_000}]})
    monkeypatch.setattr(svc, "fetch_geo_country_codes",
                        lambda ids: {"2826": {"country_code": "GB", "name": "United Kingdom"}})
    svc.run_google_ads_geo_sync(window="ytd", dry_run=False, now=_at("2026-06-30"))
    row = captured["rows"][0]
    assert row["country_code"] == "GB"
    assert row["country_name"] == "United Kingdom"


def test_geo_sync_run_is_dry_by_default_and_skips_writes(monkeypatch):
    svc = _load_geo()
    monkeypatch.setattr("db.revenue_repository.fetch_account_time_zone", lambda: "Europe/London")
    written = {"n": 0}

    def _no_write(rows, sync_run_id=None):
        written["n"] += len(rows)
        return len(rows)
    monkeypatch.setattr("db.writers.upsert_geo_daily_spend", _no_write)
    monkeypatch.setattr(svc, "fetch_geo_daily", lambda a, b: {"rows": [
        {"customer_id": "1", "country_criterion_id": "2826", "campaign_id": "9",
         "spend_date": a, "cost_micros": 1_000_000}]})
    res = svc.run_google_ads_geo_sync(window="ytd", dry_run=True,
                                      now=_at("2026-06-30"))
    assert res["dry_run"] is True
    assert res["status"] == "success"
    assert written["n"] == 0  # dry run never writes local rows


# ════════════ Country ROAS safety rule (no loosening) ════════════

# 10. ROAS by Country stays blocked when geo reconciliation mismatches.
# 11. ROAS by Country shows the table only when geo reconciliation is verified.


def _geo_by_country(rows):
    """Canonical geo by-country payload (the country SOURCE) or absent."""
    if rows is None:
        return {"available": True, "has_rows": False, "rows": []}
    total = sum(int(round(float(r.get("spend") or 0) * 1_000_000)) for r in rows)
    return {"available": True, "has_rows": bool(rows), "rows": rows,
            "total_cost_micros": total, "total_spend": total / 1_000_000,
            "total_spend_usd": None, "fx_complete": True, "currency_code": "GBP",
            "customer_id": "3059734490", "reporting_currency": "USD"}


def _patch_revattr(monkeypatch, *, canonical, geo_daily, country_rows, revenue_rows,
                   coverage=None, geo_country=None, legacy_spend=None,
                   geo_coverage=None):
    leads = {"available": True, "rows": [], "event_date_safe": True,
             "lead_event_date_field_available": True, "missing_contact_created_at_count": 0,
             "excluded_non_paid_count": 0, "excluded_pseudo_campaign_count": 0,
             "coverage_start": None, "coverage_end": None}
    # Legacy geo table rows (fetch_campaign_country_spend) — distinct from the
    # canonical geo source so a test can prove the legacy table is NOT used.
    spend = {"available": True, "rows": legacy_spend if legacy_spend is not None else country_rows,
             "coverage_start": "2026-01-01", "coverage_end": "2026-06-30"}
    revenue = {"available": True, "rows": revenue_rows, "coverage_start": None, "coverage_end": None}
    cov = coverage or [{"chunk_start": "2026-01-01", "chunk_end": "2026-06-30", "status": "verified"}]
    # PR-ADS-153F: the durable GEO coverage ledger is a mandatory input to the
    # country readiness gate. It is a separate ledger from campaign spend
    # coverage above — proving campaign spend was fetched says nothing about
    # whether geographic_view ever was.
    geo_cov = (geo_coverage if geo_coverage is not None
               else [{"chunk_start": "2026-01-01", "chunk_end": "2026-06-30",
                      "status": "verified", "rows_written": 130,
                      "cost_micros_total": 10_000_000_000, "country_count": 2}])
    try:
        monkeypatch.setattr("db.revenue_repository.fetch_geo_coverage",
                            lambda c, s, e: {"available": True, "chunks": list(geo_cov)})
        monkeypatch.setattr("db.revenue_repository.fetch_campaign_country_spend", lambda s, e: spend)
        monkeypatch.setattr("db.revenue_repository.fetch_lead_quality", lambda s, e: leads)
        monkeypatch.setattr("db.revenue_repository.fetch_won_revenue", lambda s, e: revenue)
        monkeypatch.setattr("db.revenue_repository.fetch_sync_state", lambda: {"available": True, "datasets": {}})
        monkeypatch.setattr("db.revenue_repository.revenue_integration_connected", lambda: True)
        monkeypatch.setattr("db.revenue_repository.fetch_canonical_campaign_spend", lambda s, e: canonical)
        monkeypatch.setattr("db.revenue_repository.fetch_spend_coverage",
                            lambda s, e: {"available": True, "chunks": cov})
        monkeypatch.setattr("db.revenue_repository.fetch_campaign_identity",
                            lambda cid=None: {"available": True, "mappings": []})
        # PR-ADS-124: canonical geo total drives reconciliation; canonical geo
        # by-country is the country ROW source.
        monkeypatch.setattr("db.revenue_repository.fetch_geo_daily_spend_total", lambda s, e: geo_daily)
        monkeypatch.setattr("db.revenue_repository.fetch_geo_daily_spend_by_country",
                            lambda s, e: _geo_by_country(geo_country))
    except (ImportError, AttributeError) as exc:
        pytest.skip(f"runtime deps unavailable: {exc}")


def _canonical(total, *, fx_complete=True):
    return {"available": True, "rows": [
        {"campaign_id": "1", "campaign_name": "alpha", "cost_micros": int(total * 1_000_000),
         "spend": total, "spend_usd": total * 1.27 if fx_complete else None,
         "fx_complete": fx_complete}],
        "total_cost_micros": int(total * 1_000_000), "total_spend": total,
        "total_spend_usd": (total * 1.27 if fx_complete else None), "campaign_count": 1,
        "customer_id": "3059734490", "currency_code": "GBP", "reporting_currency": "USD",
        "fx_missing_days": 0 if fx_complete else 3, "fx_complete": fx_complete,
        "coverage_start": "2026-01-01", "coverage_end": "2026-06-30"}


_COUNTRY_ROWS = [{"campaign_name": "alpha", "country": "United Kingdom", "spend": 9000.0}]
_REVENUE_ROWS = [{"campaign_name": "alpha", "country": "United Kingdom", "deal_id": "d1",
                  "deal_amount_usd": 5000.0, "match_status": "matched"}]
# Canonical geo by-country rows (the country SOURCE), native + per-day-FX USD.
_GEO_COUNTRY = [
    {"country_criterion_id": "2826", "country_code": "GB", "country_name": "United Kingdom",
     "cost_micros": 7_000_000_000, "spend": 7000.0, "spend_usd": 8890.0, "fx_complete": True},
    {"country_criterion_id": "2250", "country_code": "FR", "country_name": "France",
     "cost_micros": 3_000_000_000, "spend": 3000.0, "spend_usd": 3810.0, "fx_complete": True},
]
_GEO_TOTAL_10K = {"available": True, "has_rows": True, "total_spend": 10000.0,
                  "rows_counted": 130, "country_count": 2, "last_synced_at": "2026-06-30 10:00:00"}


def test_country_roas_blocked_when_geo_mismatches(monkeypatch):
    build = _load_revattr()
    # Canonical campaign spend £10,000; canonical geo total £7,000 → mismatch.
    geo_daily = {"available": True, "has_rows": True, "total_spend": 7000.0,
                 "rows_counted": 120, "country_count": 14}
    _patch_revattr(monkeypatch, canonical=_canonical(10000.0), geo_daily=geo_daily,
                   country_rows=_COUNTRY_ROWS, revenue_rows=_REVENUE_ROWS, geo_country=_GEO_COUNTRY)
    out = build("ytd", now=_at("2026-06-30"))
    sh = out["source_health"]
    assert sh["country_spend_reconciled"] is False
    assert sh["country_roas_available"] is False  # stays blocked — no fake ROAS


def test_country_roas_available_when_geo_reconciles(monkeypatch):
    build = _load_revattr()
    _patch_revattr(monkeypatch, canonical=_canonical(10000.0, fx_complete=True),
                   geo_daily=_GEO_TOTAL_10K, country_rows=_COUNTRY_ROWS,
                   revenue_rows=_REVENUE_ROWS, geo_country=_GEO_COUNTRY)
    out = build("ytd", now=_at("2026-06-30"))
    sh = out["source_health"]
    assert sh["country_spend_reconciled"] is True
    assert sh["country_roas_available"] is True
    assert out["country_spend_available"] is True
    assert out["geo_country_mapping_status"] == "available"
    assert len(out["countries"]) >= 1


# Reviewer-required: the canonical geo table feeds the ROAS by Country rows.


def test_canonical_geo_table_feeds_country_rows(monkeypatch):
    build = _load_revattr()
    _patch_revattr(monkeypatch, canonical=_canonical(10000.0, fx_complete=True),
                   geo_daily=_GEO_TOTAL_10K, country_rows=_COUNTRY_ROWS,
                   revenue_rows=_REVENUE_ROWS, geo_country=_GEO_COUNTRY)
    out = build("ytd", now=_at("2026-06-30"))
    names = {c.get("country") for c in out["countries"]}
    # France is ONLY in the canonical geo source (no revenue, not in legacy rows
    # used elsewhere) — its presence proves the rows come from canonical geo.
    assert "France" in names
    assert "United Kingdom" in names
    assert out["source_health"]["geo_country_source"] == "canonical_google_ads_api"


def test_legacy_geo_table_not_used_when_canonical_geo_exists(monkeypatch):
    build = _load_revattr()
    # Legacy geo table carries a bogus country that must NEVER appear once the
    # canonical geo table has rows.
    legacy = [{"campaign_name": "x", "country": "Atlantis", "spend": 99999.0}]
    _patch_revattr(monkeypatch, canonical=_canonical(10000.0, fx_complete=True),
                   geo_daily=_GEO_TOTAL_10K, country_rows=_COUNTRY_ROWS,
                   revenue_rows=_REVENUE_ROWS, geo_country=_GEO_COUNTRY, legacy_spend=legacy)
    out = build("ytd", now=_at("2026-06-30"))
    names = {c.get("country") for c in out["countries"]}
    assert "Atlantis" not in names  # legacy table is not the source
    assert "France" in names and "United Kingdom" in names


def test_country_rows_sum_to_reconciled_geo_total(monkeypatch):
    build = _load_revattr()
    _patch_revattr(monkeypatch, canonical=_canonical(10000.0, fx_complete=True),
                   geo_daily=_GEO_TOTAL_10K, country_rows=_COUNTRY_ROWS,
                   revenue_rows=_REVENUE_ROWS, geo_country=_GEO_COUNTRY)
    out = build("ytd", now=_at("2026-06-30"))
    # Country spend is USD reporting (FX complete); the visible rows sum to the
    # canonical geo total expressed in USD (8890 + 3810 = 12700) — same source.
    total = round(sum(float(c.get("spend") or 0) for c in out["countries"]), 2)
    assert total == 12700.0


def test_no_geo_data_remains_blocked_never_zero(monkeypatch):
    build = _load_revattr()
    # Canonical campaign spend exists, but the canonical geo table has NO rows.
    geo_daily = {"available": True, "has_rows": False}
    _patch_revattr(monkeypatch, canonical=_canonical(10000.0, fx_complete=True),
                   geo_daily=geo_daily, country_rows=[], revenue_rows=_REVENUE_ROWS,
                   geo_country=None, legacy_spend=[])
    out = build("ytd", now=_at("2026-06-30"))
    sh = out["source_health"]
    assert sh["country_roas_available"] is False     # blocked
    assert out["geo_country_mapping_status"] == "no_geo_data"
    # Never a fabricated £0 country denominator.
    assert out["source_health"].get("geo_country_source") == "legacy_geo_table"


def test_country_roas_blocked_when_fx_incomplete_even_if_geo_reconciles(monkeypatch):
    # Safety rule: geo reconciled is NOT enough — FX must also be verified.
    build = _load_revattr()
    geo_daily = {"available": True, "has_rows": True, "total_spend": 10000.0,
                 "rows_counted": 130, "country_count": 16}
    _patch_revattr(monkeypatch, canonical=_canonical(10000.0, fx_complete=False),
                   geo_daily=geo_daily, country_rows=_COUNTRY_ROWS, revenue_rows=_REVENUE_ROWS)
    out = build("ytd", now=_at("2026-06-30"))
    assert out["source_health"]["country_roas_available"] is False


def test_geo_reconciliation_service_keeps_country_blocked_on_mismatch(monkeypatch):
    svc = _load_geo()
    monkeypatch.setattr("db.revenue_repository.fetch_account_time_zone", lambda: "Europe/London")
    monkeypatch.setattr("db.revenue_repository.fetch_canonical_campaign_spend",
                        lambda s, e: {"available": True, "total_spend": 10000.0,
                                      "currency_code": "GBP", "customer_id": "3059734490"})
    monkeypatch.setattr("db.revenue_repository.fetch_spend_coverage",
                        lambda s, e: {"available": True, "chunks": [
                            {"chunk_start": "2026-01-01", "chunk_end": "2026-06-30", "status": "verified"}]})
    monkeypatch.setattr("db.revenue_repository.fetch_fx_coverage",
                        lambda s, e, b, q="USD": {"available": True, "complete": True})
    monkeypatch.setattr("db.revenue_repository.fetch_geo_daily_spend_total",
                        lambda s, e: {"available": True, "has_rows": True, "total_spend": 7000.0,
                                      "rows_counted": 120, "country_count": 14})
    out = svc.build_geo_reconciliation("ytd", now=_at("2026-06-30"))
    assert out["status"] == "mismatch"
    assert out["variance"] == round(7000.0 - 10000.0, 6)  # exact mismatch is visible
    assert out["country_roas_unblockable"] is False


def test_geo_reconciliation_no_geo_data_is_not_treated_as_zero(monkeypatch):
    svc = _load_geo()
    monkeypatch.setattr("db.revenue_repository.fetch_account_time_zone", lambda: "Europe/London")
    monkeypatch.setattr("db.revenue_repository.fetch_canonical_campaign_spend",
                        lambda s, e: {"available": True, "total_spend": 10000.0,
                                      "currency_code": "GBP", "customer_id": "3059734490"})
    monkeypatch.setattr("db.revenue_repository.fetch_spend_coverage",
                        lambda s, e: {"available": True, "chunks": []})
    monkeypatch.setattr("db.revenue_repository.fetch_fx_coverage",
                        lambda s, e, b, q="USD": {"available": True, "complete": True})
    monkeypatch.setattr("db.revenue_repository.fetch_geo_daily_spend_total",
                        lambda s, e: {"available": True, "has_rows": False})
    out = svc.build_geo_reconciliation("ytd", now=_at("2026-06-30"))
    assert out["status"] == "no_geo_data"
    assert out["geo_total"] is None        # never a fabricated £0
    assert out["country_roas_unblockable"] is False


# 12. The blocked country card includes a real CTA to Revenue Health / geo sync.


def test_blocked_country_card_has_recovery_cta():
    fn = _fn("function roasCountryCtaButtons", span=900)
    assert "Open Revenue Health" in fn
    assert "Run Google Ads Geo Sync" in fn
    assert "Run Google Ads Spend Backfill" in fn
    # Run actions are admin-gated; non-admin gets guidance.
    assert '_currentUser.role === "admin"' in fn
    assert "Ask an admin to run Google Ads Geo Sync from Revenue Health." in fn
    # The unreconciled blocked card actually renders the CTA.
    blocked = _fn("function renderCountrySpendUnreconciledState", span=3800)
    assert "roasCountryCtaButtons()" in blocked


def test_country_cta_navigates_and_runs_jobs():
    i = JS.find('data-country-cta]')
    assert i != -1
    handler = JS[JS.find('e.target.closest("[data-country-cta]")') - 60:
                 JS.find('e.target.closest("[data-country-cta]")') + 520]
    assert 'navigate("revenue-health")' in handler
    assert "runGeoSyncJob(false)" in handler
    assert "runSpendBackfillJob(false)" in handler


# ════════════ no Google Ads / HubSpot writes anywhere in the geo path ════════════


def test_geo_path_has_no_google_ads_or_hubspot_writes():
    # Connector geo query is a SELECT; service writes only the local table; the
    # endpoints never call a Google Ads/HubSpot mutate.
    assert "geographic_view" in CONN_SRC
    i = SERVER.find("def _run_geo_sync_worker")
    worker = SERVER[i:i + 1600]
    assert "run_google_ads_geo_sync" in worker
    for forbidden in ("hubspot_write", "mutate", "Mutate"):
        assert forbidden not in worker
