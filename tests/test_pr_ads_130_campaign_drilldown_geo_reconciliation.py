"""
PR-ADS-130 — Campaign Revenue Drilldown + Country Geo Reconciliation Root-Cause.

Two connected fixes:

  Part 1 — ROAS by Campaign gains a per-row drilldown of the closed-won
           clients/deals behind the revenue (company / contact / deal record ids,
           amount, close date, attribution), served by a read-only endpoint and
           rendered in an expandable drawer. Missing company/contact ids show
           "unavailable" (never fabricated) and never hide the deal.

  Part 2 — ROAS by Country stays correctly BLOCKED on a real geo↔campaign
           mismatch, but the mismatch is now ROOT-CAUSED (largest daily/campaign
           gaps, campaigns missing geo, unknown/null-country spend, query
           dimensions) instead of a blind "run geo sync" loop.

No fake ROAS, no fake $0, no native-GBP-as-USD, no Google Ads / HubSpot writes.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.canonical_ledger_fixtures import (  # noqa: E402
    from_legacy_deal_rows, patch_canonical_ledger,
)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
SERVER = open(os.path.join(ROOT, "api", "server.py"), encoding="utf-8").read()


def _slice(marker, span=2200):
    i = JS.find(marker)
    assert i != -1, f"{marker} not found in app.js"
    return JS[i:i + span]


# ══════════════ Part 1 — Campaign drilldown ══════════════

DEAL_DETAIL_ROWS = [
    {"deal_id": "555111", "contact_id": "987654321", "gclid": "Cj0KCQtestgclidvalue",
     "company": "Vracht Free Zone", "country": "Mexico",
     "campaign_name": "Mexico, Chile, Colombia", "deal_close_date": "2026-05-10",
     "deal_amount_usd": 51400.0, "deal_stage_label": "Closed Won",
     "match_status": "matched", "match_source": "gclid"},
    # A deal with NO company / contact — must still appear, ids "unavailable".
    {"deal_id": "555222", "contact_id": None, "gclid": None,
     "company": None, "country": "Chile",
     "campaign_name": "Mexico, Chile, Colombia", "deal_close_date": "2026-04-01",
     "deal_amount_usd": 7000.0, "deal_stage_label": "Closed Won",
     "match_status": "url_fallback", "match_source": "first_url"},
    # A deal on a DIFFERENT campaign — must be excluded.
    {"deal_id": "999999", "contact_id": "111", "gclid": None,
     "company": "Other Co", "country": "Gulf",
     "campaign_name": "Gulf", "deal_close_date": "2026-03-01",
     "deal_amount_usd": 3000.0, "deal_stage_label": "Closed Won",
     "match_status": "matched", "match_source": "gclid"},
]


def _patch_details(monkeypatch, rows=None, *, available=True):
    import db.revenue_repository as repo
    # PR-ADS-153E-B: the campaign drilldown reads the canonical deal ledger at
    # the SAME scope the campaign row was aggregated at.
    patch_canonical_ledger(
        monkeypatch,
        from_legacy_deal_rows(rows if rows is not None else DEAL_DETAIL_ROWS),
        available=available)
    # Identity map: canonical campaigns from the spend set (auto-links), no manual maps.
    monkeypatch.setattr(repo, "fetch_canonical_campaign_spend", lambda s, e: {
        "available": True, "customer_id": "111",
        "rows": [{"campaign_name": "Mexico, Chile, Colombia"}, {"campaign_name": "Gulf"}]})
    monkeypatch.setattr(repo, "fetch_campaign_identity",
                        lambda cid=None: {"available": True, "mappings": []})


# Test 1 — a campaign-detail endpoint exists (mart rows OR endpoint)
def test_campaign_detail_endpoint_exists():
    assert '@app.get("/api/revenue-performance/campaign-detail")' in SERVER
    try:
        import api.server as s
        routes = [r.path for r in s.app.routes if hasattr(r, "path")]
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"api.server import failed: {exc}")
    assert "/api/revenue-performance/campaign-detail" in routes


# Test 2 — detail rows include the required identity fields
def test_detail_rows_have_required_fields(monkeypatch):
    _patch_details(monkeypatch)
    from services.revenue_attribution_service import build_campaign_deal_details
    out = build_campaign_deal_details("ytd", "Mexico, Chile, Colombia")
    details = out["details"]
    assert len(details) == 2  # only the two Mexico deals, Gulf excluded
    d = details[0]
    for key in ("company_name", "company_record_id", "main_contact_name",
                "main_contact_record_id", "deal_name", "deal_record_id",
                "deal_amount_usd", "deal_close_date"):
        assert key in d, f"detail row missing {key}"
    # The populated deal carries the real HubSpot record ids we DO store.
    top = next(x for x in details if x["deal_record_id"] == "555111")
    # PR-ADS-153E-B: the canonical ledger names the DEAL and holds no company
    # record, so `company_name` is Unavailable and `deal_name` carries identity.
    assert top["company_name"] is None
    assert top["deal_name"] == "Vracht Free Zone"
    assert top["main_contact_record_id"] == "987654321"
    assert top["deal_amount_usd"] == 51400.0
    # GCLID is masked, never shown in full.
    assert top["gclid"] and top["gclid"] != "Cj0KCQtestgclidvalue"


# Test 3 — missing company/contact does not remove the deal
def test_missing_company_contact_does_not_drop_deal(monkeypatch):
    _patch_details(monkeypatch)
    from services.revenue_attribution_service import build_campaign_deal_details
    out = build_campaign_deal_details("ytd", "Mexico, Chile, Colombia")
    dealless = next((x for x in out["details"] if x["deal_record_id"] == "555222"), None)
    assert dealless is not None, "a deal with no company/contact must still appear"
    assert dealless["company_name"] is None          # rendered as "unavailable"
    assert dealless["main_contact_record_id"] is None
    assert dealless["deal_amount_usd"] == 7000.0      # never a fabricated $0


# Test 3b — a MISSING deal amount stays None, never a fabricated $0.00
def test_missing_deal_amount_is_null_not_zero(monkeypatch):
    rows = [
        {"deal_id": "555333", "contact_id": "222", "gclid": None,
         "company": "No Amount Co", "country": "Mexico",
         "campaign_name": "Mexico, Chile, Colombia", "deal_close_date": "2026-02-01",
         "deal_amount_usd": None, "deal_stage_label": "Closed Won",
         "match_status": "matched", "match_source": "gclid"},
    ]
    _patch_details(monkeypatch, rows)
    from services.revenue_attribution_service import build_campaign_deal_details
    out = build_campaign_deal_details("ytd", "Mexico, Chile, Colombia")
    d = next(x for x in out["details"] if x["deal_record_id"] == "555333")
    # None (→ "unavailable" in the UI), NOT 0.0 which would read as real revenue.
    assert d["deal_amount_usd"] is None
    # The frontend drawer renders a null amount as "unavailable", not $0.00.
    assert 'd.deal_amount_usd == null ? drilldownValue(null) : fmtMoney(d.deal_amount_usd)' in JS


def test_campaign_detail_db_unavailable_is_reported(monkeypatch):
    _patch_details(monkeypatch, rows=[], available=False)
    from services.revenue_attribution_service import build_campaign_deal_details
    out = build_campaign_deal_details("ytd", "Mexico, Chile, Colombia")
    assert out["details"] == []
    # PR-ADS-153E-B: the reason names the canonical read, and nothing falls back.
    assert out["revenue_available"] is False
    assert out["legacy_fallback_used"] is False
    assert out["source_health"]["status"] == "canonical_ledger_unreadable"


# Test 4 — frontend has an expandable campaign row control
def test_frontend_has_expandable_campaign_row():
    table = _slice("function renderRoasCampaignTable", span=3000)
    assert "data-campaign-expand-idx" in table
    assert "campaign-drilldown-row" in table
    # A click handler toggles the drawer and lazy-loads the detail.
    assert "data-campaign-expand-idx" in JS
    assert "function loadCampaignDrilldown" in JS
    assert "/api/revenue-performance/campaign-detail?window=" in JS


# Test 5 — expanded drawer renders a client/deal table
def test_drawer_renders_client_deal_table():
    fn = _slice("function renderCampaignDrilldown", span=2000)
    assert "Clients / Deals Behind This Campaign" in fn
    for col in (">Company<", ">Company ID<", ">Main Contact<", ">Contact ID<",
                ">Deal<", ">Deal ID<", ">Amount<", ">Close Date<"):
        assert col in fn, f"drilldown table missing column {col}"
    # Empty state copy for a campaign with no attributed client detail.
    assert "No attributed closed-won client detail found for this campaign." in fn


# Test 6 — no undefined / null text in the drilldown UI
def test_no_undefined_or_null_in_drilldown():
    fn = _slice("function renderCampaignDrilldown", span=2000)
    helper = _slice("function drilldownValue", span=400)
    # Missing values render an explicit "unavailable", not undefined/null/blank.
    assert '"unavailable"' in helper or "unavailable" in helper
    assert "undefined" not in fn
    # The drilldown routes every value through the safe formatter.
    assert "drilldownValue(" in fn


# ══════════════ Part 2 — Geo reconciliation root-cause ══════════════

def _geo(monkeypatch, *, geo_total, canonical_total=69991.91, breakdown=None):
    import db.revenue_repository as repo
    monkeypatch.setattr(repo, "fetch_account_time_zone", lambda: "Europe/London")
    monkeypatch.setattr(repo, "fetch_canonical_campaign_spend", lambda s, e: {
        "available": True, "total_spend": canonical_total, "currency_code": "GBP",
        "customer_id": "111", "coverage_start": "2026-01-01", "coverage_end": "2026-06-30"})
    monkeypatch.setattr(repo, "fetch_geo_daily_spend_total", lambda s, e: {
        "available": True, "has_rows": True, "total_spend": geo_total,
        "rows_counted": 13516, "country_count": 191,
        "last_synced_at": "2026-07-01 05:03:37+00:00"})
    monkeypatch.setattr(repo, "fetch_spend_coverage", lambda s, e: {"available": True, "chunks": [
        {"chunk_start": "2000-01-01", "chunk_end": "2100-01-01", "status": "verified",
         "rows_written": 10, "cost_micros_total": 5_000_000}]})
    monkeypatch.setattr(repo, "fetch_fx_coverage", lambda s, e, c: {"complete": True})
    default_breakdown = {
        "available": True,
        "daily": [
            {"spend_date": "2026-06-12", "campaign_spend": 1200.0, "geo_spend": 900.0},
            {"spend_date": "2026-05-01", "campaign_spend": 500.0, "geo_spend": 500.0},
        ],
        "by_campaign": [
            {"campaign_id": "123", "campaign_name": "Gulf", "campaign_spend": 19407.94, "geo_spend": 18000.0},
            {"campaign_id": "124", "campaign_name": "MENA", "campaign_spend": 5000.0, "geo_spend": 5000.0},
        ],
        "unmapped_geo_native": 2704.0,
        "geo_rows_with_null_country": 10,
        "campaign_rows_counted": 3623,
    }
    monkeypatch.setattr(repo, "fetch_geo_reconciliation_breakdown",
                        lambda s, e: breakdown if breakdown is not None else default_breakdown)
    from services.google_ads_geo_sync_service import build_geo_reconciliation
    return build_geo_reconciliation("ytd")


# Test 1/2 — mismatch above tolerance stays blocked; sync does not fake verify
def test_geo_mismatch_stays_blocked(monkeypatch):
    out = _geo(monkeypatch, geo_total=67287.91)  # 3.86% below canonical
    assert out["status"] == "mismatch"
    assert out["reconciled"] is False
    assert out["country_roas_unblockable"] is False


# Test 3 — audit exposes totals, variance, tolerance, reason, next_action
def test_geo_audit_exposes_core_fields(monkeypatch):
    out = _geo(monkeypatch, geo_total=67287.91)
    for key in ("canonical_campaign_total", "geo_total", "variance", "variance_pct",
                "tolerance", "reason", "next_action"):
        assert key in out and out[key] is not None, f"missing {key}"


# Test 4 — largest daily gaps
def test_geo_audit_largest_daily_gaps(monkeypatch):
    out = _geo(monkeypatch, geo_total=67287.91)
    gaps = out["largest_daily_gaps"]
    assert gaps and gaps[0]["date"] == "2026-06-12"
    assert gaps[0]["variance_native"] == -300.0


# Test 5 — largest campaign gaps
def test_geo_audit_largest_campaign_gaps(monkeypatch):
    out = _geo(monkeypatch, geo_total=67287.91)
    gaps = out["largest_campaign_gaps"]
    assert gaps and gaps[0]["campaign_name"] == "Gulf"
    assert gaps[0]["variance_native"] == round(18000.0 - 19407.94, 6)


# Test 6 — campaign spend without geo rows
def test_geo_audit_detects_campaign_without_geo(monkeypatch):
    breakdown = {
        "available": True, "daily": [],
        "by_campaign": [
            {"campaign_id": "123", "campaign_name": "Gulf", "campaign_spend": 2704.0, "geo_spend": 0.0},
            {"campaign_id": "124", "campaign_name": "MENA", "campaign_spend": 5000.0, "geo_spend": 5000.0},
        ],
        "unmapped_geo_native": 0.0, "geo_rows_with_null_country": 0, "campaign_rows_counted": 2,
    }
    out = _geo(monkeypatch, geo_total=67287.91, breakdown=breakdown)
    missing = out["campaigns_missing_geo"]
    assert missing and missing[0]["campaign_name"] == "Gulf"
    assert out["campaign_spend_without_geo_native"] == 2704.0
    assert out["reason"] == "campaign_spend_without_geo"


# Test 7 — unknown / null country spend detected
def test_geo_audit_detects_unknown_country_spend(monkeypatch):
    out = _geo(monkeypatch, geo_total=67287.91)
    assert out["geo_rows_with_null_country"] == 10
    assert out["unknown_country_spend_native"] == 2704.0
    # Reason names the by-design residual (geo report omits location-less spend).
    assert out["reason"] == "geo_report_does_not_reconcile_by_design"
    assert "campaign_query_dimensions" in out and "geo_query_dimensions" in out


# Test 8 — unknown-location spend surfaced (not distributed across countries)
def test_unknown_location_spend_surfaced_not_distributed(monkeypatch):
    out = _geo(monkeypatch, geo_total=67287.91)
    # It is exposed as a distinct bucket value, NOT spread onto country rows.
    assert out["unknown_location_spend_native"] == 2704.0


# Test 9 — country ROAS unblocks only when within tolerance
def test_geo_within_tolerance_reconciles(monkeypatch):
    out = _geo(monkeypatch, geo_total=69991.91)  # exact match
    assert out["status"] == "reconciled"
    assert out["reconciled"] is True
    assert out["country_roas_unblockable"] is True


# Test — frontend surfaces the root cause on the country card + Revenue Health
def test_frontend_country_card_shows_root_cause():
    card = _slice("function renderCountrySpendUnreconciledState", span=3800)
    assert "roasCountryGeoReconcile" in card
    assert "Likely cause" in card
    assert "Largest gap" in card
    loader = _slice("async function loadRoasCountries", span=1800)
    assert "/api/google-ads-geo-reconcile?window=" in loader


def test_frontend_health_root_cause_section():
    fn = _slice("function renderGeoReconcileDetail", span=3200)
    assert "Geo Reconciliation Root Cause" in fn
    assert "Largest campaign gaps" in fn
    assert "Largest daily gaps" in fn


# Test 10 — no unsafe writes anywhere in the change
def test_no_unsafe_writes():
    files = [
        "static/app.js",
        "services/revenue_attribution_service.py",
        "services/google_ads_geo_sync_service.py",
        "db/revenue_repository.py",
        "api/server.py",
    ]
    for rel in files:
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read().lower()
        for forbidden in (
            ".mutate(", "upload_offline", "offline_conversion",
            "google_ads_client", "hubspot_client",
            "requests.post", "requests.put", "requests.patch", "requests.delete",
            "set_budget", "set_bid", "pause_campaign", "enable_campaign",
        ):
            assert forbidden not in src, f"{rel} must not reference '{forbidden}'"


def test_campaign_deal_details_query_is_read_only():
    src = open(os.path.join(ROOT, "db", "revenue_repository.py"), encoding="utf-8").read()
    i = src.find("def fetch_campaign_deal_details")
    j = src.find("\ndef ", i + 1)
    body = src[i:j].lower()
    for verb in ("insert", "update ", "delete", "upsert", "drop", "alter"):
        assert verb not in body, f"detail query must be read-only (found '{verb}')"
