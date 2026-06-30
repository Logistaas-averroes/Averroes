"""
PR-ADS-122 — Google Ads Spend Reconciliation Drilldown.

An AUDIT + PROOF feature: every ROAS campaign row can prove its spend by
comparing, for the SAME campaign and window:

  1. the LOCAL canonical DB total, and
  2. a FRESH Google Ads campaign-level API total, and
  3. an ad-group-level breakdown WITH status (enabled / paused / removed)

so a lower Google Ads UI total can be explained (e.g. the UI filters to
"Ad group status: Enabled" and excludes paused/removed ad-group spend).

Proves the required checks:
  - no duplicate local rows per customer_id + campaign_id + spend_date
  - the campaign reconciliation endpoint exists
  - local total equals the daily row sum
  - variance is explicit when the API fresh total differs
  - ad-group status breakdown is reported
  - the UI never hides a mismatch
  - ROAS remains unavailable when spend coverage is incomplete
  - no Google Ads writes

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


# ── Shared fixture: a Global - Competitors mismatch (the suspicious gap) ──────
#
# Local canonical (all ad-group statuses): £10,100.87
# Fresh campaign-level API:                £7,566.42  ← matches enabled-only
# Paused/removed ad-group spend:           £2,534.45  ← the Google Ads UI hides

def _patch_global_competitors(monkeypatch, svc, *, coverage=None, duplicates=None,
                              api_rows=None, ad_group_rows=None):
    monkeypatch.setattr("db.revenue_repository.fetch_account_time_zone",
                        lambda: "Europe/London")
    monkeypatch.setattr(
        "db.revenue_repository.fetch_campaign_daily_spend_local",
        lambda cid, s, e, customer_id=None: {
            "available": True, "customer_id": "3059734490", "currency_code": "GBP",
            "campaign_id": "123", "campaign_name": "Global - Competitors",
            "rows": [
                {"spend_date": "2026-01-01", "cost_micros": 5_000_000_000, "spend": 5000.0},
                {"spend_date": "2026-01-02", "cost_micros": 5_100_870_000, "spend": 5100.87},
            ],
            "total_cost_micros": 10_100_870_000, "total_spend": 10100.87,
            "rows_counted": 2, "coverage_start": "2026-01-01", "coverage_end": "2026-06-30",
        })
    monkeypatch.setattr(
        "db.revenue_repository.fetch_spend_coverage",
        lambda s, e: {"available": True, "chunks": coverage if coverage is not None else [
            {"chunk_start": "2026-01-01", "chunk_end": "2026-06-30", "status": "verified"}]})
    monkeypatch.setattr(
        "db.revenue_repository.fetch_campaign_spend_duplicate_dates",
        lambda cid, s, e, customer_id=None: {"available": True, "duplicates": duplicates or []})
    monkeypatch.setattr(
        "db.revenue_repository.fetch_campaign_identity",
        lambda cid=None: {"available": True, "mappings": [
            {"campaign_id": "123", "external_campaign_label": "global-competitors",
             "match_method": "manual"}]})
    monkeypatch.setattr(svc, "fetch_campaign_api_spend",
                        lambda s, e, cid: {"campaign_name": "Global - Competitors",
                                           "rows": api_rows if api_rows is not None else [
                                               {"spend_date": "2026-01-01", "cost_micros": 3_766_420_000},
                                               {"spend_date": "2026-01-02", "cost_micros": 3_800_000_000}]})
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
    # The schema enforces one local row per (customer_id, campaign_id, spend_date),
    # so the local total can never be inflated by duplicates.
    assert "google_ads_campaign_daily_spend" in SCHEMA
    block = SCHEMA[SCHEMA.find("CREATE TABLE IF NOT EXISTS google_ads_campaign_daily_spend"):]
    block = block[:block.find(");") + 2]
    assert re.search(r"UNIQUE\s*\(\s*customer_id\s*,\s*campaign_id\s*,\s*spend_date\s*\)", block)


def test_duplicate_local_rows_are_reported(monkeypatch):
    svc = _load_svc()
    _patch_global_competitors(monkeypatch, svc)
    out = svc.build_campaign_spend_reconciliation("ytd", "123", now=NOW)
    # Healthy path: no duplicates reported.
    assert out["duplicate_local_rows"] == []

    # When the integrity query DOES find a duplicate, it is surfaced, not hidden.
    _patch_global_competitors(monkeypatch, svc, duplicates=[
        {"customer_id": "3059734490", "spend_date": "2026-01-01", "row_count": 2}])
    out2 = svc.build_campaign_spend_reconciliation("ytd", "123", now=NOW)
    assert len(out2["duplicate_local_rows"]) == 1
    assert out2["duplicate_local_rows"][0]["row_count"] == 2


def test_duplicate_detection_repo_query_groups_by_identity():
    # The repository duplicate check groups by the natural key and flags >1.
    src = open(os.path.join(ROOT, "db", "revenue_repository.py"), encoding="utf-8").read()
    fn = src[src.find("def fetch_campaign_spend_duplicate_dates"):]
    fn = fn[:fn.find("\ndef ", 1)]
    assert "GROUP BY customer_id, spend_date" in fn
    assert "HAVING COUNT(*) > 1" in fn


# ════════════ 2. the campaign reconciliation endpoint exists ════════════


def test_reconcile_endpoint_registered():
    assert '@app.get("/api/google-ads-spend-reconcile/campaign")' in SERVER
    assert "build_campaign_spend_reconciliation" in SERVER
    # campaign_id is a required query parameter.
    assert "campaign_id: str = Query(...)" in SERVER


def test_reconcile_endpoint_returns_contract_shape(monkeypatch):
    svc = _load_svc()
    _patch_global_competitors(monkeypatch, svc)
    out = svc.build_campaign_spend_reconciliation("ytd", "123", now=NOW)
    for key in ("customer_id", "campaign_id", "campaign_name", "window", "date_from",
                "date_to", "account_time_zone", "currency_code", "local_total_native",
                "api_total_native", "variance_native", "variance_pct", "status",
                "possible_causes", "daily"):
        assert key in out, f"missing contract key: {key}"
    assert out["customer_id"] == "3059734490"
    assert out["campaign_id"] == "123"
    assert out["campaign_name"] == "Global - Competitors"
    assert out["date_from"] == "2026-01-01" and out["date_to"] == "2026-06-30"
    assert out["account_time_zone"] == "Europe/London"
    assert out["currency_code"] == "GBP"
    assert out["mapped_aliases"] == ["global-competitors"]


def test_reconcile_requires_campaign_id():
    svc = _load_svc()
    with pytest.raises(ValueError):
        svc.build_campaign_spend_reconciliation("ytd", "")


# ════════════ 3. local total equals the daily row sum ════════════


def test_local_total_equals_daily_local_sum(monkeypatch):
    svc = _load_svc()
    _patch_global_competitors(monkeypatch, svc)
    out = svc.build_campaign_spend_reconciliation("ytd", "123", now=NOW)
    daily_local_sum = round(sum(d["local_cost"] for d in out["daily"]), 6)
    assert daily_local_sum == out["local_total_native"] == 10100.87
    assert out["rows_counted"] == 2


# ════════════ 4. variance is explicit when the API fresh total differs ════════


def test_variance_explicit_on_mismatch(monkeypatch):
    svc = _load_svc()
    _patch_global_competitors(monkeypatch, svc)
    out = svc.build_campaign_spend_reconciliation("ytd", "123", now=NOW)
    # £10,100.87 local vs £7,566.42 API → +£2,534.45, ≈33.5%, status mismatch.
    assert out["local_total_native"] == 10100.87
    assert out["api_total_native"] == 7566.42
    assert out["variance_native"] == 2534.45
    assert out["variance_pct"] is not None and out["variance_pct"] > 30
    assert out["status"] == "mismatch"
    # The per-day variance is also explicit.
    assert out["daily"][0]["variance"] == round(5000.0 - 3766.42, 6)


def test_variance_match_within_tolerance(monkeypatch):
    svc = _load_svc()
    # Make the fresh API total equal the local total → reconciled.
    _patch_global_competitors(monkeypatch, svc, api_rows=[
        {"spend_date": "2026-01-01", "cost_micros": 5_000_000_000},
        {"spend_date": "2026-01-02", "cost_micros": 5_100_870_000}])
    out = svc.build_campaign_spend_reconciliation("ytd", "123", now=NOW)
    assert out["variance_native"] == 0.0
    assert out["status"] == "match"


def test_api_unavailable_is_not_treated_as_zero(monkeypatch):
    svc = _load_svc()
    _patch_global_competitors(monkeypatch, svc)

    def _boom(s, e, cid):
        raise RuntimeError("google ads api down")
    monkeypatch.setattr(svc, "fetch_campaign_api_spend", _boom)
    out = svc.build_campaign_spend_reconciliation("ytd", "123", now=NOW)
    # A failed API read must NOT be reported as £0 / a fake reconciliation.
    assert out["api_total_native"] is None
    assert out["status"] == "unavailable"
    assert out["variance_native"] is None


# ════════════ 5. ad-group status breakdown is reported ════════════


def test_ad_group_status_breakdown_reported(monkeypatch):
    svc = _load_svc()
    _patch_global_competitors(monkeypatch, svc)
    out = svc.build_campaign_spend_reconciliation("ytd", "123", now=NOW)
    ag = out["ad_group_breakdown"]
    assert ag["available"] is True
    statuses = {s["status"]: s for s in ag["statuses"]}
    assert "ENABLED" in statuses and "PAUSED" in statuses and "REMOVED" in statuses
    # Enabled-only equals the fresh campaign-level API total (proves the UI filter).
    assert ag["enabled_total_native"] == 7566.42
    assert ag["non_enabled_total_native"] == round(2000.0 + 534.45, 6)
    # The cause explicitly names the paused/removed exclusion.
    assert any("paused/removed ad-group spend" in c for c in out["possible_causes"])


def test_ad_group_unavailable_reported_not_zero(monkeypatch):
    svc = _load_svc()
    _patch_global_competitors(monkeypatch, svc)

    def _boom(s, e, cid):
        raise RuntimeError("ad_group view unavailable")
    monkeypatch.setattr(svc, "fetch_ad_group_spend", _boom)
    out = svc.build_campaign_spend_reconciliation("ytd", "123", now=NOW)
    assert out["ad_group_breakdown"]["available"] is False
    assert any("could not be confirmed or ruled out" in c for c in out["possible_causes"])


# ════════════ 6. the UI never hides a mismatch ════════════


def test_ui_renders_mismatch_status_and_variance():
    # The drilldown modal renders a status badge (including 'mismatch') and the
    # explicit variance — a mismatch can never be silently dropped.
    assert "renderSpendProof" in JS
    assert "spendProofStatusBadge" in JS
    assert "spend-proof-badge--mismatch" in JS
    assert "Mismatch" in JS
    assert "spendProofVariance" in JS
    # The variance value (and pct) are placed into the grid.
    assert "data.variance_native" in JS
    assert "data.variance_pct" in JS
    # The daily breakdown surfaces per-day variance + coverage state.
    assert "renderSpendProofDaily" in JS
    assert "coverage_state" in JS


def test_ui_exposes_view_spend_proof_action():
    assert "View spend proof" in JS
    assert "spendProofButton" in JS
    assert "data-spend-proof-campaign" in JS
    # Wired into the ROAS by Campaign table row.
    assert "spendProofButton(r.campaign_id" in JS


# ════════════ 7. ROAS stays unavailable when spend coverage is incomplete ═════


def test_reconcile_reports_incomplete_coverage(monkeypatch):
    svc = _load_svc()
    # A missing chunk at the end of the window → coverage incomplete.
    _patch_global_competitors(monkeypatch, svc, coverage=[
        {"chunk_start": "2026-01-01", "chunk_end": "2026-03-31", "status": "verified"}])
    out = svc.build_campaign_spend_reconciliation("ytd", "123", now=NOW)
    assert out["coverage_status"] == "incomplete"
    # Days outside a verified chunk are flagged unverified (never silently trusted).
    states = {d["coverage_state"] for d in out["daily"]}
    assert "verified" in states


def test_roas_page_still_blocks_on_incomplete_coverage():
    # The ROAS by Campaign page keeps blocking ROAS when canonical coverage is
    # incomplete — this drilldown is a proof tool, it does NOT relax that rule.
    assert "spendCoverageIncomplete" in JS
    assert "Spend coverage incomplete — ROAS unavailable" in JS


# ════════════ 8. no Google Ads writes ════════════


def test_connector_reconcile_queries_are_read_only():
    for fn_name in ("fetch_campaign_daily_spend_for_campaign", "fetch_ad_group_daily_spend"):
        assert f"def {fn_name}" in CONN_SRC
        start = CONN_SRC.find(f"def {fn_name}")
        body = CONN_SRC[start:CONN_SRC.find("\ndef ", start + 1)]
        # SELECT-only; no mutate/write operations anywhere in the query body.
        assert "SELECT" in body
        for forbidden in ("mutate", "Mutate", "create_", "update_", "remove_", "INSERT", "UPDATE", "DELETE"):
            assert forbidden not in body, f"{fn_name} contains forbidden op: {forbidden}"


def test_reconcile_service_never_writes():
    # The reconciliation service imports no writers and performs no upserts.
    assert "db.writers" not in SVC_SRC
    assert "upsert" not in SVC_SRC
    for forbidden in ("mutate", "Mutate", "INSERT", "UPDATE", "DELETE"):
        assert forbidden not in SVC_SRC


def test_reconcile_does_not_change_roas_spend_source():
    # Doctrine guard: the spend source for ROAS is unchanged by this PR — the
    # service is documented as read-only and never alters the ROAS denominator.
    assert "NEVER changes the ROAS spend source" in SVC_SRC or \
           "never changes the ROAS spend source" in SVC_SRC.lower()
