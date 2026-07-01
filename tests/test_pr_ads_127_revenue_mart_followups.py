"""
PR-ADS-127 — Follow-up fixes to the Revenue Decision Mart rewire (PR-ADS-126).

A post-merge adversarial review of PR-ADS-126 confirmed four defects (none corrupt
data or fabricate ROAS, but several mislead):

  1. (high)   A durable deal-ledger OUTAGE rendered as a truthful "No closed-won
              deals found" empty window, because the mart dropped the ledger's
              availability signal.
  2. (medium) The Deals "Won Revenue" / "Average Deal Value" were derived from the
              canonical attribution total over the raw ledger row count, so they
              did not reconcile with the displayed deal rows when an exclusion /
              identity remap was configured.
  3/4. (low)  The country "no geo data" (unavailable) state showed reconciliation-
              MISMATCH wording ("Geo spend does not reconcile…") instead of a
              no-geo / run-the-geo-sync message.

These tests prove the fixes at the mart-contract level and via static UI checks.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
MART = open(os.path.join(ROOT, "services", "revenue_decision_mart.py"), encoding="utf-8").read()


def _slice(marker, span=1600):
    i = JS.find(marker)
    assert i != -1, f"{marker} not found in app.js"
    return JS[i:i + span]


# ── Fix #1 — deal-ledger outage is surfaced, never shown as empty ───────────

def test_mart_deal_view_surfaces_ledger_status(monkeypatch):
    import test_pr_ads_125_revenue_decision_mart as t125
    import db.revenue_repository as repo
    t125._patch_durable(monkeypatch)
    # Deal-ledger-specific outage: the gclid_attribution read is unavailable while
    # spend/leads (the attribution core) stay reachable.
    monkeypatch.setattr(repo, "fetch_revenue_deals", lambda s, e: {"available": False})
    from services.revenue_decision_mart import build_revenue_decision_mart
    mart = build_revenue_decision_mart(view="deal", window=t125.WINDOW, now=t125.NOW)
    assert mart["data_health"]["deal_ledger_status"] == "database_unavailable"
    assert mart["rows"] == []
    # The ledger summary is withheld (no fabricated zero ledger).
    assert (mart["ledger_summary"] or {}).get("won_revenue") is None


def test_deal_ledger_status_present_when_available(monkeypatch):
    import test_pr_ads_125_revenue_decision_mart as t125
    t125._patch_durable(monkeypatch)
    from services.revenue_decision_mart import build_revenue_decision_mart
    mart = build_revenue_decision_mart(view="deal", window=t125.WINDOW, now=t125.NOW)
    assert mart["data_health"]["deal_ledger_status"] == "available"


def test_loaddeals_maps_outage_to_unavailable_card_in_ui():
    loader = _slice("async function loadDeals", span=2000)
    # The mart's ledger status drives the ledger-unavailable state (not "empty").
    assert "data.data_health" in loader and "deal_ledger_status" in loader
    assert 'revenueDealsLedgerStatus = dealLedgerStatus === "database_unavailable"' in loader \
        or '"database_unavailable"' in loader
    # The database_unavailable branch is reachable again and renders the honest card.
    page = _slice("function renderRevenueDealsPage", span=900)
    assert '"database_unavailable"' in page
    assert "renderRevenueDealsUnavailable()" in page


# ── Fix #2 — Deals KPIs reconcile with the displayed rows ───────────────────

def test_mart_deal_ledger_summary_matches_rows(monkeypatch):
    import test_pr_ads_125_revenue_decision_mart as t125
    t125._patch_durable(monkeypatch)
    from services.revenue_decision_mart import build_revenue_decision_mart
    from services.revenue_attribution_service import build_revenue_deals
    mart = build_revenue_decision_mart(view="deal", window=t125.WINDOW, now=t125.NOW)
    ledger = build_revenue_deals(t125.WINDOW, now=t125.NOW)
    # The mart's deal summary IS the deal ledger's own summary (same source as rows).
    assert mart["ledger_summary"] == ledger["summary"]
    rows_sum = round(sum(r["deal_amount_usd"] for r in mart["rows"]), 2)
    assert mart["ledger_summary"]["won_revenue"] == rows_sum
    assert mart["ledger_summary"]["deal_count"] == len(mart["rows"])


def test_ui_deal_summary_sourced_from_ledger_summary():
    fn = _slice("function martDealLedgerSummary", span=900)
    # Prefer the backend deal-ledger summary; never the canonical attribution total.
    assert "data.ledger_summary" in fn
    assert "won_revenue_usd" not in fn
    # Fallback (older payloads) sums the displayed rows — still internally consistent.
    assert "deal_amount_usd" in fn


# ── Fix #3/#4 — country no-geo wording is distinct from a reconciliation mismatch ─

def test_country_unavailable_uses_no_geo_wording(monkeypatch):
    import test_pr_ads_125_revenue_decision_mart as t125
    import db.revenue_repository as repo
    t125._patch_durable(monkeypatch)
    # Canonical campaign spend unreachable -> reconciliation is None -> country
    # spend status "unavailable" (no geo to reconcile), with revenue still ready.
    monkeypatch.setattr(repo, "fetch_canonical_campaign_spend", lambda s, e: {"available": False})
    from services.revenue_decision_mart import build_revenue_decision_mart
    mart = build_revenue_decision_mart(view="country", window=t125.WINDOW, now=t125.NOW)
    assert mart["spend_truth"]["country_spend_status"] == "unavailable"
    assert mart["readiness"]["revenue_decision_ready"] is True


def test_country_card_copy_branches_on_status():
    # PR-ADS-128: the card takes the whole spend_truth and renders cause-specific
    # copy (mismatch vs unavailable) plus status chips.
    fn = _slice("function renderCountrySpendUnreconciledState", span=3800)
    assert "function renderCountrySpendUnreconciledState(spendTruth)" in JS
    assert 'status === "mismatch"' in fn
    # Mismatch wording (geo exists but doesn't reconcile).
    assert "does not reconcile" in fn
    # No-geo wording (nothing to reconcile yet — run the geo sync).
    assert "no canonical Google Ads geo" in fn
    # Both still surface the recovery CTA.
    assert "roasCountryCtaButtons()" in fn


def test_country_call_site_passes_status():
    page = _slice("function renderRoasCountriesPage", span=2600)
    assert "renderCountrySpendUnreconciledState(st)" in page


# ── Read-only doctrine preserved ────────────────────────────────────────────

def test_followups_introduce_no_writes():
    lowered = MART.lower()
    for forbidden in (
        "execute_action", "mutate", "upload_offline", "offline_conversion",
        "google_ads_client", "googleads", "hubspot_client",
        "requests.post", "requests.put", "requests.patch", "requests.delete",
        "import requests", "db.writers",
    ):
        assert forbidden not in lowered, f"mart must not reference '{forbidden}'"
