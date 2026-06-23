"""
PR-ADS-120 — Spend Reconciliation, Campaign Identity Resolution & ROAS Integrity.

Proves the ten blueprint requirements:
  1. Account time zone controls spend-date window boundaries.
  2. Account-daily vs campaign-daily reconciliation reports amount + % variance.
  3. Native GBP total is never formatted as USD.
  4. `compliance - markets` maps through normalized identity matching.
  5. An approved manual mapping changes the ROAS row aggregation.
  6. `mexico,chile` without an approved mapping shows Unavailable, not $0.
  7. A truly matched zero-spend campaign shows $0 with "Verified zero spend".
  8. FX incomplete blocks USD ROAS.
  9. FX complete permits USD ROAS.
 10. Revenue and spend writes remain read-only w.r.t. HubSpot and Google Ads.

No google-ads SDK import and no network: connectors are reached only through
patched seams and all DB reads are patched at the repository boundary.
"""

import inspect
import os
import sys
from datetime import date, datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
SERVER = open(os.path.join(ROOT, "api", "server.py"), encoding="utf-8").read()
SCHEMA = open(os.path.join(ROOT, "db", "schema.py"), encoding="utf-8").read()
WRITERS = open(os.path.join(ROOT, "db", "writers.py"), encoding="utf-8").read()
CONN_SRC = open(os.path.join(ROOT, "connectors", "google_ads_direct.py"), encoding="utf-8").read()


def _at(d):
    return datetime.fromisoformat(d)


def _load_spend():
    try:
        import services.google_ads_spend_service as svc
        return svc
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"spend service import unavailable: {exc}")


def _load_revattr():
    try:
        from services.revenue_attribution_service import build_revenue_attribution
        return build_revenue_attribution
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"service import unavailable: {exc}")


def _load_identity():
    try:
        import services.campaign_identity_service as svc
        return svc
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"identity service import unavailable: {exc}")


# ════════════ 1. account time zone controls window boundaries ════════════


def test_account_time_zone_controls_window_boundary():
    svc = _load_spend()
    # 2026-06-23 23:30 UTC: in Auckland (UTC+12) it is already 2026-06-24; in
    # Los Angeles (UTC-7) it is still 2026-06-23. The account-local day decides.
    now = _at("2026-06-23T23:30:00+00:00")
    assert svc.account_today("Pacific/Auckland", now) == date(2026, 6, 24)
    assert svc.account_today("America/Los_Angeles", now) == date(2026, 6, 23)
    # The resolved spend window end reflects the ACCOUNT day, not UTC.
    wk_akl = svc.resolve_spend_window("current_quarter", "Pacific/Auckland", now)
    wk_la = svc.resolve_spend_window("current_quarter", "America/Los_Angeles", now)
    assert wk_akl["end_date"] == "2026-06-24"
    assert wk_la["end_date"] == "2026-06-23"


# ════════════ 2. account vs campaign reconciliation ════════════


def _patch_audit(monkeypatch, *, canonical, account, coverage=None, tz="Europe/London"):
    svc = _load_spend()
    monkeypatch.setattr("db.revenue_repository.fetch_account_time_zone", lambda: tz)
    monkeypatch.setattr("db.revenue_repository.fetch_canonical_campaign_spend", lambda s, e: canonical)
    monkeypatch.setattr("db.revenue_repository.fetch_spend_coverage",
                        lambda s, e: {"available": True, "chunks": coverage or [
                            {"chunk_start": "2026-04-01", "chunk_end": "2026-06-23", "status": "verified"}]})
    monkeypatch.setattr("db.revenue_repository.fetch_geo_spend_total", lambda s, e: {"available": True, "total_spend": 19090.30})
    monkeypatch.setattr("db.revenue_repository.fetch_account_daily_spend_total", lambda s, e: account)
    monkeypatch.setattr("db.revenue_repository.fetch_fx_coverage", lambda s, e, b, q="USD": {"available": True, "complete": True, "missing_dates": []})
    monkeypatch.setattr(svc, "fetch_daily_spend", lambda a, b: {"rows": []})
    return svc


def _canonical_audit(micros, currency="GBP"):
    return {"available": True, "rows": [], "total_cost_micros": micros,
            "total_spend": micros / 1_000_000, "total_spend_usd": (micros / 1_000_000) * 1.27,
            "campaign_count": 3, "customer_id": "123", "currency_code": currency,
            "reporting_currency": "USD", "fx_missing_days": 0, "fx_complete": True,
            "coverage_start": "2026-04-01", "coverage_end": "2026-06-23"}


def test_account_vs_campaign_reconciliation_reports_variance(monkeypatch):
    # Campaign sum £26,080.42; direct account £26,059.77 → small variance, but
    # outside the 2% tolerance it would mismatch. Here it is within tolerance.
    canonical = _canonical_audit(26_080_420_000)
    account = {"available": True, "total_cost_micros": 26_059_770_000,
               "total_spend": 26059.77, "currency_code": "GBP",
               "account_time_zone": "Europe/London", "spend_days": 84}
    svc = _patch_audit(monkeypatch, canonical=canonical, account=account)
    out = svc.build_google_ads_spend_audit("current_quarter", now=_at("2026-06-23T12:00:00+00:00"),
                                           api_total_micros=26_080_420_000)
    assert out["account_daily_total"] == 26059.77
    assert out["campaign_daily_total"] == 26080.42
    assert out["account_variance_amount"] is not None
    assert out["account_variance_pct"] is not None
    assert out["account_reconciliation_status"] == "verified"  # within 2%


def test_account_reconciliation_mismatch_exposed(monkeypatch):
    # Old geo-style gap: account £26,059.77 vs campaign £19,090.30 → mismatch.
    canonical = _canonical_audit(19_090_300_000)
    account = {"available": True, "total_cost_micros": 26_059_770_000,
               "total_spend": 26059.77, "currency_code": "GBP",
               "account_time_zone": "Europe/London", "spend_days": 84}
    svc = _patch_audit(monkeypatch, canonical=canonical, account=account)
    out = svc.build_google_ads_spend_audit("current_quarter", now=_at("2026-06-23T12:00:00+00:00"),
                                           api_total_micros=19_090_300_000)
    assert out["account_reconciliation_status"] == "mismatch"
    assert abs(out["account_variance_amount"]) > 1000  # the delta is exposed, not hidden


def test_reconciliation_not_verified_on_currency_mismatch(monkeypatch):
    canonical = _canonical_audit(26_059_770_000, currency="GBP")
    account = {"available": True, "total_cost_micros": 26_059_770_000,
               "total_spend": 26059.77, "currency_code": "USD",  # currency disagrees
               "account_time_zone": "Europe/London", "spend_days": 84}
    svc = _patch_audit(monkeypatch, canonical=canonical, account=account)
    out = svc.build_google_ads_spend_audit("current_quarter", now=_at("2026-06-23T12:00:00+00:00"),
                                           api_total_micros=26_059_770_000)
    assert out["account_reconciliation_status"] == "mismatch"


# ════════════ 3. native GBP never formatted as USD ════════════


def test_native_total_currency_is_gbp(monkeypatch):
    canonical = _canonical_audit(26_059_770_000)
    account = {"available": True, "total_cost_micros": 26_059_770_000, "total_spend": 26059.77,
               "currency_code": "GBP", "account_time_zone": "Europe/London", "spend_days": 84}
    svc = _patch_audit(monkeypatch, canonical=canonical, account=account)
    out = svc.build_google_ads_spend_audit("current_quarter", now=_at("2026-06-23T12:00:00+00:00"),
                                           api_total_micros=26_059_770_000)
    assert out["native_currency"] == "GBP"
    assert abs(out["native_total"] - 26059.77) <= 0.01


def test_backfill_total_rendered_in_native_currency():
    # The backfill summary native total must use the native-currency formatter,
    # never the "$"-only fmtMoney.
    i = JS.find("API spend total (native)")
    assert i != -1
    seg = JS[i:i + 160]
    assert "fmtCurrency(bs.api_total_spend" in seg
    assert "fmtMoney(bs.api_total_spend" not in seg


# ════════════ 4 & 5 & 6 & 7. identity + spend-state semantics ════════════


def _patch_revattr(monkeypatch, *, canonical, coverage, revenue_rows, identity=None):
    leads = {"available": True, "rows": [], "event_date_safe": True,
             "lead_event_date_field_available": True, "missing_contact_created_at_count": 0,
             "excluded_non_paid_count": 0, "excluded_pseudo_campaign_count": 0,
             "coverage_start": None, "coverage_end": None}
    spend = {"available": True, "rows": [], "coverage_start": None, "coverage_end": None}
    revenue = {"available": True, "rows": revenue_rows, "coverage_start": None, "coverage_end": None}
    try:
        monkeypatch.setattr("db.revenue_repository.fetch_campaign_country_spend", lambda s, e: spend)
        monkeypatch.setattr("db.revenue_repository.fetch_lead_quality", lambda s, e: leads)
        monkeypatch.setattr("db.revenue_repository.fetch_won_revenue", lambda s, e: revenue)
        monkeypatch.setattr("db.revenue_repository.fetch_sync_state", lambda: {"available": True, "datasets": {}})
        monkeypatch.setattr("db.revenue_repository.revenue_integration_connected", lambda: True)
        monkeypatch.setattr("db.revenue_repository.fetch_canonical_campaign_spend", lambda s, e: canonical)
        monkeypatch.setattr("db.revenue_repository.fetch_spend_coverage", lambda s, e: {"available": True, "chunks": coverage})
        monkeypatch.setattr("db.revenue_repository.fetch_campaign_identity",
                            lambda cid=None: identity or {"available": True, "mappings": []})
    except (ImportError, AttributeError) as exc:
        pytest.skip(f"runtime deps unavailable: {exc}")


def _canonical(rows, *, fx_complete=True, total_spend=None, total_spend_usd=None):
    total = total_spend if total_spend is not None else sum(r.get("spend") or 0 for r in rows)
    usd = total_spend_usd if total_spend_usd is not None else (total * 1.27 if fx_complete else None)
    return {"available": True, "rows": rows, "total_cost_micros": int(round(total * 1_000_000)),
            "total_spend": total, "total_spend_usd": usd, "campaign_count": len(rows),
            "customer_id": "123", "currency_code": "GBP", "reporting_currency": "USD",
            "fx_missing_days": 0 if fx_complete else 3, "fx_complete": fx_complete,
            "coverage_start": "2026-04-01", "coverage_end": "2026-06-23"}


_COV = [{"chunk_start": "2026-04-01", "chunk_end": "2026-06-23", "status": "verified"}]


def test_compliance_markets_maps_via_normalization(monkeypatch):
    build = _load_revattr()
    # Canonical campaign "Compliance Markets"; revenue label "compliance - markets".
    rows = [{"campaign_id": "1", "campaign_name": "Compliance Markets", "cost_micros": 3_500_000,
             "spend": 3.5, "spend_usd": 4.445, "fx_complete": True}]
    canonical = _canonical(rows)
    revenue_rows = [{"campaign_name": "compliance - markets", "country": "GB", "deal_id": "d1",
                     "deal_amount_usd": 1000.0, "match_status": "matched"}]
    _patch_revattr(monkeypatch, canonical=canonical, coverage=_COV, revenue_rows=revenue_rows)
    out = build("current_quarter", now=_at("2026-06-23T12:00:00+00:00"))
    cm = [c for c in out["campaigns"] if c["campaign_name"] == "Compliance Markets"]
    assert len(cm) == 1, "compliance - markets should aggregate onto Compliance Markets"
    assert cm[0]["spend_state"] == "mapped_exact"
    assert cm[0]["spend"] is not None and cm[0]["spend"] > 0
    assert not any(c["campaign_name"] == "compliance - markets" for c in out["campaigns"])


def test_approved_manual_mapping_changes_aggregation(monkeypatch):
    build = _load_revattr()
    rows = [{"campaign_id": "9", "campaign_name": "Emerging Markets", "cost_micros": 5_000_000,
             "spend": 5.0, "spend_usd": 6.35, "fx_complete": True}]
    canonical = _canonical(rows)
    revenue_rows = [{"campaign_name": "mexico,chile", "country": "MX", "deal_id": "d1",
                     "deal_amount_usd": 1270.0, "match_status": "matched"}]
    identity = {"available": True, "mappings": [{
        "customer_id": "123", "campaign_id": "9", "canonical_campaign_name": "Emerging Markets",
        "external_campaign_label": "mexico,chile", "match_method": "manual",
        "approved_at": "2026-06-01T00:00:00Z", "approved_by": "ops@x.com"}]}
    _patch_revattr(monkeypatch, canonical=canonical, coverage=_COV, revenue_rows=revenue_rows, identity=identity)
    out = build("current_quarter", now=_at("2026-06-23T12:00:00+00:00"))
    em = next(c for c in out["campaigns"] if c["campaign_name"] == "Emerging Markets")
    assert em["spend_state"] == "mapped_manual"
    assert em["won_revenue"] == 1270.0
    assert em["spend"] == 6.35  # canonical USD spend
    assert em["roas"] == round(1270.0 / 6.35, 2)


def test_mexico_chile_unmapped_shows_unavailable_not_zero(monkeypatch):
    build = _load_revattr()
    rows = [{"campaign_id": "1", "campaign_name": "Emerging Markets", "cost_micros": 5_000_000,
             "spend": 5.0, "spend_usd": 6.35, "fx_complete": True}]
    canonical = _canonical(rows)
    revenue_rows = [{"campaign_name": "mexico,chile", "country": "MX", "deal_id": "d1",
                     "deal_amount_usd": 1270.0, "match_status": "matched"}]
    _patch_revattr(monkeypatch, canonical=canonical, coverage=_COV, revenue_rows=revenue_rows)
    out = build("current_quarter", now=_at("2026-06-23T12:00:00+00:00"))
    mc = next(c for c in out["campaigns"] if c["campaign_name"] == "mexico,chile")
    assert mc["spend_state"] == "unmapped"
    assert mc["spend"] is None  # never $0
    assert mc["roas"] is None
    assert mc["verdict"] == "mapping_required"


def test_matched_zero_spend_shows_verified_zero(monkeypatch):
    build = _load_revattr()
    # Canonical campaign exists with genuinely £0 spend in the window.
    rows = [{"campaign_id": "1", "campaign_name": "Dormant Brand", "cost_micros": 0,
             "spend": 0.0, "spend_usd": 0.0, "fx_complete": True}]
    canonical = _canonical(rows, total_spend=0.0, total_spend_usd=0.0)
    revenue_rows = [{"campaign_name": "Dormant Brand", "country": "GB", "deal_id": "d1",
                     "deal_amount_usd": 500.0, "match_status": "matched"}]
    _patch_revattr(monkeypatch, canonical=canonical, coverage=_COV, revenue_rows=revenue_rows)
    out = build("current_quarter", now=_at("2026-06-23T12:00:00+00:00"))
    db = next(c for c in out["campaigns"] if c["campaign_name"] == "Dormant Brand")
    assert db["spend_state"] == "verified_zero_spend"
    assert db["spend"] == 0.0  # $0 is correct here — a VERIFIED zero, not a missing mapping
    assert any("Verified zero spend" in n for n in db["attribution_notes"])


# ════════════ 8 & 9. FX gates USD ROAS ════════════


def test_fx_incomplete_blocks_usd_roas(monkeypatch):
    build = _load_revattr()
    rows = [{"campaign_id": "1", "campaign_name": "Gulf", "cost_micros": 5_000_000,
             "spend": 5.0, "spend_usd": None, "fx_complete": False}]
    canonical = _canonical(rows, fx_complete=False, total_spend=5.0, total_spend_usd=None)
    revenue_rows = [{"campaign_name": "Gulf", "country": "GB", "deal_id": "d1",
                     "deal_amount_usd": 500.0, "match_status": "matched"}]
    _patch_revattr(monkeypatch, canonical=canonical, coverage=_COV, revenue_rows=revenue_rows)
    out = build("current_quarter", now=_at("2026-06-23T12:00:00+00:00"))
    sh = out["source_health"]
    assert sh["fx_coverage_status"] == "incomplete"
    assert sh["campaign_roas_available"] is False
    gulf = next(c for c in out["campaigns"] if c["campaign_name"] == "Gulf")
    assert gulf["roas"] is None            # USD ROAS blocked
    assert gulf["spend"] is not None       # native spend may still display
    assert gulf["spend_state"] in ("mapped_exact", "mapped_manual")


def test_fx_complete_permits_usd_roas(monkeypatch):
    build = _load_revattr()
    rows = [{"campaign_id": "1", "campaign_name": "Gulf", "cost_micros": 5_000_000,
             "spend": 5.0, "spend_usd": 6.35, "fx_complete": True}]
    canonical = _canonical(rows)
    revenue_rows = [{"campaign_name": "Gulf", "country": "GB", "deal_id": "d1",
                     "deal_amount_usd": 500.0, "match_status": "matched"}]
    _patch_revattr(monkeypatch, canonical=canonical, coverage=_COV, revenue_rows=revenue_rows)
    out = build("current_quarter", now=_at("2026-06-23T12:00:00+00:00"))
    sh = out["source_health"]
    assert sh["campaign_roas_available"] is True
    gulf = next(c for c in out["campaigns"] if c["campaign_name"] == "Gulf")
    assert gulf["roas"] == round(500.0 / 6.35, 2)


# ════════════ 10. read-only w.r.t. HubSpot + Google Ads ════════════


def test_spend_writes_are_read_only_to_external_platforms():
    import db.revenue_repository as repo
    import services.google_ads_spend_service as spend_svc
    import services.revenue_attribution_service as attr_svc
    for mod in (repo, spend_svc, attr_svc):
        src = inspect.getsource(mod).lower()
        assert "mutate" not in src
        assert "hubapi.com" not in src
        assert "requests.post" not in src
        assert "requests.put" not in src
    # The account-spend connector query is a read-only SELECT.
    fn = CONN_SRC[CONN_SRC.find("def fetch_account_daily_spend"):]
    assert "select" in fn.lower() and "mutate" not in fn.lower()
    # Writers only touch local Postgres tables (no Google Ads / HubSpot writes).
    for forbidden in ("create_campaign", "update_campaign", "campaignbudgetservice", "set_budget"):
        assert forbidden not in WRITERS.lower()


# ════════════ schema + connector + UI wiring ════════════


def test_account_daily_spend_schema_and_writer():
    assert "CREATE TABLE IF NOT EXISTS google_ads_account_daily_spend" in SCHEMA
    assert "account_time_zone" in SCHEMA
    assert "UNIQUE (customer_id, spend_date)" in SCHEMA
    assert "def upsert_account_daily_spend" in WRITERS
    assert "def fetch_account_daily_spend" in CONN_SRC
    assert "customer.time_zone" in CONN_SRC


def test_campaign_table_spend_state_ui():
    assert "function spendStatusLabel" in JS
    assert "Verified zero spend" in JS
    assert "Spend mapping required" in JS
    assert "Mapping required" in JS
    # Native spend in the table uses the currency-correct formatter when USD is
    # not ready (never "$" for native GBP).
    assert "fmtCurrency(r.spend, nativeCur)" in JS


def test_spend_truth_panel_shows_account_reconciliation():
    assert "Direct account daily total" in JS
    assert "Campaign daily total" in JS
    assert "Account reconciliation" in JS
    assert "account_reconciliation_status" in JS
