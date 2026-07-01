"""
PR-ADS-129 — ROAS Availability + Geo Reconciliation Audit/Fix.

Audit-and-fix: first PROVE why ROAS is unavailable, then fix only the cases that
are safe to fix; where the data is genuinely incomplete or mismatched, keep ROAS
blocked but make the reason impossible to miss.

  - Campaign spend coverage is judged from the durable coverage LEDGER, so a window
    whose first spend row starts after the window start is COMPLETE when the earlier
    period was fetched and verified as zero spend — and INCOMPLETE (ROAS withheld)
    when it was never fetched or failed. The reason is classified and exposed.
  - Geo reconciliation gains a breakdown (reason, next_action, per-day / per-campaign
    variances, unmapped-location spend) so Revenue Health explains the exact gap.
  - Country ROAS stays blocked on a real mismatch; nothing loosens the tolerance.

No attribution/sync/schema changes; no Google Ads / HubSpot writes.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()


def _slice(marker, span=2200):
    i = JS.find(marker)
    assert i != -1, f"{marker} not found in app.js"
    return JS[i:i + span]


def _wide_verified_chunk():
    return {"available": True, "chunks": [
        {"chunk_start": "2000-01-01", "chunk_end": "2100-01-01",
         "status": "verified", "rows_written": 10, "cost_micros_total": 5_000_000},
    ]}


# ── classify_coverage (the audit core) ──────────────────────────────────────

from datetime import date  # noqa: E402
from services.google_ads_spend_service import classify_coverage  # noqa: E402


# Test 1 — first spend after window start + verified-zero earlier → COMPLETE
def test_verified_zero_before_first_spend_is_complete():
    chunks = _wide_verified_chunk()["chunks"]
    out = classify_coverage(date(2026, 1, 1), date(2026, 7, 1), chunks,
                            first_spend_date="2026-04-04")
    assert out["complete"] is True
    assert out["status"] == "complete"
    assert out["reason"] == "verified_zero_before_first_spend"


# Test 2 — missing pre-April chunks block ROAS
def test_missing_pre_april_chunks_incomplete():
    chunks = [{"chunk_start": "2026-04-01", "chunk_end": "2100-01-01",
               "status": "verified", "rows_written": 5, "cost_micros_total": 1_000}]
    out = classify_coverage(date(2026, 1, 1), date(2026, 7, 1), chunks,
                            first_spend_date="2026-04-04")
    assert out["complete"] is False
    assert out["status"] == "incomplete"
    assert out["reason"] == "missing_chunks"
    assert out["missing_chunks"]  # names the unverified period


def test_no_chunks_is_not_backfilled():
    out = classify_coverage(date(2026, 1, 1), date(2026, 7, 1), [],
                            first_spend_date=None)
    assert out["status"] == "incomplete"
    assert out["reason"] == "not_backfilled"


# Test 3 — failed chunks block ROAS
def test_failed_chunks_incomplete():
    chunks = [
        {"chunk_start": "2000-01-01", "chunk_end": "2100-01-01",
         "status": "verified", "rows_written": 5, "cost_micros_total": 1_000},
        {"chunk_start": "2026-05-01", "chunk_end": "2026-05-31",
         "status": "failed", "rows_written": 0, "cost_micros_total": 0},
    ]
    out = classify_coverage(date(2026, 1, 1), date(2026, 7, 1), chunks,
                            first_spend_date="2026-01-01")
    assert out["complete"] is False
    assert out["status"] == "incomplete"
    assert out["reason"] == "failed_chunks"
    assert out["failed_chunks"]


# ── Mart-level coverage gating (Tests 1/2/4) ────────────────────────────────

def _mart(view, monkeypatch, **overrides):
    import test_pr_ads_125_revenue_decision_mart as t125
    t125._patch_durable(monkeypatch, **overrides)
    from services.revenue_decision_mart import build_revenue_decision_mart
    return build_revenue_decision_mart(view=view, window=t125.WINDOW, now=t125.NOW), t125


def test_mart_exposes_verified_zero_coverage_complete(monkeypatch):
    import test_pr_ads_125_revenue_decision_mart as t125
    canon = dict(t125.CANONICAL)
    canon["coverage_start"] = "2026-04-04"  # first spend after YTD start
    mart, _ = _mart("campaign", monkeypatch, canonical=canon)
    st = mart["spend_truth"]
    assert st["campaign_spend_coverage_status"] == "complete"
    assert st["campaign_spend_coverage_reason"] == "verified_zero_before_first_spend"
    # Coverage complete + FX complete → ROAS available.
    assert st["campaign_spend_status"] == "verified"
    assert mart["summary"]["roas"] is not None


def test_mart_blocks_roas_on_missing_chunks(monkeypatch):
    import test_pr_ads_125_revenue_decision_mart as t125
    canon = dict(t125.CANONICAL)
    canon["coverage_start"] = "2026-04-04"
    coverage = [
        {"chunk_start": "2026-04-01", "chunk_end": "2100-01-01",
         "status": "verified", "rows_written": 5, "cost_micros_total": 1_000}]
    mart, _ = _mart("campaign", monkeypatch, canonical=canon, coverage=coverage)
    st = mart["spend_truth"]
    assert st["campaign_spend_coverage_status"] == "incomplete"
    assert st["campaign_spend_status"] != "verified"
    assert mart["summary"]["roas"] is None
    codes = {d["code"] for d in mart["diagnostics"]}
    assert "campaign_spend_coverage" in codes
    # The diagnostic names the missing/unverified period.
    msg = next(d["message"] for d in mart["diagnostics"] if d["code"] == "campaign_spend_coverage")
    assert "unverified" in msg.lower() or "missing" in msg.lower()


# Test 4 — FX complete but campaign spend incomplete still blocks ROAS (spend cause)
def test_fx_complete_but_coverage_incomplete_blocks_for_coverage(monkeypatch):
    import test_pr_ads_125_revenue_decision_mart as t125
    canon = dict(t125.CANONICAL)  # fx_complete True
    canon["coverage_start"] = "2026-04-04"
    coverage = [
        {"chunk_start": "2026-04-01", "chunk_end": "2100-01-01",
         "status": "verified", "rows_written": 5, "cost_micros_total": 1_000}]
    mart, _ = _mart("campaign", monkeypatch, canonical=canon, coverage=coverage)
    st = mart["spend_truth"]
    assert st["fx_status"] == "verified"        # FX is fine…
    assert st["campaign_spend_status"] != "verified"  # …ROAS blocked on coverage
    assert mart["summary"]["roas"] is None
    codes = {d["code"] for d in mart["diagnostics"]}
    assert "campaign_spend_coverage" in codes


# ── Geo reconciliation (Tests 5/6/7) ────────────────────────────────────────

def _geo_recon(monkeypatch, *, geo_total, canonical_total=69720.093979, chunks=None):
    import db.revenue_repository as repo
    from datetime import datetime, timezone
    monkeypatch.setattr(repo, "fetch_account_time_zone", lambda: "Europe/London")
    monkeypatch.setattr(repo, "fetch_canonical_campaign_spend", lambda s, e: {
        "available": True, "total_spend": canonical_total, "currency_code": "GBP",
        "customer_id": "111", "coverage_start": "2026-01-01", "coverage_end": "2026-06-30"})
    monkeypatch.setattr(repo, "fetch_geo_daily_spend_total", lambda s, e: {
        "available": True, "has_rows": True, "total_spend": geo_total,
        "rows_counted": 13516, "country_count": 191,
        "last_synced_at": "2026-07-01 05:03:37+00:00"})
    monkeypatch.setattr(repo, "fetch_spend_coverage", lambda s, e: chunks or _wide_verified_chunk())
    monkeypatch.setattr(repo, "fetch_fx_coverage", lambda s, e, c: {"complete": True})
    monkeypatch.setattr(repo, "fetch_geo_reconciliation_breakdown", lambda s, e: {
        "available": True, "daily": [], "by_campaign": [],
        "unmapped_geo_native": 0.0, "campaign_rows_counted": 3623})
    from services.google_ads_geo_sync_service import build_geo_reconciliation
    return build_geo_reconciliation("ytd")


# Test 5 — geo rows exist but mismatch keeps country blocked
def test_geo_mismatch_keeps_country_blocked(monkeypatch):
    out = _geo_recon(monkeypatch, geo_total=67271.062131)  # 3.51% below canonical
    assert out["status"] == "mismatch"
    assert out["reconciled"] is False
    assert out["country_roas_unblockable"] is False
    assert round(out["variance_pct"], 2) == 3.51
    assert out["tolerance"] == 0.02


# Test 6 — geo within tolerance unblocks country ROAS
def test_geo_within_tolerance_unblocks_country(monkeypatch):
    out = _geo_recon(monkeypatch, geo_total=69720.093979)  # exact match
    assert out["status"] == "reconciled"
    assert out["reconciled"] is True
    assert out["country_roas_unblockable"] is True


def test_geo_within_tolerance_unblocks_country_backend_mart(monkeypatch):
    # The mart country view is verified + decision-ready when geo reconciles.
    import test_pr_ads_125_revenue_decision_mart as t125
    mart, _ = _mart("country", monkeypatch)  # default fixture: geo reconciles
    assert mart["spend_truth"]["country_spend_status"] == "verified"
    assert mart["readiness"]["country_decision_ready"] is True


# Test 7 — geo diagnostics expose totals, variance, tolerance, next_action
def test_geo_diagnostics_expose_totals_and_next_action(monkeypatch):
    out = _geo_recon(monkeypatch, geo_total=67271.062131)
    for key in ("canonical_campaign_total", "geo_total", "variance", "variance_pct",
                "tolerance", "coverage_status", "fx_coverage_status", "last_geo_sync_at",
                "reason", "next_action", "missing_geo_dates", "campaign_geo_gaps"):
        assert key in out, f"geo reconciliation missing {key}"
    assert out["reason"]  # a concrete cause
    assert out["next_action"]  # a concrete action


# ── Frontend copy (Tests 8/9) ───────────────────────────────────────────────

# Test 8 — campaign diagnostic copy
def test_frontend_campaign_coverage_copy():
    assert "function renderMartCampaignCoverage" in JS
    fn = _slice("function renderMartCampaignCoverage", span=2200)
    assert "Campaign spend coverage incomplete" in fn
    assert "Missing / unverified period" in fn
    assert "verified as zero spend" in fn
    assert "spend_coverage_detail" in fn
    # Wired into the campaign page render.
    page = _slice("function renderRoasCampaignsPage", span=1800)
    assert "renderMartCampaignCoverage(data)" in page


# Test 9 — country blocked copy with totals
def test_frontend_country_blocked_copy():
    fn = _slice("function renderCountrySpendUnreconciledState", span=2800)
    assert "Country ROAS is blocked" in fn
    assert "Campaign spend" in fn
    assert "Geo spend" in fn
    assert "Variance" in fn
    assert "Tolerance" in fn
    # Totals come from the mart's spend_truth (never recomputed).
    assert "campaign_spend_total" in fn and "country_geo_total" in fn


def test_frontend_geo_health_diagnostics():
    assert "function renderGeoReconcileDetail" in JS
    fn = _slice("function renderGeoReconcileDetail", span=1200)
    assert "Likely cause" in fn
    assert "Next action" in fn
    assert "Tolerance" in fn
    panel = _slice("function renderGeoSyncPanel", span=3600)
    assert "renderGeoReconcileDetail(r, cur)" in panel


# ── Test 10 — read-only safety ──────────────────────────────────────────────

def test_no_unsafe_writes_introduced():
    files = [
        "static/app.js",
        "services/google_ads_spend_service.py",
        "services/google_ads_geo_sync_service.py",
        "services/revenue_decision_mart.py",
        "services/revenue_attribution_service.py",
        "db/revenue_repository.py",
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


def test_geo_reconciliation_breakdown_is_read_only():
    # The new repo breakdown query is SELECT-only.
    src = open(os.path.join(ROOT, "db", "revenue_repository.py"), encoding="utf-8").read()
    i = src.find("def fetch_geo_reconciliation_breakdown")
    j = src.find("\ndef ", i + 1)
    body = src[i:j]
    lowered = body.lower()
    for verb in ("insert", "update ", "delete", "upsert", "drop", "alter"):
        assert verb not in lowered, f"breakdown query must be read-only (found '{verb}')"
