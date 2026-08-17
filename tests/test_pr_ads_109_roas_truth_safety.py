"""
PR-ADS-109 — ROAS truth-safety tests (ROAS null vs 0, exclusions, frontend).
"""

import inspect
import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.canonical_ledger_fixtures import (  # noqa: E402
    canonical_ledger_patch, from_legacy_deal_rows,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import db.revenue_repository as repo
import services.revenue_attribution_service as svc
from services.revenue_attribution_service import build_revenue_attribution

JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()

NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


def _leads(rows=None, safe=True):
    return {"available": True, "table": "leads",
            "rows": rows if rows is not None else [],
            "event_date_safe": safe, "lead_event_date_field_available": True,
            "missing_contact_created_at_count": 0,
            "excluded_non_paid_count": 0, "excluded_pseudo_campaign_count": 0}


def _canonical(spend_rows):
    """Canonical campaign spend mirroring geo (PR-ADS-118 spend-truth contract).

    Campaign ROAS comes from canonical spend with COMPLETE coverage — never geo.
    Mirroring geo into canonical (with a wide verified chunk) keeps these
    truth-safety tests exercising real ROAS math under the new contract.
    """
    by_camp = {}
    for r in spend_rows:
        by_camp[r.get("campaign_name")] = by_camp.get(r.get("campaign_name"), 0.0) + float(r.get("spend") or 0)
    rows = [{"campaign_id": str(i + 1), "campaign_name": n,
             "cost_micros": int(round(a * 1_000_000)), "spend": a}
            for i, (n, a) in enumerate(by_camp.items())]
    tot = sum(r["cost_micros"] for r in rows)
    return {"available": True, "rows": rows, "total_cost_micros": tot,
            "total_spend": tot / 1_000_000, "campaign_count": len(rows),
            "customer_id": "C1", "currency_code": "USD",
            "coverage_start": "2000-01-01", "coverage_end": "2100-01-01"}


def _build(spend_rows, revenue_rows, lead_rows=None, safe=True):
    spend = {"available": True, "rows": spend_rows, "coverage_start": None, "coverage_end": None, "table": "geo"}
    rev = {"available": True, "rows": revenue_rows, "coverage_start": None, "coverage_end": None, "table": "gclid_attribution"}
    coverage = {"available": True,
                "chunks": [{"chunk_start": "2000-01-01", "chunk_end": "2100-01-01", "status": "verified"}]}
    with patch("db.revenue_repository.fetch_campaign_country_spend", return_value=spend), \
         patch("db.revenue_repository.fetch_lead_quality", return_value=_leads(lead_rows, safe)), \
         canonical_ledger_patch(from_legacy_deal_rows(rev.get("rows") or [])), \
         patch("db.revenue_repository.fetch_canonical_campaign_spend", return_value=_canonical(spend_rows)), \
         patch("db.revenue_repository.fetch_spend_coverage", return_value=coverage), \
         patch("db.revenue_repository.fetch_sync_state", return_value={"available": True, "datasets": {}}):
        return build_revenue_attribution("current_quarter", now=NOW)


def _by(rows, field, value):
    for r in rows:
        if r.get(field) == value:
            return r
    return None


# ── ROAS null vs 0 ───────────────────────────────────────────────────────────


def test_roas_null_when_revenue_not_wired():
    r = _build(
        spend_rows=[{"campaign_name": "gulf", "country": "SA", "spend": 1000.0}],
        revenue_rows=[],  # not wired
    )
    # PR-ADS-153E-B: a READABLE ledger with no campaign-attributable deals is a
    # different fact from "revenue not wired", and the status now says which.
    assert r["revenue_attribution_status"] == "no_campaign_attributable_deals_in_window"
    gulf = _by(r["campaigns"], "campaign_name", "gulf")
    assert gulf["roas"] is None  # null, not 0
    assert r["summary"]["roas"] is None


def test_roas_zero_when_revenue_available_but_campaign_has_none():
    r = _build(
        spend_rows=[
            {"campaign_name": "gulf", "country": "SA", "spend": 1000.0},
            {"campaign_name": "waste", "country": "EG", "spend": 500.0},
        ],
        revenue_rows=[
            {"campaign_name": "gulf", "country": "SA", "deal_id": "d1", "deal_amount_usd": 5000.0, "match_status": "matched"},
        ],
    )
    assert r["revenue_attribution_status"] == "hubspot_deal_ledger"
    waste = _by(r["campaigns"], "campaign_name", "waste")
    assert waste["roas"] == 0.0  # revenue source available, this campaign truly earned 0
    gulf = _by(r["campaigns"], "campaign_name", "gulf")
    assert gulf["roas"] == 5.0


# ── Exclusions ───────────────────────────────────────────────────────────────


def test_unknown_blank_campaign_rows_excluded():
    r = _build(
        spend_rows=[
            {"campaign_name": "gulf", "country": "SA", "spend": 1000.0},
            {"campaign_name": None, "country": None, "spend": 50.0},  # unknown
        ],
        revenue_rows=[],
    )
    names = [c["campaign_name"] for c in r["campaigns"]]
    assert "unknown" not in names
    assert None not in names
    assert "gulf" in names


def test_sql_withheld_when_event_date_unsafe():
    r = _build(
        spend_rows=[{"campaign_name": "gulf", "country": "SA", "spend": 1000.0}],
        revenue_rows=[],
        lead_rows=[{"campaign_name": "gulf", "country": "SA", "status_category": "qualified", "has_gclid": True}],
        safe=False,
    )
    assert r["lead_metrics_status"] == "withheld"
    assert r["summary"]["sqls"] is None
    gulf = _by(r["campaigns"], "campaign_name", "gulf")
    assert gulf["sqls"] is None


# ── Repository remains read-only ─────────────────────────────────────────────


def test_repository_is_select_only():
    source = inspect.getsource(repo).upper()
    for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "TRUNCATE", "MUTATE"):
        assert forbidden not in source


def test_no_external_or_write_calls_in_repo_and_service():
    for mod in (repo, svc):
        src = inspect.getsource(mod)
        assert "requests.post" not in src
        assert "requests.put" not in src
        assert "google.ads" not in src
        assert "hubapi.com" not in src
        assert "mutate" not in src.lower()


def test_no_windsor_in_service_or_repo():
    assert "windsor" not in inspect.getsource(svc).lower()
    assert "windsor" not in inspect.getsource(repo).lower()


# ── Frontend truth-layer behavior ────────────────────────────────────────────
# NOTE: the PR-ADS-109 truth-banner / audit-button UI was demolished in
# PR-ADS-110 (business pages are decision pages, not diagnostics). The frontend
# truth-safety behavior is now asserted in tests/test_pr_ads_110_revenue_ui_demolition.py
# (clean blocked card when unsafe, no audit button on business pages).
# The backend safety contract above remains the durable proof.
