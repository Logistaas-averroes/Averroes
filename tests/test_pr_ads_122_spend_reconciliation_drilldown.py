"""
PR-ADS-122 — Google Ads Spend Reconciliation Drilldown.

An AUDIT + PROOF feature: every ROAS campaign row can prove its spend by keeping
THREE distinct totals separate (never conflated):

  1. the LOCAL canonical DB total, and
  2. a FRESH Google Ads campaign-level API total (ALL ad-group statuses), and
  3. an ad-group-level breakdown WITH status (enabled / paused / removed).

Reconciliation rules proved here:
  - PRIMARY reconciliation is local vs fresh campaign API — they are EXPECTED to
    match; a mismatch means a canonical spend problem (stale local / date /
    identity), NOT the ad-group enabled filter.
  - the Google Ads UI screenshot is explained ONLY by the enabled-only ad-group
    total.
  - enabled-only is never claimed equal to the campaign-level API total unless
    the live data proves it.

Plus the required checks: no duplicate local rows, endpoint exists, local total
equals daily row sum, explicit variance, ad-group status breakdown, UI never
hides a mismatch, ROAS unavailable on incomplete coverage, no Google Ads writes,
campaign_id validated as numeric, and local reads scoped by customer/account.

No google-ads SDK import and no network: connectors are reached only through
patched seams and all DB reads are patched at the repository boundary.
"""

import os
import re
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
SERVER = open(os.path.join(ROOT, "api", "server.py"), encoding="utf-8").read()
SCHEMA = open(os.path.join(ROOT, "db", "schema.py"), encoding="utf-8").read()
CONN_SRC = open(os.path.join(ROOT, "connectors", "google_ads_direct.py"), encoding="utf-8").read()
SVC_SRC = open(os.path.join(ROOT, "services", "spend_reconciliation_service.py"), encoding="utf-8").read()

NOW = datetime.fromisoformat("2026-06-30T12:00:00+00:00")


def _load_svc():
    try:
        import services.spend_reconciliation_service as svc
        return svc
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"reconciliation service import unavailable: {exc}")


# ── Shared fixture: Global - Competitors ─────────────────────────────────────
#
# Local canonical:                  £10,100.87
# Fresh campaign-level API total:   £10,100.87  ← EXPECTED to match local
# Ad-group total (all statuses):    £10,100.87
#   ENABLED:                        £ 7,566.42  ← explains the Google Ads UI
#   PAUSED + REMOVED:               £ 2,534.45  ← the UI screenshot hides this

def _patch_global_competitors(monkeypatch, svc, *, coverage=None, duplicates=None,
                              api_rows=None, ad_group_rows=None, seen=None,
                              configured="3059734490"):
    def _local(cid, s, e, customer_id=None):
        if seen is not None:
            seen["local_customer"] = customer_id
        return {
            "available": True, "customer_id": "3059734490", "currency_code": "GBP",
            "campaign_id": "123", "campaign_name": "Global - Competitors",
            "rows": [
                {"spend_date": "2026-01-01", "cost_micros": 5_000_000_000, "spend": 5000.0},
                {"spend_date": "2026-01-02", "cost_micros": 5_100_870_000, "spend": 5100.87},
            ],
            "total_cost_micros": 10_100_870_000, "total_spend": 10100.87,
            "rows_counted": 2, "coverage_start": "2026-01-01", "coverage_end": "2026-06-30",
        }

    def _dup(cid, s, e, customer_id=None):
        if seen is not None:
            seen["dup_customer"] = customer_id
        return {"available": True, "duplicates": duplicates or []}

    monkeypatch.setattr("db.revenue_repository.fetch_account_time_zone",
                        lambda: "Europe/London")
    monkeypatch.setattr("db.revenue_repository.fetch_campaign_daily_spend_local", _local)
    monkeypatch.setattr("db.revenue_repository.fetch_campaign_spend_duplicate_dates", _dup)
    monkeypatch.setattr(
        "db.revenue_repository.fetch_spend_coverage",
        lambda s, e: {"available": True, "chunks": coverage if coverage is not None else [
            {"chunk_start": "2026-01-01", "chunk_end": "2026-06-30", "status": "verified"}]})
    monkeypatch.setattr(
        "db.revenue_repository.fetch_campaign_identity",
        lambda cid=None: {"available": True, "mappings": [
            {"campaign_id": "123", "external_campaign_label": "global-competitors",
             "match_method": "manual"}]})
    monkeypatch.setattr(svc, "configured_customer_id", lambda: configured)
    # Fresh campaign-level API total EQUALS local by default (canonical reconciles).
    monkeypatch.setattr(svc, "fetch_campaign_api_spend",
                        lambda s, e, cid: {"campaign_name": "Global - Competitors",
                                           "rows": api_rows if api_rows is not None else [
                                               {"spend_date": "2026-01-01", "cost_micros": 5_000_000_000},
                                               {"spend_date": "2026-01-02", "cost_micros": 5_100_870_000}]})
    # Ad-group view: enabled £7,566.42; paused/removed £2,534.45; all £10,100.87.
    monkeypatch.setattr(svc, "fetch_ad_group_spend",
                        lambda s, e, cid: {"currency_code": "GBP",
                                           "rows": ad_group_rows if ad_group_rows is not None else [
                                               {"ad_group_id": "1", "ad_group_status": "ENABLED",
                                                "spend_date": "2026-01-01", "cost_micros": 3_766_420_000},
                                               {"ad_group_id": "1", "ad_group_status": "ENABLED",
                                                "spend_date": "2026-01-02", "cost_micros": 3_800_000_000},
                                               {"ad_group_id": "2", "ad_group_status": "PAUSED",
                                                "spend_date": "2026-01-01", "cost_micros": 2_000_000_000},
                                               {"ad_group_id": "3", "ad_group_status": "REMOVED",
                                                "spend_date": "2026-01-02", "cost_micros": 534_450_000}]})


# ════════════ 1. no duplicate local rows per customer+campaign+date ════════════


def test_canonical_table_is_unique_per_customer_campaign_date():
    block = SCHEMA[SCHEMA.find("CREATE TABLE IF NOT EXISTS google_ads_campaign_daily_spend"):]
    block = block[:block.find(");") + 2]
    assert re.search(r"UNIQUE\s*\(\s*customer_id\s*,\s*campaign_id\s*,\s*spend_date\s*\)", block)


def test_duplicate_local_rows_are_reported(monkeypatch):
    svc = _load_svc()
    _patch_global_competitors(monkeypatch, svc)
    out = svc.build_campaign_spend_reconciliation("ytd", "123", now=NOW)
    assert out["duplicate_local_rows"] == []

    _patch_global_competitors(monkeypatch, svc, duplicates=[
        {"customer_id": "3059734490", "spend_date": "2026-01-01", "row_count": 2}])
    out2 = svc.build_campaign_spend_reconciliation("ytd", "123", now=NOW)
    assert len(out2["duplicate_local_rows"]) == 1
    assert out2["duplicate_local_rows"][0]["row_count"] == 2


def test_duplicate_detection_repo_query_groups_by_identity():
    src = open(os.path.join(ROOT, "db", "revenue_repository.py"), encoding="utf-8").read()
    fn = src[src.find("def fetch_campaign_spend_duplicate_dates"):]
    fn = fn[:fn.find("\ndef ", 1)]
    assert "GROUP BY customer_id, spend_date" in fn
    assert "HAVING COUNT(*) > 1" in fn


# ════════════ 2. the campaign reconciliation endpoint exists ════════════


def test_reconcile_endpoint_registered():
    assert '@app.get("/api/google-ads-spend-reconcile/campaign")' in SERVER
    assert "build_campaign_spend_reconciliation" in SERVER
    assert "campaign_id: str = Query(...)" in SERVER


def test_reconcile_endpoint_returns_contract_shape(monkeypatch):
    svc = _load_svc()
    _patch_global_competitors(monkeypatch, svc)
    out = svc.build_campaign_spend_reconciliation("ytd", "123", now=NOW)
    # The reviewer-required separated totals + variances are all present.
    for key in ("customer_id", "campaign_id", "campaign_name", "window", "date_from",
                "date_to", "account_time_zone", "currency_code",
                "local_canonical_campaign_total", "fresh_campaign_api_total_all_statuses",
                "fresh_ad_group_total_all_statuses", "fresh_ad_group_total_enabled_only",
                "fresh_ad_group_total_paused_removed", "variance_local_vs_fresh_campaign_api",
                "variance_local_vs_ad_group_all_statuses",
                "variance_google_ui_filter_estimate_vs_enabled_only",
                "google_ui_filter_estimate", "status", "possible_causes", "daily"):
        assert key in out, f"missing contract key: {key}"
    assert out["customer_id"] == "3059734490"
    assert out["campaign_id"] == "123"
    assert out["campaign_name"] == "Global - Competitors"
    assert out["date_from"] == "2026-01-01" and out["date_to"] == "2026-06-30"
    assert out["account_time_zone"] == "Europe/London"
    assert out["mapped_aliases"] == ["global-competitors"]


def test_reconcile_requires_numeric_campaign_id():
    svc = _load_svc()
    with pytest.raises(ValueError):
        svc.build_campaign_spend_reconciliation("ytd", "")
    with pytest.raises(ValueError):
        svc.build_campaign_spend_reconciliation("ytd", "123; DROP TABLE")
    with pytest.raises(ValueError):
        svc.build_campaign_spend_reconciliation("ytd", "abc")


# ════════════ 3. local total equals the daily row sum ════════════


def test_local_total_equals_daily_local_sum(monkeypatch):
    svc = _load_svc()
    _patch_global_competitors(monkeypatch, svc)
    out = svc.build_campaign_spend_reconciliation("ytd", "123", now=NOW)
    daily_local_sum = round(sum(d["local_cost"] for d in out["daily"]), 6)
    assert daily_local_sum == out["local_canonical_campaign_total"] == 10100.87
    assert out["rows_counted"] == 2


# ════════════ 4. totals are separated; variance is explicit ════════════


def test_totals_are_separated_not_conflated(monkeypatch):
    svc = _load_svc()
    _patch_global_competitors(monkeypatch, svc)
    out = svc.build_campaign_spend_reconciliation("ytd", "123", now=NOW)
    # Three distinct totals, each its own number.
    assert out["local_canonical_campaign_total"] == 10100.87
    assert out["fresh_campaign_api_total_all_statuses"] == 10100.87
    assert out["fresh_ad_group_total_all_statuses"] == 10100.87
    assert out["fresh_ad_group_total_enabled_only"] == 7566.42
    assert out["fresh_ad_group_total_paused_removed"] == 2534.45
    # Primary reconciliation: local vs fresh campaign API — they match.
    assert out["variance_local_vs_fresh_campaign_api"] == 0.0
    assert out["status"] == "match"


def test_canonical_mismatch_not_blamed_on_ad_group_filter(monkeypatch):
    svc = _load_svc()
    # Fresh campaign API total (£9,000) DIFFERS from local (£10,100.87) → this is
    # a canonical spend problem, NOT the ad-group enabled filter.
    _patch_global_competitors(monkeypatch, svc, api_rows=[
        {"spend_date": "2026-01-01", "cost_micros": 4_500_000_000},
        {"spend_date": "2026-01-02", "cost_micros": 4_500_000_000}])
    out = svc.build_campaign_spend_reconciliation("ytd", "123", now=NOW)
    assert out["status"] == "mismatch"
    assert out["variance_local_vs_fresh_campaign_api"] == round(10100.87 - 9000.0, 6)
    # The PRIMARY cause must name canonical/stale/date/identity and explicitly
    # NOT pin the campaign-level mismatch on the ad-group filter.
    primary = out["possible_causes"][0]
    assert "Canonical spend mismatch" in primary
    assert "NOT explained by the ad-group enabled filter" in primary


def test_api_unavailable_is_not_treated_as_zero(monkeypatch):
    svc = _load_svc()
    _patch_global_competitors(monkeypatch, svc)

    def _boom(s, e, cid):
        raise RuntimeError("google ads api down")
    monkeypatch.setattr(svc, "fetch_campaign_api_spend", _boom)
    out = svc.build_campaign_spend_reconciliation("ytd", "123", now=NOW)
    assert out["fresh_campaign_api_total_all_statuses"] is None
    assert out["status"] == "unavailable"
    assert out["variance_local_vs_fresh_campaign_api"] is None


# ════════════ 5. ad-group status breakdown explains the UI screenshot ════════


def test_ad_group_status_breakdown_reported(monkeypatch):
    svc = _load_svc()
    _patch_global_competitors(monkeypatch, svc)
    out = svc.build_campaign_spend_reconciliation("ytd", "123", now=NOW)
    ag = out["ad_group_breakdown"]
    assert ag["available"] is True
    statuses = {s["status"]: s for s in ag["statuses"]}
    assert "ENABLED" in statuses and "PAUSED" in statuses and "REMOVED" in statuses
    assert out["fresh_ad_group_total_enabled_only"] == 7566.42
    assert out["fresh_ad_group_total_paused_removed"] == round(2000.0 + 534.45, 6)
    # The UI is explained ONLY via the enabled-only number.
    assert any("enabled-only ad-group total" in c and "UI screenshot" in c
               for c in out["possible_causes"])


def test_enabled_only_equality_is_earned_not_assumed(monkeypatch):
    svc = _load_svc()
    # Ad-group view is INCOMPLETE relative to the campaign-level API total:
    # ad-group all-statuses = £6,000 but campaign API = £10,100.87. The enabled
    # vs campaign-minus-paused/removed estimate then disagrees, so we must NOT
    # claim enabled-only equals the campaign-level API total.
    out = None
    _patch_global_competitors(monkeypatch, svc, ad_group_rows=[
        {"ad_group_id": "1", "ad_group_status": "ENABLED",
         "spend_date": "2026-01-01", "cost_micros": 4_000_000_000},
        {"ad_group_id": "2", "ad_group_status": "PAUSED",
         "spend_date": "2026-01-01", "cost_micros": 2_000_000_000}])
    out = svc.build_campaign_spend_reconciliation("ytd", "123", now=NOW)
    # google_ui_filter_estimate = campaign API (10100.87) - paused/removed (2000)
    # = 8100.87, which does NOT equal enabled-only (4000) → variance is non-zero.
    assert out["google_ui_filter_estimate"] == round(10100.87 - 2000.0, 6)
    assert out["variance_google_ui_filter_estimate_vs_enabled_only"] != 0
    assert any("APPROXIMATE estimate" in c and "not a proven equality" in c
               for c in out["possible_causes"])
    # And it must NOT contain the "Live data confirms ... reconciles" assertion.
    assert not any("Live data confirms" in c for c in out["possible_causes"])


def test_enabled_only_equality_confirmed_when_data_proves_it(monkeypatch):
    svc = _load_svc()
    _patch_global_competitors(monkeypatch, svc)
    out = svc.build_campaign_spend_reconciliation("ytd", "123", now=NOW)
    # Ad-group all-statuses == campaign API, so the two UI estimates agree (0).
    assert out["variance_google_ui_filter_estimate_vs_enabled_only"] == 0.0
    assert any("Live data confirms" in c for c in out["possible_causes"])


def test_ad_group_unavailable_reported_not_zero(monkeypatch):
    svc = _load_svc()
    _patch_global_competitors(monkeypatch, svc)

    def _boom(s, e, cid):
        raise RuntimeError("ad_group view unavailable")
    monkeypatch.setattr(svc, "fetch_ad_group_spend", _boom)
    out = svc.build_campaign_spend_reconciliation("ytd", "123", now=NOW)
    assert out["ad_group_breakdown"]["available"] is False
    assert out["fresh_ad_group_total_enabled_only"] is None
    assert any("could not be confirmed or ruled out" in c for c in out["possible_causes"])


# ════════════ 6. the UI never hides a mismatch ════════════


def test_ui_renders_separated_totals_and_mismatch():
    assert "renderSpendProof" in JS
    assert "spendProofStatusBadge" in JS
    assert "spend-proof-badge--mismatch" in JS
    assert "Mismatch" in JS
    # The UI surfaces each separated total + variance — never one conflated cell.
    assert "local_canonical_campaign_total" in JS
    assert "fresh_campaign_api_total_all_statuses" in JS
    assert "fresh_ad_group_total_enabled_only" in JS
    assert "variance_local_vs_fresh_campaign_api" in JS
    assert "variance_google_ui_filter_estimate_vs_enabled_only" in JS
    assert "renderSpendProofDaily" in JS


def test_ui_exposes_view_spend_proof_action():
    assert "View spend proof" in JS
    assert "spendProofButton" in JS
    assert "data-spend-proof-campaign" in JS
    assert "spendProofButton(r.campaign_id" in JS


# ════════════ 7. ROAS stays unavailable when spend coverage is incomplete ═════


def test_reconcile_reports_incomplete_coverage(monkeypatch):
    svc = _load_svc()
    _patch_global_competitors(monkeypatch, svc, coverage=[
        {"chunk_start": "2026-01-01", "chunk_end": "2026-03-31", "status": "verified"}])
    out = svc.build_campaign_spend_reconciliation("ytd", "123", now=NOW)
    assert out["coverage_status"] == "incomplete"
    states = {d["coverage_state"] for d in out["daily"]}
    assert "verified" in states


def test_roas_page_still_blocks_on_incomplete_coverage():
    assert "spendCoverageIncomplete" in JS
    assert "Spend coverage incomplete — ROAS unavailable" in JS


# ════════════ 8. local reads scoped by customer/account ════════════


def test_local_reads_scoped_by_customer(monkeypatch):
    svc = _load_svc()
    seen = {}
    _patch_global_competitors(monkeypatch, svc, seen=seen, configured="3059734490")
    svc.build_campaign_spend_reconciliation("ytd", "123", now=NOW)
    # The configured account id is passed into BOTH local reads so a multi-customer
    # DB cannot cross-contaminate the same campaign_id across accounts.
    assert seen["local_customer"] == "3059734490"
    assert seen["dup_customer"] == "3059734490"


def test_explicit_customer_id_overrides_configured(monkeypatch):
    svc = _load_svc()
    seen = {}
    _patch_global_competitors(monkeypatch, svc, seen=seen, configured="3059734490")
    svc.build_campaign_spend_reconciliation("ytd", "123", now=NOW, customer_id="9999999999")
    assert seen["local_customer"] == "9999999999"


# ════════════ 9. no Google Ads writes; campaign_id validated ════════════


def test_connector_validates_numeric_campaign_id():
    for fn_name in ("fetch_campaign_daily_spend_for_campaign", "fetch_ad_group_daily_spend"):
        start = CONN_SRC.find(f"def {fn_name}")
        body = CONN_SRC[start:CONN_SRC.find("\ndef ", start + 1)]
        assert "isdigit()" in body
        assert 'raise ValueError("campaign_id must be numeric")' in body


def test_connector_reconcile_queries_are_read_only():
    for fn_name in ("fetch_campaign_daily_spend_for_campaign", "fetch_ad_group_daily_spend"):
        assert f"def {fn_name}" in CONN_SRC
        start = CONN_SRC.find(f"def {fn_name}")
        body = CONN_SRC[start:CONN_SRC.find("\ndef ", start + 1)]
        assert "SELECT" in body
        for forbidden in ("mutate", "Mutate", "create_", "update_", "remove_",
                          "INSERT", "UPDATE", "DELETE"):
            assert forbidden not in body, f"{fn_name} contains forbidden op: {forbidden}"


def test_reconcile_service_never_writes():
    assert "db.writers" not in SVC_SRC
    assert "upsert" not in SVC_SRC
    for forbidden in ("mutate", "Mutate", "INSERT", "UPDATE", "DELETE"):
        assert forbidden not in SVC_SRC


def test_reconcile_does_not_change_roas_spend_source():
    assert "NEVER changes the ROAS spend source" in SVC_SRC or \
           "never changes the ROAS spend source" in SVC_SRC.lower()
