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

from tests.canonical_ledger_fixtures import patch_canonical_ledger  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
MART = open(os.path.join(ROOT, "services", "revenue_decision_mart.py"), encoding="utf-8").read()


def _slice(marker, span=1600):
    i = JS.find(marker)
    assert i != -1, f"{marker} not found in app.js"
    return JS[i:i + span]


def _fn(marker):
    """The whole top-level app.js function starting at ``marker`` (to its
    closing ``}`` at column 0) — robust to a body that grows (PR-ADS-161E's
    longer period-ratio copy pushed the CTA past a fixed span)."""
    i = JS.find(marker)
    assert i != -1, f"{marker} not found in app.js"
    j = JS.find("\n}\n", i)
    assert j != -1, f"end of {marker} not found in app.js"
    return JS[i:j + 3]


# ── Fix #1 — deal-ledger outage is surfaced, never shown as empty ───────────

def test_mart_deal_view_surfaces_ledger_status(monkeypatch):
    import test_pr_ads_125_revenue_decision_mart as t125
    t125._patch_durable(monkeypatch)
    # PR-ADS-153E-B: the deal-ledger outage is now an outage of the CANONICAL
    # ledger, while spend/leads (the attribution core) stay reachable.
    patch_canonical_ledger(monkeypatch, [], available=False)
    from services.revenue_decision_mart import build_revenue_decision_mart
    mart = build_revenue_decision_mart(view="deal", window=t125.WINDOW, now=t125.NOW)
    # PR-ADS-153E-B: the status names the canonical ledger, so an operator can
    # tell a canonical-read failure from a generic database outage.
    assert mart["data_health"]["deal_ledger_status"] == "canonical_ledger_unreadable"
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
    # PR-ADS-164A. This test used to assert the mechanism that WAS the defect:
    # the loader recognised only `deal_ledger_status === "database_unavailable"`
    # and defaulted every other status to "available" — but since PR-ADS-153E-B
    # the server sends the canonical reason (`canonical_ledger_unreadable`,
    # `canonical_ledger_stale`, …) and never that string, so the branch was
    # unreachable and an outage rendered as "No closed-won deals found". The
    # outage is now driven by the canonical verdict the mart passes through,
    # and anything but a published verdict renders the unavailable card.
    # PR-ADS-178B: the deal list is the Pipeline page's (pipelineDealsHtml).
    page = _fn("function pipelineDealsHtml")
    rows = _fn("function pipelineDealRows")
    assert "closedWonPopulation(data)" in rows
    assert 'closedWonPopulationState(pop, data.deals.length) === "blocked"' in rows
    assert "closedWonBlockedHtml(pipelineDealListVerdict(d))" in page
    assert '"database_unavailable"' not in page
    loader = _fn("async function loadPipeline")
    assert '|| "available"' not in loader, "a missing status must never default to available"


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
    # PR-ADS-178B: the Pipeline builder reads the deal KPIs from the backend
    # (the mart deal view's all-source summary metrics), never the canonical
    # ATTRIBUTION total, and shows the list only when it numbers that count.
    svc = open(os.path.join(ROOT, "services", "pipeline_service.py"), encoding="utf-8").read()
    assert 'revenue_m = _verdict(summary_metrics.get("revenue_usd"))' in svc
    assert "attributed" not in svc
    assert "len(rows) != deals[\"value\"]" in svc
    # PR-ADS-164A: the client-side fallback is gone. It re-added the rows with
    # `Number(r.deal_amount_usd) || 0`, which counts a deal with no proven
    # amount as zero and publishes the priced remainder as "Won Revenue" — a
    # partial presented as the total. The KPI cards render the server's
    # verdicts only.
    own = _fn("function pipelineKpiCardsHtml")
    assert "deal_amount_usd" not in own and ".reduce(" not in own


# ── Fix #3/#4 — country no-geo wording is distinct from a reconciliation mismatch ─

def test_country_unavailable_uses_no_geo_wording(monkeypatch):
    import test_pr_ads_125_revenue_decision_mart as t125
    import db.revenue_repository as repo
    t125._patch_durable(monkeypatch)
    # Canonical campaign spend unreachable -> reconciliation is None -> country
    # spend status "unavailable" (no geo to reconcile), with revenue still ready.
    monkeypatch.setattr(repo, "fetch_canonical_campaign_spend", lambda s, e, *_a, **_k: {"available": False})
    from services.revenue_decision_mart import build_revenue_decision_mart
    mart = build_revenue_decision_mart(view="country", window=t125.WINDOW, now=t125.NOW)
    assert mart["spend_truth"]["country_spend_status"] == "unavailable"
    assert mart["readiness"]["revenue_decision_ready"] is True


def test_country_card_copy_branches_on_status():
    # PR-ADS-128: the card takes the whole spend_truth and renders cause-specific
    # copy (mismatch vs unavailable) plus status chips.
    fn = _fn("function renderCountrySpendUnreconciledState")
    assert "function renderCountrySpendUnreconciledState(spendTruth)" in JS
    assert 'status === "mismatch"' in fn
    # Mismatch wording (geo exists but doesn't reconcile).
    assert "does not reconcile" in fn
    # No-geo wording (nothing to reconcile yet — run the geo sync).
    assert "no canonical Google Ads geo" in fn
    # Both still surface the recovery CTA. PR-ADS-176A truth audit MINOR 1:
    # it is told which runs the card's gap warrants.
    assert "roasCountryCtaButtons(guide.run)" in fn


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
