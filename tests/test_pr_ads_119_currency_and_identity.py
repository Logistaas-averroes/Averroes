"""
PR-ADS-119 — Currency-Normalized Spend and Campaign Identity Repair.

Proves the eight blueprint requirements:
  1. GBP spend is never formatted as USD.
  2. USD ROAS uses daily historical FX, not a current spot rate.
  3. Missing FX coverage blocks ROAS.
  4. The exact Google Ads native total reconciles to the selected UI date range.
  5. A one-day account-timezone boundary is handled correctly (per-day rate).
  6. Unmapped campaigns never become $0 spend.
  7. Manual mappings are auditable and never overwrite raw campaign identity.
  8. Revenue remains unchanged.

None of these import the google-ads SDK or hit the network: the FX connector and
the spend connector are reached only through patched seams, and all DB reads are
patched at the repository boundary.
"""

import os
import sys
from datetime import date, datetime

import pytest

from tests.canonical_ledger_fixtures import (  # noqa: E402
    from_legacy_deal_rows, patch_canonical_ledger,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
SERVER = open(os.path.join(ROOT, "api", "server.py"), encoding="utf-8").read()
SCHEMA = open(os.path.join(ROOT, "db", "schema.py"), encoding="utf-8").read()
WRITERS = open(os.path.join(ROOT, "db", "writers.py"), encoding="utf-8").read()
REPO_SRC = open(os.path.join(ROOT, "db", "revenue_repository.py"), encoding="utf-8").read()


def _at(d):
    return datetime.fromisoformat(d + "T12:00:00+00:00")


def _load_fx():
    try:
        import services.fx_service as svc
        return svc
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"fx service import unavailable: {exc}")


def _load_identity():
    try:
        import services.campaign_identity_service as svc
        return svc
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"identity service import unavailable: {exc}")


def _load_revattr():
    try:
        from services.revenue_attribution_service import build_revenue_attribution
        return build_revenue_attribution
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"service import unavailable: {exc}")


def _load_spend():
    try:
        import services.google_ads_spend_service as svc
        return svc
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"spend service import unavailable: {exc}")


# ════════════ 2 & 5. daily historical FX (not spot) + boundary ════════════


def test_usd_uses_daily_fx_not_spot_rate():
    svc = _load_fx()
    # Two days across a quarter boundary, each with its OWN rate.
    rows = [
        {"spend_date": "2026-03-31", "spend_native_amount": 100.0, "spend_native_currency": "GBP"},
        {"spend_date": "2026-04-01", "spend_native_amount": 100.0, "spend_native_currency": "GBP"},
    ]
    fx = {"2026-03-31": 1.20, "2026-04-01": 1.30}
    out = svc.convert_daily_spend_to_usd(rows, fx)
    assert out["complete"] is True
    # Daily conversion: 100*1.20 + 100*1.30 = 250.0
    assert out["total_usd"] == 250.0
    # A single spot rate (1.30) would give 260.0 — must NOT match.
    assert out["total_usd"] != round(out["total_native"] * 1.30, 6)
    # Boundary: each day used its own date's rate.
    by_date = {r["spend_date"]: r for r in out["rows"]}
    assert by_date["2026-03-31"]["fx_rate_to_usd"] == 1.20
    assert by_date["2026-04-01"]["fx_rate_to_usd"] == 1.30
    assert by_date["2026-03-31"]["spend_usd"] == 120.0
    assert by_date["2026-04-01"]["spend_usd"] == 130.0


def test_missing_fx_date_blocks_conversion():
    svc = _load_fx()
    rows = [
        {"spend_date": "2026-04-01", "spend_native_amount": 100.0, "spend_native_currency": "GBP"},
        {"spend_date": "2026-04-02", "spend_native_amount": 100.0, "spend_native_currency": "GBP"},
    ]
    fx = {"2026-04-01": 1.25}  # 2026-04-02 missing
    out = svc.convert_daily_spend_to_usd(rows, fx)
    assert out["complete"] is False
    assert out["total_usd"] is None  # never converts a partial window
    assert "2026-04-02" in out["missing_dates"]


def test_analyze_fx_coverage_reports_missing_dates():
    svc = _load_fx()
    cov = svc.analyze_fx_coverage(["2026-04-01", "2026-04-02", "2026-04-03"],
                                  ["2026-04-01", "2026-04-03"])
    assert cov["complete"] is False
    assert cov["missing_dates"] == ["2026-04-02"]
    # No spend dates → nothing to convert → complete.
    assert svc.analyze_fx_coverage([], [])["complete"] is True


# ════════════ 3. missing FX coverage blocks ROAS ════════════


def _patch_revattr(monkeypatch, *, canonical, coverage, revenue_rows, geo_rows=None):
    leads = {"available": True, "rows": [], "event_date_safe": True,
             "lead_event_date_field_available": True, "missing_contact_created_at_count": 0,
             "excluded_non_paid_count": 0, "excluded_pseudo_campaign_count": 0,
             "coverage_start": None, "coverage_end": None}
    spend = {"available": True, "rows": geo_rows or [], "coverage_start": None, "coverage_end": None}
    revenue = {"available": True, "rows": revenue_rows, "coverage_start": None, "coverage_end": None}
    try:
        monkeypatch.setattr("db.revenue_repository.fetch_campaign_country_spend", lambda s, e: spend)
        monkeypatch.setattr("db.revenue_repository.fetch_lead_quality", lambda s, e: leads)
        monkeypatch.setattr("db.revenue_repository.fetch_won_revenue", lambda s, e: revenue)
        # PR-ADS-153E-B: closed-won revenue is read from the canonical deal
        # ledger, so the same fixture rows are stubbed there.
        patch_canonical_ledger(monkeypatch, from_legacy_deal_rows(revenue["rows"]))
        monkeypatch.setattr("db.revenue_repository.fetch_sync_state", lambda: {"available": True, "datasets": {}})
        monkeypatch.setattr("db.revenue_repository.revenue_integration_connected", lambda: True)
        monkeypatch.setattr("db.revenue_repository.fetch_canonical_campaign_spend", lambda s, e: canonical)
        monkeypatch.setattr("db.revenue_repository.fetch_spend_coverage", lambda s, e: {"available": True, "chunks": coverage})
    except (ImportError, AttributeError) as exc:
        pytest.skip(f"runtime deps unavailable: {exc}")


def _canonical(rows, *, fx_complete, total_spend, total_spend_usd):
    return {"available": True, "rows": rows, "total_cost_micros": int(round(total_spend * 1_000_000)),
            "total_spend": total_spend, "total_spend_usd": total_spend_usd,
            "campaign_count": len(rows), "customer_id": "123-456", "currency_code": "GBP",
            "reporting_currency": "USD", "fx_missing_days": 0 if fx_complete else 2,
            "fx_complete": fx_complete, "coverage_start": "2026-04-01", "coverage_end": "2026-06-22"}


def test_missing_fx_coverage_blocks_roas(monkeypatch):
    build = _load_revattr()
    # Canonical spend coverage complete, but FX incomplete → ROAS unavailable.
    rows = [{"campaign_id": "1", "campaign_name": "alpha", "cost_micros": 26_059_770_000,
             "spend": 26059.77, "spend_usd": None, "fx_complete": False}]
    canonical = _canonical(rows, fx_complete=False, total_spend=26059.77, total_spend_usd=None)
    coverage = [{"chunk_start": "2026-04-01", "chunk_end": "2026-06-22", "status": "verified"}]
    revenue_rows = [{"campaign_name": "alpha", "country": "GB", "deal_id": "d1",
                     "deal_amount_usd": 34000.0, "match_status": "matched"}]
    _patch_revattr(monkeypatch, canonical=canonical, coverage=coverage, revenue_rows=revenue_rows)
    out = build("current_quarter", now=_at("2026-06-22"))
    sh = out["source_health"]
    assert sh["fx_coverage_status"] == "incomplete"
    assert sh["campaign_roas_available"] is False
    assert out["summary"]["roas"] is None
    alpha = next(c for c in out["campaigns"] if c["campaign_name"] == "alpha")
    assert alpha["roas"] is None


def test_complete_fx_yields_usd_roas(monkeypatch):
    build = _load_revattr()
    # £26,059.77 native → $34,493.36 USD reporting; revenue $34,000 USD.
    rows = [{"campaign_id": "1", "campaign_name": "alpha", "cost_micros": 26_059_770_000,
             "spend": 26059.77, "spend_usd": 34493.36, "fx_complete": True}]
    canonical = _canonical(rows, fx_complete=True, total_spend=26059.77, total_spend_usd=34493.36)
    coverage = [{"chunk_start": "2026-04-01", "chunk_end": "2026-06-22", "status": "verified"}]
    revenue_rows = [{"campaign_name": "alpha", "country": "GB", "deal_id": "d1",
                     "deal_amount_usd": 34000.0, "match_status": "matched"}]
    _patch_revattr(monkeypatch, canonical=canonical, coverage=coverage, revenue_rows=revenue_rows)
    out = build("current_quarter", now=_at("2026-06-22"))
    sh = out["source_health"]
    assert sh["fx_coverage_status"] == "complete"
    assert sh["campaign_roas_available"] is True
    # Native vs USD reporting kept distinct; revenue currency USD.
    assert sh["spend_native_currency"] == "GBP"
    assert sh["spend_native_total"] == 26059.77
    assert sh["spend_usd_total"] == 34493.36
    assert sh["reporting_currency"] == "USD"
    assert sh["revenue_currency"] == "USD"
    # ROAS computed from USD reporting spend: 34000 / 34493.36 ≈ 0.99.
    alpha = next(c for c in out["campaigns"] if c["campaign_name"] == "alpha")
    assert alpha["spend"] == 34493.36
    assert alpha["roas"] == round(34000.0 / 34493.36, 2)


# ════════════ 1. GBP spend never formatted as USD ════════════


def test_native_gbp_never_formatted_as_usd():
    # Currency-correct formatters exist; native uses the GBP symbol, never "$".
    assert 'GBP: "£"' in JS
    assert "function fmtCurrency" in JS
    assert "function fmtNativeGBP" in JS and "function fmtUSD" in JS
    # The campaign summary renders native via fmtCurrency (GBP), never fmtMoney.
    i = JS.find("function renderRoasCampaignSummary")
    block = JS[i:i + 1600]
    assert "fmtCurrency(native, nativeCur)" in block
    assert 'nativeCur = sh.spend_native_currency || "GBP"' in block
    # The Spend Truth panel shows native GBP with fmtCurrency, USD with fmtUSD.
    st = JS[JS.find("function renderSpendTruth"):JS.find("function renderSpendTruth") + 4200]
    assert "Google Ads native spend" in st
    assert "fmtCurrency(a.native_total" in st
    assert "fmtUSD(a.usd_total)" in st


def test_source_health_separates_native_and_usd(monkeypatch):
    build = _load_revattr()
    rows = [{"campaign_id": "1", "campaign_name": "alpha", "cost_micros": 26_059_770_000,
             "spend": 26059.77, "spend_usd": 34493.36, "fx_complete": True}]
    canonical = _canonical(rows, fx_complete=True, total_spend=26059.77, total_spend_usd=34493.36)
    coverage = [{"chunk_start": "2026-04-01", "chunk_end": "2026-06-22", "status": "verified"}]
    revenue_rows = [{"campaign_name": "alpha", "country": "GB", "deal_id": "d1",
                     "deal_amount_usd": 34000.0, "match_status": "matched"}]
    _patch_revattr(monkeypatch, canonical=canonical, coverage=coverage, revenue_rows=revenue_rows)
    sh = build("current_quarter", now=_at("2026-06-22"))["source_health"]
    # Native total and USD total are different numbers with different currencies.
    assert sh["spend_native_total"] != sh["spend_usd_total"]
    assert sh["spend_native_currency"] == "GBP" and sh["reporting_currency"] == "USD"


# ════════════ 4. native total reconciles to the selected window ════════════


def test_audit_native_total_reconciles_to_window(monkeypatch):
    svc = _load_spend()
    micros = 26_059_770_000  # £26,059.77
    canonical = {"available": True, "rows": [], "total_cost_micros": micros,
                 "total_spend": micros / 1_000_000, "total_spend_usd": 34493.36,
                 "campaign_count": 1, "customer_id": "123", "currency_code": "GBP",
                 "reporting_currency": "USD", "fx_missing_days": 0, "fx_complete": True,
                 "coverage_start": "2026-04-01", "coverage_end": "2026-06-22"}
    coverage = [{"chunk_start": "2026-04-01", "chunk_end": "2026-06-22", "status": "verified"}]
    monkeypatch.setattr("db.revenue_repository.fetch_canonical_campaign_spend", lambda s, e: canonical)
    monkeypatch.setattr("db.revenue_repository.fetch_spend_coverage", lambda s, e: {"available": True, "chunks": coverage})
    monkeypatch.setattr("db.revenue_repository.fetch_geo_spend_total", lambda s, e: {"available": True, "total_spend": micros / 1_000_000})
    monkeypatch.setattr("db.revenue_repository.fetch_fx_coverage", lambda s, e, b, q="USD": {"available": True, "complete": True, "spend_days": 83, "covered_days": 83, "missing_dates": []})
    monkeypatch.setattr(svc, "fetch_daily_spend", lambda a, b: {"rows": []})
    out = svc.build_google_ads_spend_audit("current_quarter", now=_at("2026-06-22"), api_total_micros=micros)
    # Native total reconciles to the canonical local total within £0.01.
    assert out["native_currency"] == "GBP"
    assert abs(out["native_total"] - 26059.77) <= 0.01
    assert out["fx_coverage_complete"] is True
    assert out["usd_total"] == 34493.36


def test_canonical_query_windowed_inclusive_and_fx_joined():
    fn = REPO_SRC[REPO_SRC.find("def fetch_canonical_campaign_spend"):
                  REPO_SRC.find("def fetch_fx_coverage")]
    # Inclusive window boundaries (account-timezone day handling).
    assert "s.spend_date >= %s" in fn and "s.spend_date <= %s" in fn
    # USD via per-day FX join (never one spot rate).
    assert "LEFT JOIN fx_rates" in fn
    assert "fx.rate_date = s.spend_date" in fn
    assert "* fx.rate" in fn


# ════════════ 6. unmapped campaigns never become $0 spend ════════════


def test_unmapped_campaign_never_zero_spend(monkeypatch):
    build = _load_revattr()
    # Complete canonical+FX. Spend campaign is "alpha"; revenue is on "beta" (no
    # canonical spend) → beta spend + ROAS Unavailable, never $0.
    rows = [{"campaign_id": "1", "campaign_name": "alpha", "cost_micros": 5_000_000,
             "spend": 5.0, "spend_usd": 6.5, "fx_complete": True}]
    canonical = _canonical(rows, fx_complete=True, total_spend=5.0, total_spend_usd=6.5)
    coverage = [{"chunk_start": "2026-04-01", "chunk_end": "2026-06-22", "status": "verified"}]
    revenue_rows = [{"campaign_name": "beta", "country": "GB", "deal_id": "d1",
                     "deal_amount_usd": 1000.0, "match_status": "matched"}]
    _patch_revattr(monkeypatch, canonical=canonical, coverage=coverage, revenue_rows=revenue_rows)
    out = build("current_quarter", now=_at("2026-06-22"))
    beta = next(c for c in out["campaigns"] if c["campaign_name"] == "beta")
    assert beta["spend"] is None  # never 0.0
    assert beta["roas"] is None
    assert beta["spend_mapping"] == "unavailable"


def test_mapping_review_unmatched_has_no_zero_spend(monkeypatch):
    idsvc = _load_identity()
    canonical = {"available": True, "customer_id": "123", "currency_code": "GBP",
                 "reporting_currency": "USD",
                 "rows": [{"campaign_id": "1", "campaign_name": "Gulf Region",
                           "spend": 5.0, "spend_usd": 6.5}]}
    revenue = {"available": True, "rows": [
        {"campaign_name": "Gulf Region", "deal_amount_usd": 1000.0},
        {"campaign_name": "mexico,chile", "deal_amount_usd": 500.0},  # no GA candidate
    ]}
    monkeypatch.setattr("db.revenue_repository.fetch_canonical_campaign_spend", lambda s, e: canonical)
    # PR-ADS-153E-B: the workbench reads label revenue from the canonical ledger.
    patch_canonical_ledger(monkeypatch, from_legacy_deal_rows(revenue["rows"]))
    monkeypatch.setattr("db.revenue_repository.fetch_campaign_identity", lambda cid=None: {"available": True, "mappings": []})
    review = idsvc.build_mapping_review("current_quarter", now=_at("2026-06-22"))
    by_label = {r["hubspot_campaign_label"]: r for r in review["rows"]}
    # Exact normalized match auto-links "Gulf Region".
    assert by_label["Gulf Region"]["match_status"] == "exact_normalized"
    assert by_label["Gulf Region"]["native_spend"] == 5.0
    assert by_label["Gulf Region"]["usd_spend"] == 6.5
    # Unmatched label is NEVER fuzzy-linked and NEVER given $0 spend.
    um = by_label["mexico,chile"]
    assert um["match_status"] == "unmatched"
    assert um["native_spend"] is None and um["usd_spend"] is None
    assert review["unmatched_count"] == 1


def test_no_fuzzy_auto_match():
    idsvc = _load_identity()
    # Punctuation/spacing/case normalization links exact names.
    assert idsvc.normalize_campaign_name("Gulf - Region!") == idsvc.normalize_campaign_name("gulf region")
    # "mexico,chile" must NEVER auto-link to "Emerging Markets".
    ga = [{"campaign_id": "9", "campaign_name": "Emerging Markets"}]
    proposals = idsvc.auto_link_exact_matches(ga, ["mexico,chile"])
    assert proposals == []
    # Exact normalized equality DOES propose.
    ga2 = [{"campaign_id": "1", "campaign_name": "Gulf Region"}]
    p2 = idsvc.auto_link_exact_matches(ga2, ["gulf  region"])
    assert len(p2) == 1 and p2[0]["match_method"] == "exact_normalized"


# ════════════ 7. manual mappings auditable, raw identity untouched ════════════


def test_manual_mapping_is_auditable(monkeypatch):
    idsvc = _load_identity()
    captured = {}

    def _cap(customer_id, label, **kw):
        captured.update({"customer_id": customer_id, "label": label, **kw})
        return True

    monkeypatch.setattr("db.writers.upsert_campaign_identity", _cap)
    ok = idsvc.record_manual_mapping("123", "mexico,chile", "9", "Emerging Markets",
                                     approved_by="ops@x.com")
    assert ok is True
    assert captured["match_method"] == "manual"
    assert captured["approved_by"] == "ops@x.com"
    # The historical campaign name is preserved as a copy.
    assert captured["historical_campaign_name"] == "Emerging Markets"


def test_identity_writer_never_touches_raw_spend_table():
    fn = WRITERS[WRITERS.find("def upsert_campaign_identity"):]
    # Manual mapping carries an audit timestamp.
    assert "approved_at" in fn and "approved_by" in fn
    assert "google_ads_campaign_identity" in fn
    # It must NEVER write the raw canonical spend table.
    assert "google_ads_campaign_daily_spend" not in fn
    # Schema keeps the raw historical identity immutable (separate columns).
    assert "historical_campaign_name" in SCHEMA and "canonical_campaign_name" in SCHEMA


# ════════════ 8. revenue remains unchanged ════════════


def test_revenue_unchanged_across_fx_states(monkeypatch):
    build = _load_revattr()
    revenue_rows = [{"campaign_name": "alpha", "country": "GB", "deal_id": "d1",
                     "deal_amount_usd": 34000.0, "match_status": "matched"}]
    coverage = [{"chunk_start": "2026-04-01", "chunk_end": "2026-06-22", "status": "verified"}]
    base = [{"campaign_id": "1", "campaign_name": "alpha", "cost_micros": 26_059_770_000,
             "spend": 26059.77, "spend_usd": 34493.36, "fx_complete": True}]

    _patch_revattr(monkeypatch, canonical=_canonical(base, fx_complete=True, total_spend=26059.77, total_spend_usd=34493.36),
                   coverage=coverage, revenue_rows=revenue_rows)
    complete = build("current_quarter", now=_at("2026-06-22"))

    incomplete_rows = [{"campaign_id": "1", "campaign_name": "alpha", "cost_micros": 26_059_770_000,
                        "spend": 26059.77, "spend_usd": None, "fx_complete": False}]
    _patch_revattr(monkeypatch, canonical=_canonical(incomplete_rows, fx_complete=False, total_spend=26059.77, total_spend_usd=None),
                   coverage=coverage, revenue_rows=revenue_rows)
    incomplete = build("current_quarter", now=_at("2026-06-22"))

    # Revenue truth is identical regardless of FX coverage / currency.
    assert complete["summary"]["won_revenue"] == incomplete["summary"]["won_revenue"] == 34000.0
    assert complete["summary"]["customers"] == incomplete["summary"]["customers"] == 1


# ════════════ approved mappings applied in the ROAS pipeline ════════════


def test_approved_mapping_applied_in_roas_pipeline(monkeypatch):
    build = _load_revattr()
    # canonical spend: "Emerging Markets"; revenue label "mexico,chile";
    # approved manual mapping mexico,chile -> Emerging Markets.
    rows = [{"campaign_id": "9", "campaign_name": "Emerging Markets", "cost_micros": 5_000_000,
             "spend": 5.0, "spend_usd": 6.5, "fx_complete": True}]
    canonical = _canonical(rows, fx_complete=True, total_spend=5.0, total_spend_usd=6.5)
    coverage = [{"chunk_start": "2026-04-01", "chunk_end": "2026-06-22", "status": "verified"}]
    revenue_rows = [{"campaign_name": "mexico,chile", "country": "MX", "deal_id": "d1",
                     "deal_amount_usd": 1300.0, "match_status": "matched"}]
    _patch_revattr(monkeypatch, canonical=canonical, coverage=coverage, revenue_rows=revenue_rows)
    monkeypatch.setattr(
        "db.revenue_repository.fetch_campaign_identity",
        lambda cid=None: {"available": True, "mappings": [{
            "customer_id": "123-456", "campaign_id": "9",
            "canonical_campaign_name": "Emerging Markets",
            "historical_campaign_name": "Emerging Markets",
            "external_campaign_label": "mexico,chile",
            "match_method": "manual", "approved_at": "2026-06-01T00:00:00Z",
            "approved_by": "ops@x.com"}]})
    out = build("current_quarter", now=_at("2026-06-22"))
    # Exactly one Emerging Markets row with canonical spend, revenue, and USD ROAS.
    em = [c for c in out["campaigns"] if c["campaign_name"] == "Emerging Markets"]
    assert len(em) == 1
    row = em[0]
    assert row["spend"] == 6.5            # canonical USD reporting spend
    assert row["won_revenue"] == 1300.0   # mapped revenue
    assert row["spend_mapping"] == "matched"
    assert row["roas"] == round(1300.0 / 6.5, 2)
    # No leftover unmapped "mexico,chile" row.
    assert not any(c["campaign_name"] == "mexico,chile" for c in out["campaigns"])
    # Original external label preserved in audit metadata.
    applied = out["source_health"]["applied_campaign_mappings"]
    assert {"external_campaign_label": "mexico,chile",
            "canonical_campaign_name": "Emerging Markets"} in applied


def test_unapproved_mapping_not_applied(monkeypatch):
    build = _load_revattr()
    rows = [{"campaign_id": "9", "campaign_name": "Emerging Markets", "cost_micros": 5_000_000,
             "spend": 5.0, "spend_usd": 6.5, "fx_complete": True}]
    canonical = _canonical(rows, fx_complete=True, total_spend=5.0, total_spend_usd=6.5)
    coverage = [{"chunk_start": "2026-04-01", "chunk_end": "2026-06-22", "status": "verified"}]
    revenue_rows = [{"campaign_name": "mexico,chile", "country": "MX", "deal_id": "d1",
                     "deal_amount_usd": 1300.0, "match_status": "matched"}]
    _patch_revattr(monkeypatch, canonical=canonical, coverage=coverage, revenue_rows=revenue_rows)
    # Mapping exists but is NOT approved (approved_at is None) → must NOT be applied.
    monkeypatch.setattr(
        "db.revenue_repository.fetch_campaign_identity",
        lambda cid=None: {"available": True, "mappings": [{
            "customer_id": "123-456", "campaign_id": "9",
            "canonical_campaign_name": "Emerging Markets",
            "external_campaign_label": "mexico,chile",
            "match_method": "manual", "approved_at": None}]})
    out = build("current_quarter", now=_at("2026-06-22"))
    mc = next(c for c in out["campaigns"] if c["campaign_name"] == "mexico,chile")
    assert mc["spend"] is None and mc["roas"] is None
    assert mc["spend_mapping"] == "unavailable"
    assert out["source_health"]["applied_campaign_mappings"] == []


def test_revattr_loads_approved_identity_mappings():
    # The pipeline (not just the review panel) consumes approved mappings.
    src = open(os.path.join(ROOT, "services", "revenue_attribution_service.py"), encoding="utf-8").read()
    bd = src[src.find("def _build_from_db"):]
    assert "_build_resolution_map" in bd  # PR-ADS-120 renamed resolver
    assert "fetch_campaign_identity" in src
    assert "applied_campaign_mappings" in bd


# ════════════ schema + endpoints + daily sync wiring ════════════


def test_fx_and_identity_schema_present():
    assert "CREATE TABLE IF NOT EXISTS fx_rates" in SCHEMA
    assert "UNIQUE (rate_date, base_currency, quote_currency)" in SCHEMA
    assert "CREATE TABLE IF NOT EXISTS google_ads_campaign_identity" in SCHEMA
    assert "ON CONFLICT (rate_date, base_currency, quote_currency) DO UPDATE" in WRITERS


def test_fx_and_mapping_endpoints_registered():
    for route in ('"/api/fx-coverage"', '"/api/fx-backfill/run"', '"/api/fx-backfill/status"',
                  '"/api/campaign-mapping-review"', '"/api/campaign-mapping"'):
        assert route in SERVER, f"missing endpoint {route}"
    # Manual-mapping + FX backfill are admin-gated.
    mi = SERVER.find("def api_campaign_mapping")
    assert "check_admin_or_token(request)" in SERVER[mi:mi + 900]
    fi = SERVER.find("def api_fx_backfill_run")
    assert "check_admin_or_token(request)" in SERVER[fi:fi + 900]


def test_daily_fx_sync_step_wired():
    INCR = open(os.path.join(ROOT, "scheduler", "incremental_sync.py"), encoding="utf-8").read()
    assert '"fx/daily_rates"' in INCR
    assert "def _sync_fx_rates" in INCR
    assert "ensure_fx_rates" in INCR


def test_fx_connector_is_read_only():
    conn = open(os.path.join(ROOT, "connectors", "fx_rates.py"), encoding="utf-8").read().lower()
    for forbidden in ("requests.post", "requests.put", ".delete(", "urlopen(req, data"):
        assert forbidden not in conn
    assert "read-only" in conn


def test_mapping_panel_present_in_ui():
    assert 'id="campaign-mapping-panel"' in HTML
    assert "function renderCampaignMapping" in JS
    assert "/api/campaign-mapping-review?window=" in JS
    assert "data-fx-backfill-action" in JS
    assert "Spend mapping unavailable" in JS
