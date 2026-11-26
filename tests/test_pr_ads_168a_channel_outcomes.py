"""PR-ADS-168A — closed-won outcomes per channel on the Channels page.

The Channels page counted contacts ACQUIRED in a window, by HubSpot original
source (PR-ADS-162A). This adds the deals that CLOSED in the same days, per
channel, through the reader contract (``load_won_deals`` / ``reader_row`` in
one snapshot):

* a won deal takes channel X only when EVERY associated contact's
  ``hs_analytics_source`` (normalised by ``channel_funnel_service.source_key``)
  is X; every other deal sits on the explicit ``Source not resolved`` row with
  its reason — never on a guessed channel;
* the rows (unresolved included) add back up to the window's own deal count,
  or every row is withheld (``channel_partition_does_not_reconcile``);
* customers won are distinct companies per row and the total is the window's
  own count — never a sum of the rows;
* only Paid Search carries spend (Google Ads); every other row's spend is
  unavailable (``channel_spend_not_connected``) and so are its ratios;
* outcomes and spend end YESTERDAY (the PR-ADS-167A settled-day rule); the
  funnel keeps its own cohort and includes today, and the page says so;
* the page renders each half when the other is withheld.

The production fixtures model production identities: the universe's
``acquisition_contacts`` rows carry exactly the columns production's read
returns (``deal_id``, ``contact_id``, ``contact_created_at``,
``funnel_row_present``, ``hs_analytics_source``), with HubSpot's own source
values (``PAID_SEARCH``, ``DIRECT_TRAFFIC`` …).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from analysis import closed_won_truth as cwt
from tests.canonical_ledger_fixtures import (
    company_evidence, fresh_sync_state, ledger_row, patch_canonical_ledger,
)

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

#: 12:00 UTC on 20 Nov 2026 (GMT): account today is the 20th, yesterday the 19th.
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 20)
YESTERDAY = date(2026, 11, 19)
THIS_MORNING = datetime(2026, 11, 20, 6, 2, tzinfo=timezone.utc)


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════════════════
@pytest.fixture(autouse=True)
def _meta_not_set_up(monkeypatch):
    """PR-ADS-171B: the page now reads Meta spend too. These tests are about
    the page as it was before Meta was set up, so they say so: no Meta
    variable, nothing stored — the truth's ``not_connected``. Without this
    the store is unreadable here, which the Meta line reads, correctly, as
    "could not tell" (tests/test_pr_ads_171b_meta_on_channels.py)."""
    from db import meta_ads_repository as meta_repo
    from tests import meta_ads_fakes
    meta_ads_fakes.unconfigure(monkeypatch)
    monkeypatch.setattr(meta_repo, "fetch_meta_accounts",
                        lambda: {"available": True, "accounts": []})
    monkeypatch.setattr(meta_repo, "fetch_meta_account_sync_states",
                        lambda: {"available": True, "states": {}})


def _contact(deal, cid, source, *, in_funnel=True):
    """One row of production's ``acquisition_contacts`` read."""
    return {"deal_id": str(deal), "contact_id": str(cid),
            "contact_created_at": "2026-03-01T10:00:00+00:00" if in_funnel else None,
            "funnel_row_present": in_funnel,
            "hs_analytics_source": source if in_funnel else None}


def _deal(deal_id, day="2026-11-10", **kw):
    kw.setdefault("deal_close_date", f"{day}T10:00:00+00:00")
    return ledger_row(deal_id, **kw)


def _spend(monkeypatch, *, usd=4000.0, native=3200.0, fetched_at=THIS_MORNING,
           status="verified", fx_complete=True, zone="Europe/London"):
    """The canonical campaign-spend tables, as production's reads return them:
    one verified ledger chunk through today, fetched this morning."""
    import db.revenue_repository as repo
    import services.canonical_contract as cc
    import services.google_ads_spend_service as gss

    def canonical(start, end, customer_id=None):
        return {"available": True,
                "rows": [{"campaign_id": "111", "campaign_name": "brand",
                          "cost_micros": int(native * 1e6), "spend": native,
                          "spend_usd": usd, "fx_complete": fx_complete}] if native else [],
                "total_cost_micros": int(native * 1e6), "total_spend": native,
                "total_spend_usd": usd if fx_complete else None,
                "campaign_count": 1 if native else 0, "customer_id": "123",
                "currency_code": "GBP", "customer_count": 1, "currency_count": 1,
                "reporting_currency": "USD", "fx_missing_days": 0 if fx_complete else 3,
                "fx_complete": fx_complete,
                "coverage_start": "2024-01-01" if native else None,
                "coverage_end": YESTERDAY.isoformat() if native else None}

    def coverage(start, end, customer_id=None):
        return {"available": True, "chunks": [
            {"chunk_start": "2020-01-01", "chunk_end": TODAY.isoformat(),
             "status": status, "rows_written": 1, "cost_micros_total": 0,
             "customer_id": "123", "fetched_at": fetched_at}]}

    monkeypatch.setattr(repo, "fetch_canonical_campaign_spend", canonical)
    monkeypatch.setattr(repo, "fetch_spend_coverage", coverage)
    monkeypatch.setattr(cc, "configured_account_time_zone", lambda: zone)
    monkeypatch.setattr(gss, "configured_customer_id", lambda: "123")


def _ledger(monkeypatch, rows, contacts, *, companies=None, sync_state=None,
            evidence=True):
    patch_canonical_ledger(
        monkeypatch, rows, sync_state=sync_state or fresh_sync_state(NOW),
        acquisition_contacts=contacts,
        company_associations=(company_evidence(rows, companies=companies,
                                               scan_started_at=NOW.isoformat())
                              if evidence else None))


def _outcomes(window="180d"):
    from services.source_attribution_service import build_channel_outcomes
    return build_channel_outcomes(window, now=NOW)


def _row(out, source):
    return next(r for r in out["channels"] if r["source"] == source)


def _mixed_ledger(monkeypatch, **kw):
    """Seven deals in the 180d window covering every placement class."""
    rows = [
        _deal("ps1"), _deal("ps2", "2026-10-01"),
        _deal("dt1", "2026-09-15"),
        _deal("mix", "2026-11-01"),
        _deal("none", "2026-11-02", association_status="none",
              association_count=0),
        _deal("fail", "2026-11-03", association_status="lookup_failed",
              association_count=None),
        _deal("lost", "2026-11-04"),            # contact not in the funnel
        _deal("blank", "2026-11-05"),           # contact with no source
        _deal("gone", "2026-11-06"),            # resolved, rows not stored
    ]
    contacts = [
        _contact("ps1", 1, "PAID_SEARCH"), _contact("ps1", 2, "paid_search "),
        _contact("ps2", 3, "PAID_SEARCH"),
        _contact("dt1", 4, "DIRECT_TRAFFIC"),
        _contact("mix", 5, "PAID_SEARCH"), _contact("mix", 6, "DIRECT_TRAFFIC"),
        _contact("lost", 7, None, in_funnel=False),
        {**_contact("blank", 8, "PAID_SEARCH"), "hs_analytics_source": "  "},
    ]
    _ledger(monkeypatch, rows, contacts, **kw)
    return rows


# ═════════════════════════════════════════════════════════════════════════════
# The deal → channel rule and the partition
# ═════════════════════════════════════════════════════════════════════════════
def test_01_every_won_deal_is_on_exactly_one_row_and_the_rows_reconcile(monkeypatch):
    _mixed_ledger(monkeypatch)
    _spend(monkeypatch)
    out = _outcomes()
    assert out["metric_family"] == "closed_won_outcomes"
    assert out["window_basis"] == "deal_close_date"
    p = out["partition"]
    assert p["reconciles"] is True
    assert p["canonical_closed_won_deals"] == 9 == p["deals_in_rows"]
    assert sum(r["closed_won_deals"] for r in out["channels"]) == 9
    assert _row(out, "PAID_SEARCH")["closed_won_deals"] == 2
    assert _row(out, "DIRECT_TRAFFIC")["closed_won_deals"] == 1
    un = _row(out, "__deal_source_unresolved__")
    # PR-ADS-181A: the row's label is "Deals with no single source" (was
    # "Source not resolved"); its identifier, members and figures are unchanged.
    assert un["label"] == "Deals with no single source" and un["outcome_only"] is True
    assert un["closed_won_deals"] == 6
    assert un["unresolved_by_class"] == p["unresolved_by_class"] == {
        "contacts_disagree": 1, "no_associated_contact": 1,
        "association_lookup_failed": 1, "contact_not_in_funnel": 1,
        "contact_has_no_source": 1, "associated_contacts_not_stored": 1}


def test_02_the_rule_reads_the_contacts_source_never_the_ledgers_display_pick():
    from services.source_attribution_service import deal_channel
    # The ledger's source_primary_raw would say PAID_SEARCH (lowest contact id);
    # the contacts disagree, so the deal is not placed.
    deal = ledger_row("d", source_primary_raw="PAID_SEARCH",
                      attribution_status="attributed")
    assert deal_channel(deal, [_contact("d", 1, "PAID_SEARCH"),
                               _contact("d", 2, "OFFLINE")]) == (None, "contacts_disagree")
    # Normalised exactly as the funnel's own channel key.
    assert deal_channel(deal, [_contact("d", 1, " paid_search")]) == ("PAID_SEARCH", None)
    # A GCLID deal HubSpot recorded as Direct sits on Direct.
    gclid = ledger_row("g", gclid="Cj0KCQ", acquisition_group="google_ads")
    assert deal_channel(gclid, [_contact("g", 1, "DIRECT_TRAFFIC")]) == ("DIRECT_TRAFFIC", None)
    # Only a SUCCESSFUL empty lookup is "no associated contact".
    assert deal_channel(ledger_row("n", association_status="none"), []) == (
        None, "no_associated_contact")
    assert deal_channel(ledger_row("f", association_status="lookup_failed"),
                        [_contact("f", 1, "PAID_SEARCH")]) == (None, "association_lookup_failed")
    assert deal_channel(ledger_row("r", association_status="resolved"), []) == (
        None, "associated_contacts_not_stored")


def test_03_a_partition_that_does_not_add_up_withholds_every_row(monkeypatch):
    import services.source_attribution_service as sas
    _mixed_ledger(monkeypatch)
    _spend(monkeypatch)
    real = sas.partition_deals_by_channel

    def drops_a_deal(members, contacts):
        return real(list(members)[1:], contacts)

    monkeypatch.setattr(sas, "partition_deals_by_channel", drops_a_deal)
    out = _outcomes()
    assert out["partition"]["reconciles"] is False
    assert out["partition"]["reason"] == "channel_partition_does_not_reconcile"
    assert out["partition"]["deals_in_rows"] is None
    for r in out["channels"]:
        for m in ("closed_won_deals", "revenue_usd", "customers"):
            assert r["metrics"][m] == {**r["metrics"][m], "value": None,
                                       "status": "withheld",
                                       "reason": "channel_partition_does_not_reconcile"}
        assert r["closed_won_deals"] is None and r["customers"] is None
    # The window's own totals are not a sum of the rows: still published.
    assert out["totals"]["closed_won_deals"] == 9


# ═════════════════════════════════════════════════════════════════════════════
# Customers, revenue, and the window verdict
# ═════════════════════════════════════════════════════════════════════════════
def _two_channel_company(monkeypatch):
    rows = [_deal("a"), _deal("b", "2026-11-11")]
    contacts = [_contact("a", 1, "PAID_SEARCH"), _contact("b", 2, "DIRECT_TRAFFIC")]
    _ledger(monkeypatch, rows, contacts, companies={"a": ["co-1"], "b": ["co-1"]})
    _spend(monkeypatch)
    return _outcomes()


def _assert_totals_are_the_windows(out):
    rows_sum = sum(r["customers"] or 0 for r in out["channels"])
    t = out["totals"]
    assert t["metrics"]["customers"]["status"] == "published"
    assert t["customers"] == 1 < rows_sum
    assert t["metrics"]["customers"]["additive"] is False


def test_04_a_company_in_two_channels_counts_once_per_row_and_once_in_total(monkeypatch):
    out = _two_channel_company(monkeypatch)
    assert _row(out, "PAID_SEARCH")["customers"] == 1
    assert _row(out, "DIRECT_TRAFFIC")["customers"] == 1
    _assert_totals_are_the_windows(out)


def test_05_counterfactual_a_summed_total_is_caught(monkeypatch):
    import services.source_attribution_service as sas
    real = sas._totals_block

    def summed(base, spend):
        t = real(base, spend)
        n = len({c for v in (base.get("company_ids_by_deal") or {}).values()
                 for c in v}) + 1          # what a per-row sum gives here (2)
        t["customers"] = n
        t["metrics"]["customers"] = cwt.metric(n, cwt.PUBLISHED)
        return t

    monkeypatch.setattr(sas, "_totals_block", summed)
    out = _two_channel_company(monkeypatch)
    with pytest.raises(AssertionError):
        _assert_totals_are_the_windows(out)


def test_06_an_unpriced_deal_withholds_revenue_for_its_row_and_total_not_counts(monkeypatch):
    rows = [_deal("a"), _deal("b", revenue_usd=None, amount_raw=None,
                              currency_status="unavailable", currency_reason="no_amount"),
            _deal("c")]
    contacts = [_contact("a", 1, "PAID_SEARCH"), _contact("b", 2, "PAID_SEARCH"),
                _contact("c", 3, "OFFLINE")]
    _ledger(monkeypatch, rows, contacts)
    _spend(monkeypatch)
    out = _outcomes()
    ps = _row(out, "PAID_SEARCH")
    assert ps["closed_won_deals"] == 2 and ps["customers"] == 2
    assert ps["won_revenue"] is None
    assert ps["metrics"]["revenue_usd"]["status"] == "withheld"
    assert ps["metrics"]["period_revenue_per_ad_spend"]["reason"] == \
        cwt.R_CHANNEL_POPULATION_NOT_SPEND
    assert out["google_ads_line"]["metrics"]["period_revenue_per_ad_spend"][
        "status"] == "withheld"
    assert out["totals"]["won_revenue"] is None
    assert out["totals"]["metrics"]["revenue_usd"]["status"] == "withheld"
    assert out["totals"]["closed_won_deals"] == 3
    # A subset of a withheld total is not exact either: Offline's own priced
    # deal does not publish a revenue figure.
    assert _row(out, "OFFLINE")["won_revenue"] is None


def test_07_a_stale_ledger_publishes_no_outcome_at_all(monkeypatch):
    from scripts import audit_customer_revenue_reader_cutover as audit
    _mixed_ledger(monkeypatch, sync_state=fresh_sync_state(NOW, hours_ago=80))
    _spend(monkeypatch)
    out = _outcomes()
    assert out["channels"] is None and out["partition"] is None
    for m in ("closed_won_deals", "revenue_usd", "customers"):
        assert out["totals"]["metrics"][m]["value"] is None
        assert out["totals"]["metrics"][m]["reason"] == cwt.R_LEDGER_STALE
        assert out["publication"][m]["status"] != "published"
    a = audit.Audit()
    audit.page_leak_checks(a, "180d", "channel_outcomes", out["publication"], out)
    assert not a.violations, a.violations


def test_08_withheld_customers_are_withheld_on_every_row(monkeypatch):
    rows = [_deal("a"), _deal("b")]
    _ledger(monkeypatch, rows, [_contact("a", 1, "PAID_SEARCH"),
                                _contact("b", 2, "OFFLINE")], evidence=False)
    _spend(monkeypatch)
    out = _outcomes()
    assert out["totals"]["metrics"]["customers"]["status"] != "published"
    for r in out["channels"]:
        assert r["customers"] is None
        assert r["metrics"]["customers"]["status"] != "published"
        assert r["metrics"]["period_spend_per_customer_won"]["value"] is None
    assert _row(out, "PAID_SEARCH")["closed_won_deals"] == 1


def test_09_counterfactual_a_row_that_ignores_the_window_verdict_is_caught(monkeypatch):
    from scripts import audit_customer_revenue_reader_cutover as audit
    import services.canonical_revenue_service as crs
    real = crs.reader_row

    def ungated(base, rows, **kw):
        row = real(base, rows, **kw)
        row["customers"] = len(rows)           # a figure beside a withheld metric
        return row

    rows = [_deal("a")]
    _ledger(monkeypatch, rows, [_contact("a", 1, "PAID_SEARCH")], evidence=False)
    _spend(monkeypatch)
    monkeypatch.setattr(crs, "reader_row", ungated)
    out = _outcomes()
    a = audit.Audit()
    audit.page_leak_checks(a, "180d", "channel_outcomes", out["publication"], out)
    assert a.checks.get("withheld_not_reconstructable") is False


# ═════════════════════════════════════════════════════════════════════════════
# Spend: Google Ads on Paid Search only
# ═════════════════════════════════════════════════════════════════════════════
def _probe_ledger(monkeypatch, *, resolver=True):
    """Review round 1's probe. Google Ads spend $4,000 and three deals:
    g1 Paid Search on a mapped campaign ($10k); b1 Paid Search whose label is
    approved as NOT Google Ads (PR-ADS-120b; $50k); d1 a GCLID deal HubSpot
    recorded as Direct Traffic ($20k)."""
    from tests.canonical_ledger_fixtures import label_resolver
    rows = [
        _deal("g1", revenue_usd=10000.0, amount_raw=10000.0, campaign_name_raw="brand",
              source_primary_raw="PAID_SEARCH", acquisition_group="google_ads",
              attribution_status="attributed", gclid="abc"),
        _deal("b1", revenue_usd=50000.0, amount_raw=50000.0,
              campaign_name_raw="bing-brand", source_primary_raw="PAID_SEARCH",
              acquisition_group="google_ads", attribution_status="attributed"),
        _deal("d1", revenue_usd=20000.0, amount_raw=20000.0, campaign_name_raw="brand",
              gclid="xyz", source_primary_raw="DIRECT_TRAFFIC",
              acquisition_group="organic", attribution_status="attributed"),
    ]
    patch_canonical_ledger(
        monkeypatch, rows, sync_state=fresh_sync_state(NOW),
        acquisition_contacts=[_contact("g1", 1, "PAID_SEARCH"),
                              _contact("b1", 2, "PAID_SEARCH"),
                              _contact("d1", 3, "DIRECT_TRAFFIC")],
        company_associations=company_evidence(rows, scan_started_at=NOW.isoformat()),
        resolver=label_resolver(not_google_ads=("bing-brand",)))
    if not resolver:
        import services.canonical_customer_revenue_service as ccr
        monkeypatch.setattr(ccr, "campaign_resolver",
                            lambda now=None: (None, "resolver unavailable"))
    _spend(monkeypatch, usd=4000.0)
    return _outcomes()


def _assert_paid_search_ratio_refused(out):
    ps = _row(out, "PAID_SEARCH")
    for k in cwt.PERIOD_RATIO_KEYS:
        assert ps[k] is None
        assert ps["metrics"][k] == cwt.incompatible_rate(
            cwt.R_CHANNEL_POPULATION_NOT_SPEND)


def test_10_paid_search_refuses_its_ratios_and_its_spend_cell(monkeypatch):
    """Review round 1 BLOCKER. Paid Search is HubSpot's original source: it
    holds b1 (approved as not Google Ads) and misses d1 (Google Ads, recorded
    as Direct). Its $60k over Google Ads spend was published as 15.0x, which
    is neither the measurement ($30k / $4k) nor a bound of it."""
    out = _probe_ledger(monkeypatch)
    ps = _row(out, "PAID_SEARCH")
    assert (ps["closed_won_deals"], ps["won_revenue"], ps["customers"]) == (2, 60000.0, 2)
    # The spend cell points to the Google Ads line (review round 2): the
    # page shows no figure a reader could divide Paid Search revenue by.
    assert ps["spend"] == {"value": None, "status": "unavailable",
                           "reason": "spend_shown_on_google_ads_line"}
    assert out["spend"]["google_ads"]["value"] == 4000.0
    _assert_paid_search_ratio_refused(out)
    assert ps["roas"] is None and ps["metrics"]["roas"]["status"] == "unavailable"


def test_10b_the_google_ads_line_is_the_measured_comparison(monkeypatch):
    out = _probe_ledger(monkeypatch)
    g = out["google_ads_line"]
    assert g["label"] == "Google Ads (canonical evidence)"
    assert g["in_channel_partition"] is False and g["added_to_totals"] is False
    assert (g["closed_won_deals"], g["won_revenue"], g["customers"]) == (2, 30000.0, 2)
    assert g["spend"]["value"] == 4000.0
    assert g["period_revenue_per_ad_spend"] == 7.5
    assert g["period_spend_per_customer_won"] == 2000.0
    for k in cwt.PERIOD_RATIO_KEYS:
        assert g["metrics"][k]["ratio_kind"] == cwt.RATIO_KIND_OPERATIONAL
    # b1 is not in the line; d1 is in the line AND on the Direct row.
    assert _row(out, "DIRECT_TRAFFIC")["closed_won_deals"] == 1
    assert _row(out, "DIRECT_TRAFFIC")["won_revenue"] == 20000.0
    # The line is not a channel row: the partition still sums to the window.
    assert out["partition"]["deals_in_rows"] == 3 == out["totals"]["closed_won_deals"]
    assert all(r["source"] != "__google_ads_evidence__" for r in out["channels"])


def test_10c_counterfactual_the_old_paid_search_ratio_is_caught(monkeypatch):
    import services.source_attribution_service as sas
    monkeypatch.setattr(sas, "_PAID_SEARCH_RATIO_GATE", None)
    monkeypatch.setattr(sas, "_channel_spend", lambda key, google: (
        google if key == "PAID_SEARCH"
        else sas._unconnected_spend(cwt.R_CHANNEL_SPEND_NOT_CONNECTED)))
    out = _probe_ledger(monkeypatch)
    ps = _row(out, "PAID_SEARCH")
    assert ps["period_revenue_per_ad_spend"] == 15.0     # the proxy ratio
    with pytest.raises(AssertionError):
        _assert_paid_search_ratio_refused(out)


def test_10d_no_resolver_means_the_google_ads_line_is_withheld_not_guessed(monkeypatch):
    out = _probe_ledger(monkeypatch, resolver=False)
    g = out["google_ads_line"]
    assert g["closed_won_deals"] is None and g["won_revenue"] is None
    assert g["metrics"]["closed_won_deals"]["reason"] == cwt.R_CAMPAIGN_IDENTITY_UNAVAILABLE
    assert g["period_revenue_per_ad_spend"] is None
    # The channel rows still publish: the line's gap is the line's alone.
    assert _row(out, "PAID_SEARCH")["closed_won_deals"] == 2


def test_10e_a_spend_zone_other_than_the_deal_days_withholds_the_spend(monkeypatch):
    _two_channel_company(monkeypatch)
    _spend(monkeypatch, zone="America/New_York")
    out = _outcomes()
    assert out["spend"]["google_ads"]["reason"] == "account_time_zone_differs_from_evidence_days"
    assert _row(out, "PAID_SEARCH")["spend"]["value"] is None


def test_11_every_other_row_has_no_connected_spend_and_no_ratio(monkeypatch):
    _two_channel_company(monkeypatch)
    out = _outcomes()
    for r in out["channels"]:
        if r["source"] == "PAID_SEARCH":
            continue
        want = ("deal_source_not_resolved" if r["source"] == "__deal_source_unresolved__"
                else cwt.R_CHANNEL_SPEND_NOT_CONNECTED)
        assert r["spend"] == {"value": None, "status": "unavailable", "reason": want}
        for k in cwt.PERIOD_RATIO_KEYS:
            assert r[k] is None
            if r["metrics"]["revenue_usd"]["status"] == "published" and k == cwt.PERIOD_REVENUE_PER_AD_SPEND:
                assert r["metrics"][k]["reason"] == want
    t = out["totals"]
    assert t["spend"] == {"value": None, "status": "unavailable",
                          "reason": "all_channel_spend_not_connected"}
    for k in cwt.PERIOD_RATIO_KEYS:
        assert t[k] is None and t["metrics"][k]["status"] == "unavailable"
    assert {s["platform"]: s["connected"] for s in out["spend_sources"]} == {
        "google_ads": True, "meta": False, "linkedin": False, "chatgpt": False}


def test_12_verified_zero_spend_is_a_published_zero_and_the_ratio_unavailable(monkeypatch):
    _two_channel_company(monkeypatch)
    _spend(monkeypatch, usd=0.0, native=0.0)
    out = _outcomes()
    g = out["google_ads_line"]
    assert g["spend"]["status"] == "published" and g["spend"]["value"] == 0.0
    assert g["metrics"]["period_revenue_per_ad_spend"] == {
        "value": None, "status": "unavailable", "reason": cwt.R_NO_SPEND_IN_WINDOW}
    assert out["spend"]["google_ads"]["value"] == 0.0


def test_13_spend_not_reread_since_yesterday_ended_is_not_published(monkeypatch):
    """Before the morning sync the last chunk was fetched while yesterday was
    in progress: the settle rule withholds the spend, named."""
    _two_channel_company(monkeypatch)
    _spend(monkeypatch, fetched_at=THIS_MORNING - timedelta(days=1))
    out = _outcomes()
    g = out["google_ads_line"]
    assert g["spend"] == {**g["spend"], "value": None, "status": "unavailable",
                          "reason": "spend_not_reread_since_day_ended"}
    assert g["period_revenue_per_ad_spend"] is None
    assert g["metrics"]["period_revenue_per_ad_spend"]["reason"] == \
        "spend_not_reread_since_day_ended"
    assert out["spend"]["google_ads_state"] == "coverage_incomplete"


def test_14_one_spend_state_machine_for_business_and_evidence_days():
    src = (ROOT / "services" / "revenue_attribution_service.py").read_text(encoding="utf-8")
    body = src[src.index("def _build_from_db("):src.index("def build_revenue_attribution(")]
    assert "canonical_campaign_spend_block(" in body
    assert "repo.fetch_spend_coverage(" not in body
    assert "repo.fetch_canonical_campaign_spend(" not in body
    sts = (ROOT / "services" / "revenue_spend_truth_service.py").read_text(encoding="utf-8")
    days = sts[sts.index("def build_google_ads_spend_truth_for_days("):]
    assert "canonical_campaign_spend_block(" in days
    assert "derive_google_ads_spend_truth(spend_truth_inputs(block))" in days


# ═════════════════════════════════════════════════════════════════════════════
# Windows: evidence days, ended at yesterday
# ═════════════════════════════════════════════════════════════════════════════
def test_15_outcomes_end_yesterday_and_say_so(monkeypatch):
    rows = [_deal("y", "2026-11-19"),
            _deal("t", deal_close_date="2026-11-20T09:00:00+00:00")]
    _ledger(monkeypatch, rows, [_contact("y", 1, "PAID_SEARCH"),
                                _contact("t", 2, "PAID_SEARCH")])
    _spend(monkeypatch)
    out = _outcomes("30d")
    w = out["window"]
    assert (w["window_type"], w["window_key"]) == ("evidence", "30d")
    assert (w["start_date"], w["end_date"]) == ("2026-10-22", "2026-11-19")
    assert w["end_exclusive"] == "2026-11-20T00:00:00+00:00"
    assert w["through_reason"] == "today_not_settled"
    assert w["disclosure"].startswith(
        "Through 19 Nov 2026 — today is not included until tomorrow morning's sync.")
    assert out["totals"]["closed_won_deals"] == 1
    assert _row(out, "PAID_SEARCH")["closed_won_deals"] == 1
    assert out["spend"]["end_date"] == "2026-11-19"


def test_16_the_reader_contract_reads_an_evidence_window_by_its_own_membership(monkeypatch):
    import services.canonical_customer_revenue_service as ccr
    import services.canonical_revenue_service as crs
    rows = [_deal("in", "2026-10-25"), _deal("out", "2026-09-01"),
            _deal("today", deal_close_date="2026-11-20T09:00:00+00:00")]
    _ledger(monkeypatch, rows, [])
    base = crs.load_won_deals("30d", window_type="evidence", now=NOW)
    truth = ccr.get_closed_won_truth([("evidence", "30d")], now=NOW)["windows"][0]
    assert sorted(d["deal_id"] for d in base["deals"]) == sorted(
        truth["membership"]["deal_ids"]) == ["in", "today"]
    assert base["window_type"] == "evidence"
    # A business key under the evidence type, an unknown type, and bounds
    # outside the window are refused — never approximated.
    assert crs.load_won_deals("current_quarter", window_type="evidence",
                              now=NOW)["reason"] == crs.REASON_UNKNOWN_WINDOW
    assert crs.load_won_deals("30d", window_type="fiscal",
                              now=NOW)["reason"] == crs.REASON_UNKNOWN_WINDOW
    wide = (datetime(2020, 1, 1, tzinfo=timezone.utc), NOW)
    assert crs.load_won_deals("30d", window_type="evidence", now=NOW,
                              bounds=wide)["reason"] == crs.REASON_UNKNOWN_WINDOW
    # A business window is unchanged and carries no window_type.
    assert "window_type" not in crs.load_won_deals("all_time", now=NOW)


def test_17_an_unknown_window_is_an_error_not_a_smaller_window():
    from analysis.evidence_windows import EvidenceWindowError
    from services.source_attribution_service import build_channel_outcomes
    with pytest.raises(EvidenceWindowError):
        build_channel_outcomes("current_quarter", now=NOW)


# ═════════════════════════════════════════════════════════════════════════════
# The payload contract and the endpoint
# ═════════════════════════════════════════════════════════════════════════════
def test_18_no_period_ratio_is_named_roas_or_cac_and_no_identity_map_leaks(monkeypatch):
    from scripts import audit_customer_revenue_reader_cutover as audit
    _mixed_ledger(monkeypatch)
    _spend(monkeypatch)
    out = _outcomes()
    assert audit.ratio_name_violations(out) == []
    a = audit.Audit()
    audit.page_leak_checks(a, "180d", "channel_outcomes", out["publication"], out)
    assert not a.violations, a.violations
    text = json.dumps(out)
    assert "company_ids_by_deal" not in text
    assert not re.search(r"\b(CPQL|cpql|sql_to_customer|lead_to_customer)\b", text)


def test_19_the_endpoint_composes_both_halves_with_one_now(monkeypatch):
    import api.server as server
    import services.channel_funnel_service as cfs
    import services.source_attribution_service as sas
    seen = {}
    monkeypatch.setattr(cfs, "build_channel_funnel",
                        lambda w, now=None: seen.setdefault("f", now) and {"publication": {"status": "published"}})
    monkeypatch.setattr(sas, "build_channel_outcomes",
                        lambda w, now=None, channel_sources=(): seen.setdefault("o", now) and {"channels": []})
    out = server.api_channel_funnel(window="180d", _user={"role": "viewer"})
    assert out["outcomes"] == {"channels": []}
    assert seen["f"] is seen["o"]

    def boom(w, now=None, channel_sources=()):
        raise RuntimeError("ledger read failed")

    monkeypatch.setattr(sas, "build_channel_outcomes", boom)
    out = server.api_channel_funnel(window="180d", _user={"role": "viewer"})
    assert out["publication"] == {"status": "published"}       # the funnel survives
    assert out["outcomes"]["channels"] is None
    assert out["outcomes"]["publication"]["closed_won_deals"]["status"] == "unavailable"


def test_20_the_funnel_service_stays_free_of_reader_terms():
    src = (ROOT / "services" / "channel_funnel_service.py").read_text(encoding="utf-8")
    code = src.split('"""', 2)[2]
    for term in ("load_won_deals", "reader_row", "deal_ledger", "revenue_usd"):
        assert term not in code


# ═════════════════════════════════════════════════════════════════════════════
# The page, evaluated in node
# ═════════════════════════════════════════════════════════════════════════════
def _js_extract(source: str, head: str) -> str:
    i = source.index(head)
    j = source.index("{", i)
    depth = 0
    for k in range(j, len(source)):
        depth += {"{": 1, "}": -1}.get(source[k], 0)
        if depth == 0:
            end = k + 1
            return source[i:end + (1 if source[end:end + 1] == ";" else 0)]
    raise AssertionError(head)


def _render(payload, *, role=None, view=None, mutate=None) -> str:
    """The real renderChannelFunnel, evaluated in node. ``mutate`` replaces
    text inside the Channels span only (each old string must be there).
    PR-ADS-180A: the page's funnel is the shared block (funnelBlockHtml),
    defined outside the Channels span, so the real app.js is evaluated WHOLE
    (the 161B vm harness) instead of the span plus hand-picked helpers."""
    from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app
    src = APP_JS
    start = src.index("const CHANNEL_FUNNEL_REASON_LABELS = {")
    end = src.index("async function loadChannels()")
    span = src[start:end]
    for old, new in (mutate or {}).items():
        assert old in span, old
        span = span.replace(old, new)
    user = "null" if role is None else json.dumps({"role": role})
    return _run_app(f"_currentUser = {user}; renderChannelFunnel({json.dumps(payload)}, "
                    f"{json.dumps(view or {})})", js=src[:start] + span + src[end:])


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _funnel():
    from tests.test_pr_ads_162i_channels_page_revamp import _payload
    return _payload()


def _page(monkeypatch, out=None, **funnel_over):
    if out is None:
        out = _two_channel_company(monkeypatch)
    return {**_funnel(), **funnel_over, "outcomes": json.loads(json.dumps(out))}


def _tr(html, label):
    """The table row whose channel cell names ``label`` (PR-ADS-181A: the one
    ranked table; the "All channels" row is its first)."""
    table = html[html.index("<tbody>"):html.index("</tbody>")]
    for tr in re.findall(r"<tr[^>]*>.*?</tr>", table, re.S):
        if f'<span class="channels-name">{label}<' in tr or f"<strong>{label}<" in tr:
            return tr
    raise AssertionError(label)


def _cells(tr):
    """A row's outcome cells: [customers, revenue] (PR-ADS-181A: the deal
    count is the drawer's and the group's; spend and the period ratios live on
    the Google Ads card, never on a channel row)."""
    return [_text(c) for c in re.findall(
        r'<td class="num channels-col--(?:customers|revenue)">(.*?)</td>', tr, re.S)]


def _group(html, label="Deals closed"):
    i = html.index(f'aria-label="{label}"')
    return html[i:html.index("</section>", i)]


def _card(html, which):
    i = html.index(f"channels-paid__card--{which}")
    return html[i:html.index("channels-paid__card", i + 30)] if which == "google" \
        else html[i:html.index("</section>", i)]


def test_21_published_zeros_render_as_zero_and_gaps_as_the_word_and_reason(monkeypatch):
    html = _render(_page(monkeypatch))
    assert _cells(_tr(html, "Paid Social")) == ["0", "$0"]
    assert _cells(_tr(html, "Paid Search")) == ["1", "$1.0k"]
    # PR-ADS-181A: no channel row carries a spend or ratio cell any more (the
    # ~30 "Unavailable" paid-media cells are gone by design). The only spend
    # and ratio figures are the Google Ads card's, with their own verdicts.
    table = html[html.index("<table"):html.index("</table>")]
    for gone in ("no spend source is connected", "Google Ads spend is shown on",
                 "Non-cohort operational ratio", PERIOD_LABEL):
        assert gone not in table, gone
    assert "$4.0k" in _text(_card(html, "google"))


PERIOD_LABEL = "Period Revenue / Ad Spend"


def test_21b_the_probe_renders_paid_search_refused_and_the_google_ads_line(monkeypatch):
    html = _render(_page(monkeypatch, _probe_ledger(monkeypatch)))
    ps = _tr(html, "Paid Search")
    assert _cells(ps) == ["2", "$60.0k"]
    # Paid Search is never set against Google Ads spend: no spend, no ratio.
    assert "$4.0k" not in ps and "7.50x" not in ps and "15.00x" not in html
    g = _text(_card(html, "google"))
    assert "2 deals: 2 with a Google click ID, 0 by HubSpot source only" in g
    assert "Spend $4.0k" in g and "Revenue $30.0k" in g
    assert f"{PERIOD_LABEL} 7.50x Non-cohort operational ratio" in g
    assert "Spend per Customer Won This Period $2.0k Non-cohort operational ratio" in g
    assert "2 customers from 2 deals with a Google click ID or HubSpot Paid Search." in g
    assert "Deals closed in these days may come from earlier ads." in g
    assert _cells(_tr(html, "Direct Traffic")) == ["1", "$20.0k"]
    text = _text(html)
    assert "leaves out campaign labels approved as not Google Ads" in text
    assert "it does not by itself exclude other search engines" in text


def test_21c_a_failed_outcomes_payload_has_no_context_line(monkeypatch):
    failed = {"publication": {m: {"status": "unavailable", "reason": "channel_outcomes_failed"}
                              for m in ("closed_won_deals", "revenue_usd", "customers")},
              "totals": None, "channels": None, "partition": None,
              "google_ads_line": None,
              "metric_family": "closed_won_outcomes", "window_basis": "deal_close_date"}
    html = _render({**_funnel(), "outcomes": failed})
    text = _text(html)
    assert "undefined" not in text and "all time" not in text.lower()
    group = _text(_group(html))
    assert "Customers won Unavailable the channel outcomes could not be loaded" in group
    assert "Closed-won deals: Unavailable the channel outcomes could not be loaded" in group


def test_21d_a_funnel_source_the_label_table_does_not_know_reads_a_measured_zero(monkeypatch):
    from services.source_attribution_service import build_channel_outcomes
    funnel = _funnel()
    ch = json.loads(json.dumps(funnel["channels"][0]))
    ch.update(source="NEW_HUBSPOT_SOURCE", label="NEW_HUBSPOT_SOURCE", drilldowns=[])
    funnel["channels"].append(ch)
    _ledger(monkeypatch, [_deal("a")], [_contact("a", 1, "PAID_SEARCH")])
    _spend(monkeypatch)
    out = build_channel_outcomes("180d", now=NOW, channel_sources=[
        c["source"] for c in funnel["channels"]])
    html = _render({**funnel, "outcomes": json.loads(json.dumps(out))})
    assert _cells(_tr(html, "NEW_HUBSPOT_SOURCE")) == ["0", "$0"]
    assert "no outcome row" not in html


def test_22_no_outcome_cell_carries_a_percentage(monkeypatch):
    html = _render(_page(monkeypatch))
    cells = re.findall(r'<td class="num channels-col--(?:customers|revenue)">(.*?)</td>', html, re.S)
    assert cells and not any("%" in c for c in cells)


def test_23_total_customers_is_the_windows_count_when_rows_sum_higher(monkeypatch):
    page = _page(monkeypatch)
    html = _render(page)
    assert _cells(_tr(html, "All channels"))[0] == "1"
    assert _cells(_tr(html, "Paid Search"))[0] == "1"
    assert _cells(_tr(html, "Direct Traffic"))[0] == "1"
    group = _text(_group(html))
    assert "Customers won 1 distinct companies" in group
    assert "the rows don't add up to 1" in _text(html)
    # Counterfactual: a renderer that sums the rows shows 2 and is caught.
    summed = _render(page, mutate={
        'case "customers": return canonicalMetricHtml(channelOutcomeMetric(o, "customers"), channelOutcomeInt, ctx.oOpts);':
        'case "customers": return String(o.channels.reduce((n, r) => n + (r.customers || 0), 0));'})
    assert _cells(_tr(summed, "All channels"))[0] == "2"


def test_24_a_withheld_row_renders_the_word_even_if_a_figure_leaked(monkeypatch):
    out = _two_channel_company(monkeypatch)
    for r in out["channels"] + [out["totals"]]:
        r["metrics"]["customers"] = {"value": None, "status": "withheld",
                                     "reason": cwt.R_CUSTOMER_IDENTITY}
        r["customers"] = 3                    # a leaked figure the page must ignore
    page = _page(monkeypatch, out)
    html = _render(page)
    cell = _cells(_tr(html, "Paid Search"))[0]
    assert cell.startswith("Withheld a won deal has no company, or several") and "3" not in cell
    # A withheld row is no leader: "Most from" is not said on customers.
    assert "Most from" not in _text(_group(html)).split("Closed-won revenue")[0]
    # Counterfactual: a renderer that drops the window gate shows the figure.
    ungated = _render(page, mutate={
        'rowMetricHtml(row.out, "customers", channelOutcomeInt, ctx.oOpts)':
        'dashValue(row.out.customers, channelOutcomeInt)'})
    assert _cells(_tr(ungated, "Paid Search"))[0] == "3"


def test_25_view_blockers_is_admin_only_and_names_the_evidence_window(monkeypatch):
    out = _two_channel_company(monkeypatch)
    out["totals"]["metrics"]["customers"] = {"value": None, "status": "withheld",
                                             "reason": cwt.R_CUSTOMER_IDENTITY}
    page = _page(monkeypatch, out)
    admin = _render(page, role="admin")
    assert "#/health?cte_window=evidence%2F180d&amp;cte_family=customer_identity" in admin
    for role in (None, "viewer"):
        assert "View blockers" not in _render(page, role=role)


@pytest.mark.parametrize("which", ["funnel", "outcomes"])
def test_26_each_half_renders_when_the_other_is_withheld(monkeypatch, which):
    if which == "funnel":
        page = _page(monkeypatch, publication={"status": "withheld",
                                               "reason": "source_last_incremental_failed"})
        html = _render(page)
        assert "<strong>Withheld</strong>" in html and "the last contact sync failed" in html
        assert "2,951" not in html and 'data-channel-sort="sqls"' not in html
        assert _cells(_tr(html, "Paid Search")) == ["1", "$1.0k"]
        head = html[html.index("<thead>"):html.index("</thead>")]
        assert "New contacts" not in head and "Customers" in head
        assert "New contacts, SQLs and SQL rate are withheld" in _text(html)
    else:
        rows = [_deal("a")]
        _ledger(monkeypatch, rows, [_contact("a", 1, "PAID_SEARCH")],
                sync_state=fresh_sync_state(NOW, hours_ago=80))
        _spend(monkeypatch)
        page = _page(monkeypatch, _outcomes())
        html = _render(page)
        assert "2,951" in html and 'data-channel-sort="sqls"' in html
        head = html[html.index("<thead>"):html.index("</thead>")]
        assert "Customers" not in head and "Revenue" not in head
        group = _text(_group(html))
        assert "Customers won Unavailable the deal sync is overdue" in group
        assert "Customers and revenue are unavailable" in _text(html)


def test_27_the_page_names_both_cohorts_and_the_rule_and_no_attribution_ratio(monkeypatch):
    html = _render(_page(monkeypatch))
    text = _text(html)
    # The two groups, each with its own days and its own metadata.
    assert "Deals closed 25 May – 19 Nov" in _text(_group(html))
    assert "Contacts acquired 23 May – 18 Nov" in _text(_group(html, "Contacts acquired"))
    assert 'data-metric-family="closed_won_outcomes" data-window-basis="deal_close_date"' in html
    assert "Customers won" in text and "distinct companies" in text
    assert "Through 19 Nov 2026 — today is not included until tomorrow morning's sync." in text
    assert "so no rate joins the two" in text
    assert "a company can appear under two channels, so the rows don't add up" in text
    assert "recorded as Direct Traffic is counted under Direct Traffic" in text
    assert "Non-cohort operational ratio" in text
    assert "not on this page yet" not in text
    assert not re.search(r"\b(ROAS|CAC|CPQL)\b", text)


def test_28_the_unresolved_row_is_outcome_only_and_drill_rows_carry_no_figure(monkeypatch):
    _mixed_ledger(monkeypatch)
    _spend(monkeypatch)
    page = _page(monkeypatch, _outcomes())
    html = _render(page)
    un = _tr(html, "Deals with no single source")
    assert un.count("n/a") == 2                     # new contacts, SQLs
    assert _cells(un)[0] == "6"
    # Its drawer says why each deal has no single source; no funnel.
    from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app
    drawer = _run_app("channelDrawerHtml(%s, '__deal_source_unresolved__')" % json.dumps(page))
    assert "contacts have different sources: 1" in _text(drawer)
    assert "funnel-block" not in drawer
    # A source drill-down in a drawer carries a contact count, never an outcome.
    for k in ("PAID_SEARCH", "PAID_SOCIAL"):
        d = _text(_run_app("channelDrawerHtml(%s, %s)" % (json.dumps(page), json.dumps(k))))
        assert "Top sources" in d
        assert "$" not in d[d.index("Top sources"):]


def test_29_the_reader_inventory_and_the_evidence_pass_include_channel_outcomes():
    from scripts import audit_customer_revenue_reader_cutover as audit
    row = next(r for r in audit.INVENTORY
               if r["consumer"] == "services/source_attribution_service.py")
    assert "/api/channel-funnel" in row["surface"]
    assert dict(audit._evidence_page_builders())["channel_outcomes"]
    assert audit.EVIDENCE_PASS_WINDOWS == ("30d", "60d", "180d", "all_time")
    from services import cross_page_parity_service as parity
    decl = parity.EVIDENCE_WINDOW_CONSUMERS["channel_outcomes"]
    assert decl["window_type"] == "evidence"
    assert "channel_outcomes" not in parity.CERTIFIED_CONSUMERS


def test_30_the_feasibility_report_counts_won_deals_per_placement_class(monkeypatch):
    import services.canonical_customer_revenue_service as ccr
    import services.canonical_revenue_service as crs
    from scripts import report_attribution_feasibility as rep
    _mixed_ledger(monkeypatch)
    universe = crs.load_closed_won_universe(cwt.CONFIRMED_WON_STAGE_ID)
    truth = ccr.get_closed_won_truth(list(rep.CHANNEL_WINDOWS), now=NOW,
                                     universe=universe)
    by = {c["window"]: c for c in rep.channel_placement(universe, truth)}
    assert set(by) == {"evidence/all_time", "evidence/180d", "evidence/60d",
                       "evidence/30d"}
    a = by["evidence/180d"]
    assert (a["members"], a["resolved"]) == (9, 3)
    assert a["by_channel"] == {"DIRECT_TRAFFIC": 1, "PAID_SEARCH": 2}
    assert sum(a["unresolved"].values()) == 6
    assert by["evidence/30d"]["members"] == 7      # ps2 (1 Oct) and dt1 are older


# ═════════════════════════════════════════════════════════════════════════════
# Review round 2
# ═════════════════════════════════════════════════════════════════════════════
def _ps_deal(did, rev, label=None, gclid=None):
    """A Paid Search deal as production's deal sync writes it: the contact's
    hs_analytics_source classified by the real ``classify_source``."""
    from analysis.source_classification import classify_source
    return _deal(did, revenue_usd=rev, amount_raw=rev, campaign_name_raw=label,
                 gclid=gclid, source_primary_raw="PAID_SEARCH",
                 acquisition_group=classify_source("PAID_SEARCH", None),
                 attribution_status="attributed", attribution_reason="single_contact")


def _bing_ledger(monkeypatch, *, resolver=None):
    from tests.canonical_ledger_fixtures import label_resolver
    rows = [_ps_deal("g1", 10000.0, "brand", "abc"),
            _ps_deal("b2", 50000.0, "bing-unmapped")]
    patch_canonical_ledger(
        monkeypatch, rows, sync_state=fresh_sync_state(NOW),
        acquisition_contacts=[_contact("g1", 1, "PAID_SEARCH"),
                              _contact("b2", 2, "PAID_SEARCH")],
        company_associations=company_evidence(rows, scan_started_at=NOW.isoformat()),
        resolver=resolver or label_resolver(unmapped=("bing-unmapped",)))
    _spend(monkeypatch, usd=4000.0)


def test_31_the_line_is_the_platforms_google_ads_evidence_and_shows_its_split(monkeypatch):
    """The documented population: a Paid Search deal from another engine with
    no GCLID and no approved not-Google-Ads label IS Google Ads evidence by
    the platform's doctrine. The line says so and shows the split."""
    _bing_ledger(monkeypatch)
    out = _outcomes("30d")
    g = out["google_ads_line"]
    assert (g["closed_won_deals"], g["won_revenue"]) == (2, 60000.0)
    assert g["period_revenue_per_ad_spend"] == 15.0
    sp = g["evidence_split"]
    assert sp["with_gclid"]["closed_won_deals"]["value"] == 1
    assert sp["with_gclid"]["revenue_usd"]["value"] == 10000.0
    assert sp["source_classification_only"]["closed_won_deals"]["value"] == 1
    assert sp["source_classification_only"]["revenue_usd"]["value"] == 50000.0
    assert sum(p["closed_won_deals"]["value"] for p in sp.values()) == g["closed_won_deals"]
    html = _render(_page(monkeypatch, json.loads(json.dumps(out))))
    assert "2 deals: 1 with a Google click ID, 1 by HubSpot source only" in _text(
        _card(html, "google"))
    assert "does not by itself exclude other search engines" in out["notes"]["spend"]


def test_32_the_lines_reconcile_guard_withholds_a_line_that_does_not_match_the_truth(monkeypatch):
    import services.canonical_revenue_service as crs
    _bing_ledger(monkeypatch)
    real = crs.load_won_deals

    def tampered(*a, **k):
        b = real(*a, **k)
        if b.get("available"):
            b["attribution_partition"] = {**b["attribution_partition"],
                                          "campaign_attributable": 5}
        return b

    monkeypatch.setattr(crs, "load_won_deals", tampered)
    g = _outcomes("30d")["google_ads_line"]
    assert g["evidence"] == {"status": "unavailable",
                             "reason": "google_ads_evidence_does_not_reconcile"}
    assert g["won_revenue"] is None and g["closed_won_deals"] is None
    assert g["metrics"]["revenue_usd"]["reason"] == "google_ads_evidence_does_not_reconcile"
    for k in cwt.PERIOD_RATIO_KEYS:
        assert g[k] is None and g["metrics"][k]["status"] != "published"
    assert g["evidence_split"] is None
    # Counterfactual: with the guard removed, the tampered line publishes.
    monkeypatch.setattr(crs, "_evidence_counts_match", lambda counts, part: True)
    g2 = _outcomes("30d")["google_ads_line"]
    assert g2["evidence"]["status"] == "published" and g2["won_revenue"] == 60000.0


def test_33_a_gated_spend_does_not_leave_a_verified_state_beside_it(monkeypatch):
    _bing_ledger(monkeypatch)
    _spend(monkeypatch, usd=4000.0, zone="Asia/Amman")
    out = _outcomes("30d")
    sp = out["spend"]
    assert sp["google_ads"]["status"] == "unavailable"
    assert sp["google_ads_state"] == "unavailable" and sp["native_spend"] is None
    assert out["google_ads_line"]["spend"]["status"] == "unavailable"
    html = _render(_page(monkeypatch, json.loads(json.dumps(out))))
    card = _text(_card(html, "google"))
    assert "Spend Unavailable the Google Ads account time zone differs" in card
