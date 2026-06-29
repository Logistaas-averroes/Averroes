"""
PR-ADS-120b — Campaign Mapping Workbench.

Turns Campaign Identity Mapping Review from a read-only audit into an admin
mapping workbench. Proves the blueprint requirements:

  1. Unmatched rows expose scored Google Ads candidates (exact + fuzzy + all
     active/spending + zero-spend-known-identity + archived).
  2. A manual mapping saves successfully and is auditable.
  3. A saved mapping applies to ROAS by Campaign.
  4. Mapped spend is NOT duplicated when multiple labels map to one campaign
     (the canonical campaign shows once, with aliases underneath).
  5. Unmatched spend never displays $0.
  6. "Not Google Ads" labels are excluded from Google Ads ROAS (never a fake $0).
  7. A non-admin cannot save mappings or exclusions.
  8. The raw Google Ads campaign identity is never overwritten.
  9. Exact auto-matches still work.

No google-ads SDK import and no network: connectors are reached only through
patched seams and all DB reads are patched at the repository boundary.
"""

import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
SERVER = open(os.path.join(ROOT, "api", "server.py"), encoding="utf-8").read()
SCHEMA = open(os.path.join(ROOT, "db", "schema.py"), encoding="utf-8").read()
WRITERS = open(os.path.join(ROOT, "db", "writers.py"), encoding="utf-8").read()
REPO_SRC = open(os.path.join(ROOT, "db", "revenue_repository.py"), encoding="utf-8").read()


def _at(d):
    return datetime.fromisoformat(d)


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


# ════════════ candidate pool + fuzzy ranking (no auto-link) ════════════


def _patch_review(monkeypatch, *, canonical, revenue, identity=None, known=None, leads=None):
    monkeypatch.setattr("db.revenue_repository.fetch_canonical_campaign_spend", lambda s, e: canonical)
    monkeypatch.setattr("db.revenue_repository.fetch_won_revenue", lambda s, e: revenue)
    monkeypatch.setattr("db.revenue_repository.fetch_campaign_identity",
                        lambda cid=None: identity or {"available": True, "mappings": []})
    monkeypatch.setattr("db.revenue_repository.fetch_all_known_campaigns",
                        lambda: known or {"available": True, "campaigns": []})
    monkeypatch.setattr("db.revenue_repository.fetch_lead_quality",
                        lambda s, e: leads or {"available": True, "rows": []})


def test_unmatched_rows_expose_candidates(monkeypatch):
    idsvc = _load_identity()
    canonical = {"available": True, "customer_id": "3059734490", "currency_code": "GBP",
                 "reporting_currency": "USD",
                 "rows": [{"campaign_id": "123", "campaign_name": "Mexico, Chile, Colombia",
                           "spend": 5200.12, "spend_usd": 7000.44},
                          {"campaign_id": "200", "campaign_name": "Gulf",
                           "spend": 10.0, "spend_usd": 13.0}]}
    revenue = {"available": True, "rows": [
        {"campaign_name": "mexico,chile", "deal_id": "d1", "deal_amount_usd": 7050.0},
    ]}
    # A zero-window-spend but known-identity campaign + an archived campaign.
    known = {"available": True, "campaigns": [
        {"campaign_id": "123", "campaign_name": "Mexico, Chile, Colombia",
         "had_historical_spend": True, "last_spend_date": "2026-06-20"},
        {"campaign_id": "200", "campaign_name": "Gulf",
         "had_historical_spend": True, "last_spend_date": "2026-06-20"},
        {"campaign_id": "900", "campaign_name": "Dormant Brand",
         "had_historical_spend": False, "last_spend_date": None},
        {"campaign_id": "901", "campaign_name": "Europe Low-CPC-2026 (archived)",
         "had_historical_spend": True, "last_spend_date": "2025-01-01"},
    ]}
    _patch_review(monkeypatch, canonical=canonical, revenue=revenue, known=known)
    review = idsvc.build_mapping_review("current_quarter", now=_at("2026-06-29T12:00:00"))
    by_label = {r["external_campaign_label"]: r for r in review["rows"]}
    row = by_label["mexico,chile"]
    assert row["status"] == "unmatched"
    assert review["unmatched_count"] == 1
    # Per-blueprint candidate object shape.
    cands = row["candidates"]
    assert cands, "unmatched row must expose candidates"
    top = cands[0]
    for key in ("campaign_id", "canonical_campaign_name", "native_spend",
                "native_currency", "usd_spend", "match_score", "match_reason"):
        assert key in top, f"candidate missing {key}"
    # Best candidate is the fuzzy "Mexico, Chile, Colombia" (never auto-linked).
    assert top["canonical_campaign_name"] == "Mexico, Chile, Colombia"
    assert top["match_reason"] == "fuzzy_name"
    assert top["match_score"] >= 0.5
    assert top["native_spend"] == 5200.12 and top["usd_spend"] == 7000.44
    # The full controlled list is present: active/spending, zero-spend-known
    # identity, AND archived/removed campaigns are all selectable.
    names = {c["canonical_campaign_name"] for c in cands}
    assert {"Mexico, Chile, Colombia", "Gulf", "Dormant Brand",
            "Europe Low-CPC-2026 (archived)"} <= names
    reasons = {c["canonical_campaign_name"]: c["match_reason"] for c in cands}
    assert reasons["Gulf"] in ("active_spend", "fuzzy_name")
    assert reasons["Dormant Brand"] == "zero_spend_known_identity"
    assert reasons["Europe Low-CPC-2026 (archived)"] == "historical_spend"
    # Top-level searchable dropdown source is also exposed.
    assert any(o["canonical_campaign_name"] == "Dormant Brand" for o in review["campaign_options"])


def test_fuzzy_ranks_but_never_autolinks():
    idsvc = _load_identity()
    # Fuzzy scorer ranks a strong near-match high…
    assert idsvc.fuzzy_match_score("mexico,chile", "Mexico, Chile, Colombia") >= 0.5
    assert idsvc.fuzzy_match_score("competitors - mediumcpc", "Global - Competitors") >= 0.5
    # …but identity normalization still NEVER auto-links a fuzzy pair.
    ga = [{"campaign_id": "9", "campaign_name": "Mexico, Chile, Colombia"}]
    assert idsvc.auto_link_exact_matches(ga, ["mexico,chile"]) == []
    # Exact normalized equality DOES auto-link (case/space/punctuation only).
    assert idsvc.fuzzy_match_score("Gulf - Region!", "gulf region") == 1.0


def test_unmatched_spend_never_zero(monkeypatch):
    idsvc = _load_identity()
    canonical = {"available": True, "customer_id": "123", "currency_code": "GBP",
                 "reporting_currency": "USD",
                 "rows": [{"campaign_id": "1", "campaign_name": "Gulf Region",
                           "spend": 5.0, "spend_usd": 6.5}]}
    revenue = {"available": True, "rows": [
        {"campaign_name": "Gulf Region", "deal_id": "d0", "deal_amount_usd": 1000.0},
        {"campaign_name": "mexico,chile", "deal_id": "d1", "deal_amount_usd": 500.0},
    ]}
    _patch_review(monkeypatch, canonical=canonical, revenue=revenue)
    review = idsvc.build_mapping_review("current_quarter", now=_at("2026-06-29T12:00:00"))
    by_label = {r["external_campaign_label"]: r for r in review["rows"]}
    um = by_label["mexico,chile"]
    assert um["status"] == "unmatched"
    assert um["native_spend"] is None and um["usd_spend"] is None  # never $0
    assert um["revenue_usd"] == 500.0


def test_exact_auto_match_still_works(monkeypatch):
    idsvc = _load_identity()
    canonical = {"available": True, "customer_id": "123", "currency_code": "GBP",
                 "reporting_currency": "USD",
                 "rows": [{"campaign_id": "1", "campaign_name": "Gulf Region",
                           "spend": 5.0, "spend_usd": 6.5}]}
    revenue = {"available": True, "rows": [
        {"campaign_name": "gulf  region", "deal_id": "d0", "deal_amount_usd": 1000.0}]}
    _patch_review(monkeypatch, canonical=canonical, revenue=revenue)
    review = idsvc.build_mapping_review("current_quarter", now=_at("2026-06-29T12:00:00"))
    row = review["rows"][0]
    assert row["match_status"] == "exact_normalized"
    assert row["status"] == "matched"
    assert row["native_spend"] == 5.0 and row["usd_spend"] == 6.5


def test_leads_sqls_customers_surfaced(monkeypatch):
    idsvc = _load_identity()
    canonical = {"available": True, "customer_id": "123", "currency_code": "GBP",
                 "reporting_currency": "USD", "rows": []}
    revenue = {"available": True, "rows": [
        {"campaign_name": "mexico,chile", "deal_id": "d1", "deal_amount_usd": 500.0},
        {"campaign_name": "mexico,chile", "deal_id": "d2", "deal_amount_usd": 700.0}]}
    leads = {"available": True, "rows": [
        {"campaign_name": "mexico,chile", "status_category": "qualified"},
        {"campaign_name": "mexico,chile", "status_category": "unqualified"}]}
    _patch_review(monkeypatch, canonical=canonical, revenue=revenue, leads=leads)
    review = idsvc.build_mapping_review("current_quarter", now=_at("2026-06-29T12:00:00"))
    row = {r["external_campaign_label"]: r for r in review["rows"]}["mexico,chile"]
    assert row["customers"] == 2          # distinct deals
    assert row["leads"] == 2 and row["sqls"] == 1
    assert review["leads_available"] is True


# ════════════ manual mapping save + exclusion are auditable, read-only ════════════


def test_manual_mapping_is_auditable(monkeypatch):
    idsvc = _load_identity()
    captured = {}

    def _cap(customer_id, label, **kw):
        captured.update({"customer_id": customer_id, "label": label, **kw})
        return True

    monkeypatch.setattr("db.writers.upsert_campaign_identity", _cap)
    ok = idsvc.record_manual_mapping("3059734490", "mexico,chile", "123",
                                     "Mexico, Chile, Colombia", approved_by="ops@x.com")
    assert ok is True
    assert captured["match_method"] == "manual"
    assert captured["approved_by"] == "ops@x.com"
    assert captured["campaign_id"] == "123"


def test_record_exclusion_marks_not_google_ads(monkeypatch):
    idsvc = _load_identity()
    captured = {}

    def _cap(customer_id, label, **kw):
        captured.update({"customer_id": customer_id, "label": label, **kw})
        return True

    monkeypatch.setattr("db.writers.upsert_campaign_identity", _cap)
    ok = idsvc.record_exclusion("3059734490", "bad utm source",
                                approved_by="ops@x.com", reason="bad UTM")
    assert ok is True
    assert captured["match_method"] == "not_google_ads"
    assert captured["approved_by"] == "ops@x.com"
    # Identity-only exclusion: NO canonical Google Ads campaign is invented.
    assert captured["campaign_id"] is None
    assert captured["canonical_campaign_name"] is None


def test_exclusion_is_approved_and_never_touches_raw_spend_table():
    fn = WRITERS[WRITERS.find("def upsert_campaign_identity"):]
    # "not_google_ads" carries an audit timestamp (approved), like manual.
    assert '"not_google_ads"' in fn
    # The identity writer must NEVER write the raw canonical spend table.
    assert "google_ads_campaign_daily_spend" not in fn
    # Schema documents the new method and keeps the raw identity immutable.
    assert "not_google_ads" in SCHEMA


def test_known_campaign_fetch_is_read_only():
    fn = REPO_SRC[REPO_SRC.find("def fetch_all_known_campaigns"):]
    assert "SELECT" in fn
    for forbidden in ("INSERT", "UPDATE ", "DELETE"):
        assert forbidden not in fn.upper()


# ════════════ saved mapping applies to ROAS by Campaign ════════════


def _patch_revattr(monkeypatch, *, canonical, coverage, revenue_rows, identity=None):
    leads = {"available": True, "rows": [], "event_date_safe": True,
             "lead_event_date_field_available": True, "missing_contact_created_at_count": 0,
             "excluded_non_paid_count": 0, "excluded_pseudo_campaign_count": 0,
             "coverage_start": None, "coverage_end": None}
    spend = {"available": True, "rows": [], "coverage_start": None, "coverage_end": None}
    revenue = {"available": True, "rows": revenue_rows, "coverage_start": None, "coverage_end": None}
    monkeypatch.setattr("db.revenue_repository.fetch_campaign_country_spend", lambda s, e: spend)
    monkeypatch.setattr("db.revenue_repository.fetch_lead_quality", lambda s, e: leads)
    monkeypatch.setattr("db.revenue_repository.fetch_won_revenue", lambda s, e: revenue)
    monkeypatch.setattr("db.revenue_repository.fetch_sync_state", lambda: {"available": True, "datasets": {}})
    monkeypatch.setattr("db.revenue_repository.revenue_integration_connected", lambda: True)
    monkeypatch.setattr("db.revenue_repository.fetch_canonical_campaign_spend", lambda s, e: canonical)
    monkeypatch.setattr("db.revenue_repository.fetch_spend_coverage", lambda s, e: {"available": True, "chunks": coverage})
    monkeypatch.setattr("db.revenue_repository.fetch_campaign_identity",
                        lambda cid=None: identity or {"available": True, "mappings": []})


def _canonical(rows, *, fx_complete=True, total_spend=None, total_spend_usd=None):
    total = total_spend if total_spend is not None else sum(r.get("spend") or 0 for r in rows)
    usd = total_spend_usd if total_spend_usd is not None else (total * 1.27 if fx_complete else None)
    return {"available": True, "rows": rows, "total_cost_micros": int(round(total * 1_000_000)),
            "total_spend": total, "total_spend_usd": usd, "campaign_count": len(rows),
            "customer_id": "123", "currency_code": "GBP", "reporting_currency": "USD",
            "fx_missing_days": 0 if fx_complete else 3, "fx_complete": fx_complete,
            "coverage_start": "2026-04-01", "coverage_end": "2026-06-23"}


_COV = [{"chunk_start": "2026-04-01", "chunk_end": "2026-06-23", "status": "verified"}]


def test_saved_mapping_applies_to_roas(monkeypatch):
    build = _load_revattr()
    rows = [{"campaign_id": "9", "campaign_name": "Mexico, Chile, Colombia", "cost_micros": 5_000_000,
             "spend": 5.0, "spend_usd": 6.35, "fx_complete": True}]
    canonical = _canonical(rows)
    revenue_rows = [{"campaign_name": "mexico,chile", "country": "MX", "deal_id": "d1",
                     "deal_amount_usd": 1270.0, "match_status": "matched"}]
    identity = {"available": True, "mappings": [{
        "customer_id": "123", "campaign_id": "9", "canonical_campaign_name": "Mexico, Chile, Colombia",
        "external_campaign_label": "mexico,chile", "match_method": "manual",
        "approved_at": "2026-06-01T00:00:00Z", "approved_by": "ops@x.com"}]}
    _patch_revattr(monkeypatch, canonical=canonical, coverage=_COV, revenue_rows=revenue_rows, identity=identity)
    out = build("current_quarter", now=_at("2026-06-23T12:00:00+00:00"))
    mc = next(c for c in out["campaigns"] if c["campaign_name"] == "Mexico, Chile, Colombia")
    assert mc["spend_state"] == "mapped_manual"
    assert mc["won_revenue"] == 1270.0
    assert mc["spend"] == 6.35
    assert mc["roas"] == round(1270.0 / 6.35, 2)
    # The original external label is exposed as an alias of the canonical campaign.
    assert mc["aliases"] == ["mexico,chile"]
    assert not any(c["campaign_name"] == "mexico,chile" for c in out["campaigns"])


def test_mapped_spend_not_duplicated_with_aliases(monkeypatch):
    build = _load_revattr()
    # ONE canonical campaign, TWO external labels both mapped onto it.
    rows = [{"campaign_id": "9", "campaign_name": "Mexico, Chile, Colombia", "cost_micros": 5_000_000,
             "spend": 5.0, "spend_usd": 6.35, "fx_complete": True}]
    canonical = _canonical(rows)
    revenue_rows = [
        {"campaign_name": "mexico,chile", "country": "MX", "deal_id": "d1",
         "deal_amount_usd": 1000.0, "match_status": "matched"},
        {"campaign_name": "sa 2 | medium cpc (latin america)", "country": "MX", "deal_id": "d2",
         "deal_amount_usd": 300.0, "match_status": "matched"},
    ]
    identity = {"available": True, "mappings": [
        {"customer_id": "123", "campaign_id": "9", "canonical_campaign_name": "Mexico, Chile, Colombia",
         "external_campaign_label": "mexico,chile", "match_method": "manual",
         "approved_at": "2026-06-01T00:00:00Z", "approved_by": "ops@x.com"},
        {"customer_id": "123", "campaign_id": "9", "canonical_campaign_name": "Mexico, Chile, Colombia",
         "external_campaign_label": "sa 2 | medium cpc (latin america)", "match_method": "manual",
         "approved_at": "2026-06-01T00:00:00Z", "approved_by": "ops@x.com"},
    ]}
    _patch_revattr(monkeypatch, canonical=canonical, coverage=_COV, revenue_rows=revenue_rows, identity=identity)
    out = build("current_quarter", now=_at("2026-06-23T12:00:00+00:00"))
    mc = [c for c in out["campaigns"] if c["campaign_name"] == "Mexico, Chile, Colombia"]
    assert len(mc) == 1, "canonical campaign must appear exactly once"
    row = mc[0]
    # Spend is the canonical spend ONCE — not summed/duplicated across the 2 labels.
    assert row["spend"] == 6.35
    assert row["won_revenue"] == 1300.0  # revenue from both labels aggregates
    assert set(row["aliases"]) == {"mexico,chile", "sa 2 | medium cpc (latin america)"}


# ════════════ "Not Google Ads" excluded from Google Ads ROAS ════════════


def test_not_google_ads_excluded_from_roas(monkeypatch):
    build = _load_revattr()
    rows = [{"campaign_id": "1", "campaign_name": "Gulf", "cost_micros": 5_000_000,
             "spend": 5.0, "spend_usd": 6.35, "fx_complete": True}]
    canonical = _canonical(rows)
    revenue_rows = [
        {"campaign_name": "Gulf", "country": "GB", "deal_id": "d1",
         "deal_amount_usd": 500.0, "match_status": "matched"},
        {"campaign_name": "offline import 2024", "country": "GB", "deal_id": "d2",
         "deal_amount_usd": 9000.0, "match_status": "matched"},
    ]
    identity = {"available": True, "mappings": [{
        "customer_id": "123", "campaign_id": None, "canonical_campaign_name": None,
        "external_campaign_label": "offline import 2024", "match_method": "not_google_ads",
        "approved_at": "2026-06-01T00:00:00Z", "approved_by": "ops@x.com"}]}
    _patch_revattr(monkeypatch, canonical=canonical, coverage=_COV, revenue_rows=revenue_rows, identity=identity)
    out = build("current_quarter", now=_at("2026-06-23T12:00:00+00:00"))
    names = {c["campaign_name"] for c in out["campaigns"]}
    # Excluded label is gone from Google Ads ROAS entirely (never a $0 row).
    assert "offline import 2024" not in names
    assert "Gulf" in names
    assert out["source_health"]["excluded_campaign_labels"] == ["offline import 2024"]


def test_excluded_label_shows_not_google_ads_in_review(monkeypatch):
    idsvc = _load_identity()
    canonical = {"available": True, "customer_id": "123", "currency_code": "GBP",
                 "reporting_currency": "USD", "rows": []}
    revenue = {"available": True, "rows": [
        {"campaign_name": "offline import 2024", "deal_id": "d2", "deal_amount_usd": 9000.0}]}
    identity = {"available": True, "mappings": [{
        "customer_id": "123", "campaign_id": None, "canonical_campaign_name": None,
        "external_campaign_label": "offline import 2024", "match_method": "not_google_ads",
        "approved_at": "2026-06-01T00:00:00Z", "approved_by": "ops@x.com"}]}
    _patch_review(monkeypatch, canonical=canonical, revenue=revenue, identity=identity)
    review = idsvc.build_mapping_review("current_quarter", now=_at("2026-06-29T12:00:00"))
    row = {r["external_campaign_label"]: r for r in review["rows"]}["offline import 2024"]
    assert row["status"] == "not_google_ads"
    assert row["native_spend"] is None and row["usd_spend"] is None  # never $0
    assert review["unmatched_count"] == 0  # an excluded label is resolved, not unmatched


# ════════════ admin-only endpoints ════════════


def _make_test_client():
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi[testclient] not available")
    try:
        from api.server import app  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"api.server import failed: {exc}")
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def client():
    return _make_test_client()


def _session_cookie(role):
    from api.auth import set_session
    from starlette.responses import Response as StarletteResponse
    r = StarletteResponse()
    try:
        set_session(r, "test" + role, role)
    except Exception:  # noqa: BLE001 — e.g. APP_SECRET_KEY not configured in env
        return None
    for part in r.headers.get("set-cookie", "").split(";"):
        part = part.strip()
        if part.startswith("ads_session="):
            return part.split("=", 1)[1]
    return None


_MAP_BODY = {"customer_id": "3059734490", "external_campaign_label": "mexico,chile",
             "campaign_id": "123", "canonical_campaign_name": "Mexico, Chile, Colombia",
             "historical_campaign_name": "mexico,chile"}
_EXCL_BODY = {"customer_id": "3059734490", "external_campaign_label": "offline import 2024",
              "reason": "imported historical campaign"}


def test_unauthenticated_cannot_save_mapping(client):
    assert client.post("/api/campaign-mapping", json=_MAP_BODY).status_code in (401, 403)
    assert client.post("/api/campaign-mapping/exclude", json=_EXCL_BODY).status_code in (401, 403)


def test_non_admin_cannot_save_mapping_or_exclude(client, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret-key-for-session-signing")
    cookie = _session_cookie("viewer")
    if not cookie:
        pytest.skip("Could not create viewer session cookie")
    res = client.post("/api/campaign-mapping", json=_MAP_BODY, cookies={"ads_session": cookie})
    assert res.status_code in (401, 403)
    assert res.status_code != 200
    res2 = client.post("/api/campaign-mapping/exclude", json=_EXCL_BODY, cookies={"ads_session": cookie})
    assert res2.status_code in (401, 403)
    assert res2.status_code != 200


def test_admin_token_can_save_mapping_and_exclude(client, monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "test-token-123")
    saved = {}
    monkeypatch.setattr("services.campaign_identity_service.record_manual_mapping",
                        lambda *a, **k: saved.setdefault("map", (a, k)) or True)
    monkeypatch.setattr("services.campaign_identity_service.record_exclusion",
                        lambda *a, **k: saved.setdefault("excl", (a, k)) or True)
    hdr = {"Authorization": "Bearer test-token-123"}
    res = client.post("/api/campaign-mapping", json=_MAP_BODY, headers=hdr)
    assert res.status_code == 200, res.text
    assert res.json()["match_method"] == "manual"
    res2 = client.post("/api/campaign-mapping/exclude", json=_EXCL_BODY, headers=hdr)
    assert res2.status_code == 200, res2.text
    assert res2.json()["match_method"] == "not_google_ads"
    assert "map" in saved and "excl" in saved


def test_exclude_endpoint_registered_and_admin_gated():
    assert '"/api/campaign-mapping/exclude"' in SERVER
    i = SERVER.find("def api_campaign_mapping_exclude")
    assert i != -1
    assert "check_admin_or_token(request)" in SERVER[i:i + 900]


# ════════════ UI workbench wiring ════════════


def test_workbench_controls_present_in_ui():
    assert 'id="campaign-mapping-panel"' in HTML
    assert "function renderCampaignMapping" in JS
    assert "/api/campaign-mapping-review?window=" in JS
    # Searchable dropdown + suggestion chips + per-label spend preview.
    assert "data-mapping-select" in JS
    assert "data-mapping-search" in JS
    assert "data-mapping-action" in JS
    assert "mappingSpendPreview" in JS
    # Approve + Mark-as-Not-Google-Ads actions and their POST endpoints.
    assert "Approve mapping" in JS
    assert "Mark as Not Google Ads" in JS
    assert '"/api/campaign-mapping"' in JS
    assert '"/api/campaign-mapping/exclude"' in JS
    assert "Excluded from Google Ads ROAS" in JS
    # Unmatched labels still never imply $0.
    assert "Spend mapping unavailable" in JS


def test_roas_campaign_renders_aliases():
    assert "function campaignAliasHtml" in JS
    assert "campaignAliasHtml(r.aliases)" in JS
    assert "Aliases:" in JS
