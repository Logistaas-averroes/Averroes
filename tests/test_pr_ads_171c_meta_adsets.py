"""PR-ADS-171C — Meta ad-set daily spend and Meta-reported lead actions.

Every case drives the real connector, the real ad-set service and the real
sync steps against the fake Graph API of PR-ADS-171A
(tests/meta_ads_fakes.py), extended to ad sets: two accounts under two
tokens, paging whose ``paging.next`` embeds the token, the CURRENT name on
every row, and ``actions`` only where a row has some.

The database is replaced by recorders here; the campaign base the ad-set
sync reconciles against is whatever the real campaign step handed its writer.
The database-backed cases are in
test_pr_ads_171c_meta_adsets_pg_integration.py.
"""
from __future__ import annotations

import inspect
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import connectors.meta_ads as connector
import db.writers as db_writers
import scheduler.incremental_sync as sync
from api import monitoring
from services import dataset_keys
from services import meta_ads_adset_service as adsets
from services import meta_ads_adset_truth_service as adset_truth
from services import meta_ads_spend_service as meta
from services.freshness_service import (
    DATASET_FRESHNESS_CONFIG, CanonicalFreshnessStatus,
)
from tests import meta_ads_fakes as fakes
from tests.meta_ads_fakes import (
    ACCOUNT_ID, ADSET_A1, ADSET_A2, ADSET_ARCHIVED, ADSET_B1, CAMPAIGN_A,
    CAMPAIGN_B, NO_ACTIONS, OLD_ACCOUNT_ID, SENTINEL_TOKEN, SENTINEL_TOKEN_2,
    FakeGraph, actions, error_response,
)

ROOT = Path(__file__).resolve().parent.parent
# The real writers, before any fixture replaces them with a recorder.
REAL_ADSET_WRITER = db_writers.replace_meta_adset_chunk
REAL_ADSET_FAILURE = db_writers.record_meta_adset_coverage_failure
# 06:00 UTC, the daily sync. Los Angeles (the new account): the last complete
# day is the 16th. Amman (the old account, with the ads): the 17th.
NOW = datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)
NEW, OLD = ACCOUNT_ID, OLD_ACCOUNT_ID
OLD_YESTERDAY = date(2026, 11, 17)
OLD_WINDOW_START = date(2026, 10, 21)

LEAD_GEN = "Lead Generation | Look Alike | 2026"
AWARENESS = "Awareness Campaign | 2026"
#: What a native lead-form ad returns: one submission counted under several
#: action types at once. Three leads, not nine.
THREE_LEADS = dict(lead=3, onsite_conversion__lead_grouped=3,
                   leadgen_grouped=3, link_click=40, post_engagement=55)


def _pair() -> FakeGraph:
    """Production: the new account has no delivery; the old one has two
    campaigns — lead generation with two ad sets, awareness with one."""
    graph = FakeGraph()
    old = graph.add_account(OLD)
    old.adset(ADSET_A1, CAMPAIGN_A, "2026-11-15", "40.00", name="Look Alike — GCC",
              campaign_name=LEAD_GEN, actions=actions(**THREE_LEADS))
    # `actions` present, no lead type: Meta reported actions, none a lead.
    old.adset(ADSET_A2, CAMPAIGN_A, "2026-11-15", "30.00",
              name="Look Alike — Levant", actions=actions(link_click=12))
    # No `actions` key at all: unknown.
    old.adset(ADSET_A1, CAMPAIGN_A, "2026-11-16", "35.34", actions=NO_ACTIONS)
    old.adset(ADSET_A2, CAMPAIGN_A, "2026-11-16", "25.00",
              actions=actions(lead=1, onsite_conversion__lead_grouped=1))
    # A day the ad set delivered and spent nothing.
    old.adset(ADSET_A2, CAMPAIGN_A, "2026-11-17", "0", impressions=40, clicks=0,
              actions=[])
    old.adset(ADSET_B1, CAMPAIGN_B, "2026-11-17", "12.00", name="Awareness — MENA",
              campaign_name=AWARENESS, actions=actions(video_view=900))
    old.adset(ADSET_A1, CAMPAIGN_A, "2026-11-18", "9.99")          # in progress
    return graph


@pytest.fixture()
def store(monkeypatch):
    """The database, replaced by recorders. The campaign base the ad-set sync
    reads is derived from what the campaign step really wrote."""
    s = SimpleNamespace(batches=[], finished=[], replaced=[], failures=[],
                        accounts={}, synced=[], adset_replaced=[],
                        adset_failures=[], adset_replace_error=None,
                        base_available=True)

    def start(**kw):
        s.batches.append(kw)
        return len(s.batches)

    def finish(**kw):
        s.finished.append(kw)
        return True

    def upsert(account):
        s.accounts[account["account_id"]] = dict(account)
        return {**account, "identity_changed": {}}

    def replace(account_id, start, end, rows, account_days, **kw):
        s.replaced.append({"account_id": account_id, "start": start, "end": end,
                           "rows": list(rows), **kw})
        return {"replaced": True, "deleted": 0, "written": len(rows),
                "account_days_written": len(account_days),
                "cost_micros_total": sum(r["cost_micros"] for r in rows),
                "days_restated": 0, "max_restated_delta_micros": 0,
                "names_preserved": 0}

    def failure(account_id, start, end, **kw):
        s.failures.append({"account_id": account_id, "start": start, "end": end, **kw})
        return True

    def synced(account_id, status, **kw):
        s.synced.append({"account_id": account_id, "status": status, **kw})
        return True

    def adset_replace(account_id, start, end, rows, **kw):
        if s.adset_replace_error:
            raise s.adset_replace_error
        s.adset_replaced.append({"account_id": account_id, "start": start,
                                 "end": end, "rows": list(rows), **kw})
        return {"replaced": True, "deleted": 0, "written": len(rows),
                "days_zeroed": 0,
                "cost_micros_total": sum(r["cost_micros"] for r in rows),
                "days_restated": 0, "max_restated_delta_micros": 0,
                "names_preserved": 0,
                "rows_without_actions": sum(1 for r in rows
                                            if r["meta_reported_lead_actions"] is None)}

    def adset_failure(account_id, start, end, **kw):
        s.adset_failures.append({"account_id": account_id, "start": str(start),
                                 "end": str(end), **kw})
        return True

    def campaign_totals(account_id, start, end):
        if not s.base_available:
            return {"available": False, "rows": [], "chunks": []}
        writes = [w for w in s.replaced if w["account_id"] == account_id]
        totals: dict = {}
        for w in writes[-1:]:
            for r in w["rows"]:
                if str(start) <= r["spend_date"] <= str(end):
                    key = (r["spend_date"], r["campaign_id"])
                    totals[key] = totals.get(key, 0) + r["cost_micros"]
        return {"available": True,
                "rows": [{"spend_date": d, "campaign_id": c, "cost_micros": m}
                         for (d, c), m in sorted(totals.items())],
                "chunks": [{"chunk_start": w["start"].isoformat(),
                            "chunk_end": w["end"].isoformat(),
                            "status": "verified"} for w in writes]}

    monkeypatch.setattr(db_writers, "start_sync_batch", start)
    monkeypatch.setattr(db_writers, "finish_sync_batch", finish)
    monkeypatch.setattr(db_writers, "upsert_meta_ads_account", upsert)
    monkeypatch.setattr(db_writers, "replace_meta_campaign_spend_chunk", replace)
    monkeypatch.setattr(db_writers, "record_meta_spend_coverage_failure", failure)
    monkeypatch.setattr(db_writers, "record_meta_account_sync", synced)
    monkeypatch.setattr(db_writers, "replace_meta_adset_chunk", adset_replace)
    monkeypatch.setattr(db_writers, "record_meta_adset_coverage_failure",
                        adset_failure)
    from db import meta_ads_repository as repo
    from services import fx_service
    monkeypatch.setattr(repo, "fetch_meta_account", lambda account_id: {
        "available": True, "account": s.accounts.get(account_id)})
    monkeypatch.setattr(repo, "fetch_meta_fx_missing_dates",
                        lambda account_id, start, end: {"available": True,
                                                        "dates": []})
    monkeypatch.setattr(repo, "fetch_meta_campaign_day_totals", campaign_totals)
    monkeypatch.setattr(fx_service, "ensure_fx_rates",
                        lambda *a, **k: {"rows_written": 0, "fetched": 0,
                                         "failed": []})
    return s


def _run(errors=None):
    """The two Meta steps, in the order the daily run calls them."""
    errors = [] if errors is None else errors
    campaign = sync._sync_meta_ads_spend(run_id=7, now=NOW, errors=errors)
    detail = sync._sync_meta_ads_adsets(run_id=7, campaign_result=campaign,
                                        errors=errors)
    return campaign, detail, errors


def _by_account(result, account_id):
    return next(a for a in result["accounts"] if a["account_id"] == account_id)


def _batch(store, dataset):
    index = next(i for i, b in enumerate(store.batches) if b["dataset"] == dataset)
    return store.batches[index], next(f for f in store.finished
                                      if f["batch_id"] == index + 1)


def _adset_rows(store, account_id=OLD):
    return {(r["adset_id"], r["spend_date"]): r
            for w in store.adset_replaced if w["account_id"] == account_id
            for r in w["rows"]}


# ── the request ───────────────────────────────────────────────────────────

def test_01_the_adset_read_is_one_get_per_page_with_the_token_in_the_header(
        monkeypatch):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    rows = [r for page in connector.iter_adset_daily(OLD, "2026-11-15", "2026-11-17")
            for r in page]
    assert len(rows) == 6
    calls = graph.insight_calls("adset", OLD)
    assert len(calls) == 1
    params = calls[0]["params"]
    assert params["level"] == "adset" and params["time_increment"] == 1
    assert json.loads(params["time_range"]) == {"since": "2026-11-15",
                                                "until": "2026-11-17"}
    assert params["fields"].split(",") == [
        "adset_id", "adset_name", "campaign_id", "campaign_name", "spend",
        "impressions", "clicks", "actions", "attribution_setting",
        "account_currency", "date_start", "date_stop"]
    # Counted under each ad set's own attribution setting.
    assert params["use_unified_attribution_setting"] == "true"
    assert calls[0]["headers"]["Authorization"] == f"Bearer {SENTINEL_TOKEN_2}"
    assert "access_token" not in params
    # The old account's token read the old account; the new one's read nothing.
    assert all(c["account_id"] == OLD for c in graph.calls)


def test_02_the_adset_read_issues_get_and_nothing_else():
    source = (ROOT / "connectors/meta_ads.py").read_text()
    assert set(re.findall(r"requests\.(\w+)\(", source)) == {"get"}
    with pytest.raises(connector.MetaAdsError, match="read-only"):
        connector._request(OLD, "insights", {"level": "adset"}, method="POST")
    # The ad-set service has no HTTP of its own and no asynchronous report.
    for path in ("services/meta_ads_adset_service.py",
                 "services/meta_ads_adset_truth_service.py"):
        text = (ROOT / path).read_text()
        assert "requests" not in text and "async" not in text


def test_03_paging_that_moves_is_read_to_the_end_and_next_is_never_followed(
        monkeypatch):
    graph = FakeGraph()
    old = graph.add_account(OLD)
    for index in range(23):
        day = (date(2026, 10, 21) + timedelta(days=index)).isoformat()
        old.adset(ADSET_A1, CAMPAIGN_A, day, "10.00")
        old.adset(ADSET_A2, CAMPAIGN_A, day, "5.50")
    graph.max_limit = 7
    fakes.configure(monkeypatch, graph)
    stats: dict = {}
    pages = list(connector.iter_adset_daily(OLD, "2026-10-21", "2026-11-17", stats))
    assert sum(len(p) for p in pages) == 46 and stats["pages_read"] == 7
    calls = graph.insight_calls("adset", OLD)
    assert [c["params"].get("after") for c in calls] == [
        None, "7", "14", "21", "28", "35", "42"]
    # Six pages promised another, each with a URL that carries the token …
    assert len(graph.next_urls) == 6
    assert all(SENTINEL_TOKEN_2 in url for url in graph.next_urls)
    # … and none of those URLs was ever requested.
    assert not {c["url"] for c in graph.calls} & set(graph.next_urls)
    assert len({(r["adset_id"], r["spend_date"]) for p in pages for r in p}) == 46


def test_04_an_adset_day_returned_twice_or_without_an_id_fails_the_read(
        monkeypatch):
    graph = _pair()
    graph.max_limit = 2
    graph.repeat_first_row = True
    fakes.configure(monkeypatch, graph)
    with pytest.raises(connector.MetaAdsMalformedResponse, match="returned twice"):
        list(connector.iter_adset_daily(OLD, "2026-11-15", "2026-11-17"))
    for missing in ("adset_id", "campaign_id"):
        graph = _pair()
        fakes.configure(monkeypatch, graph)
        real = graph._insights

        def stripped(account, params, real=real, missing=missing):
            response = real(account, params)
            for row in response._body["data"]:
                row.pop(missing, None)
            return response
        monkeypatch.setattr(graph, "_insights", stripped)
        with pytest.raises(connector.MetaAdsMalformedResponse, match="no adset_id"):
            list(connector.iter_adset_daily(OLD, "2026-11-15", "2026-11-17"))


def test_05_spend_is_exact_micros_and_ids_are_the_identity(monkeypatch):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    rows = {(r["adset_id"], r["spend_date"]): r
            for page in connector.iter_adset_daily(OLD, "2026-11-15", "2026-11-17")
            for r in page}
    row = rows[(ADSET_A1, "2026-11-16")]
    assert row["cost_micros"] == 35_340_000 and row["spend_raw"] == "35.34"
    assert row["campaign_id"] == CAMPAIGN_A and row["account_id"] == OLD
    assert row["currency_code"] == "USD"
    assert row["impressions"] == 1000 and row["clicks"] == 25
    assert row["attribution_setting"] == "7d_click_1d_view"
    zero = rows[(ADSET_A2, "2026-11-17")]
    assert zero["cost_micros"] == 0 and zero["impressions"] == 40
    assert zero["clicks"] == 0


# ── leads: Meta's own counts, per action type ─────────────────────────────

def test_06_a_missing_actions_key_is_unknown_and_an_empty_one_is_not(monkeypatch):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    rows = {(r["adset_id"], r["spend_date"]): r
            for page in connector.iter_adset_daily(OLD, "2026-11-15", "2026-11-17")
            for r in page}
    # No `actions` key: Meta said nothing. Unknown — not {} and not 0.
    assert rows[(ADSET_A1, "2026-11-16")]["meta_reported_lead_actions"] is None
    # `actions` present with no lead type: Meta reported none.
    assert rows[(ADSET_A2, "2026-11-15")]["meta_reported_lead_actions"] == {}
    # `actions` present and empty.
    assert rows[(ADSET_A2, "2026-11-17")]["meta_reported_lead_actions"] == {}
    assert rows[(ADSET_B1, "2026-11-17")]["meta_reported_lead_actions"] == {}
    assert connector.meta_reported_lead_actions({"spend": "1.00"}) is None
    assert connector.meta_reported_lead_actions({"actions": []}) == {}


def test_07_overlapping_lead_types_are_kept_apart_and_never_added_up(
        monkeypatch, store):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    _, detail, _ = _run()
    row = _adset_rows(store)[(ADSET_A1, "2026-11-15")]
    # One native-form submission is counted under each of these types. The
    # row holds each type's own count — and no figure that adds them.
    assert row["meta_reported_lead_actions"] == {"lead": 3, "leadgen_grouped": 3,
                                   "onsite_conversion.lead_grouped": 3}
    assert "link_click" not in row["meta_reported_lead_actions"]
    totals = _by_account(detail, OLD)["meta_reported_lead_action_counts"]
    assert totals == {"lead": 4, "leadgen_grouped": 3,
                      "onsite_conversion.lead_grouped": 4}
    aggregated = {"rows_without_actions": 0, "meta_reported_lead_action_counts": totals}
    for lead_type in (None, "lead", "onsite_conversion.lead_grouped"):
        figures = adset_truth.lead_metrics(aggregated, lead_type)
        dumped = json.dumps(figures)
        # 9 = 3+3+3 on the 15th; 11 = 4+3+4 over the range.
        assert '": 9' not in dumped and '": 11' not in dumped
        assert figures["meta_reported_lead_actions"]["overlapping_types"] is True
    assert adset_truth.lead_metrics(aggregated, "lead")[
        "meta_reported_leads"]["value"] == 4
    # Nothing in the ad-set code path adds lead counts across types.
    for path in ("services/meta_ads_adset_service.py",
                 "services/meta_ads_adset_truth_service.py",
                 "db/meta_ads_repository.py"):
        text = (ROOT / path).read_text()
        assert not re.search(r"sum\([^)]*lead_action[^)]*\.values\(\)", text)
        assert not re.search(r"sum\(\s*(counts|totals|leads)\.values\(\)", text)


def test_08_no_lead_type_is_published_until_one_is_confirmed(monkeypatch):
    # The shipped configuration names no action type.
    assert adsets.native_lead_action_type() is None
    row = {"rows_without_actions": 0,
           "meta_reported_lead_action_counts": {"lead": 4, "onsite_conversion.lead_grouped": 4}}
    figures = adset_truth.lead_metrics(row, adsets.native_lead_action_type())
    assert figures["meta_reported_leads"] == {
        "value": None, "status": "unavailable",
        "reason": "meta_lead_action_type_unverified", "action_type": None}
    # The raw types Meta returned are still there, per type.
    assert figures["meta_reported_lead_actions"]["value"] == row["meta_reported_lead_action_counts"]
    # Once a type is configured: that type's count, and only that type's.
    monkeypatch.setattr(meta, "_config", lambda: {
        "meta_ads": {"native_lead_action_type": "onsite_conversion.lead_grouped"}})
    assert adsets.native_lead_action_type() == "onsite_conversion.lead_grouped"
    leads = adset_truth.lead_metrics(row, adsets.native_lead_action_type())[
        "meta_reported_leads"]
    assert leads["value"] == 4 and leads["status"] == "published"
    # `actions` on every day, the type on none: Meta reported none. Zero.
    none = adset_truth.lead_metrics({"rows_without_actions": 0,
                                     "meta_reported_lead_action_counts": {}}, "lead")
    assert none["meta_reported_leads"]["value"] == 0
    # A day without `actions`: unknown — never zero, never a partial count.
    partial = adset_truth.lead_metrics(
        {"rows_without_actions": 1, "meta_reported_lead_action_counts": {"lead": 4}}, "lead")
    assert partial["meta_reported_leads"] == {
        "value": None, "status": "unavailable",
        "reason": "meta_actions_missing_on_some_days", "action_type": "lead"}
    assert partial["meta_reported_lead_actions"]["value"] is None
    assert partial["meta_reported_lead_actions"]["rows_without_actions"] == 1


@pytest.mark.parametrize("broken", [
    {"actions": "lead"},
    {"actions": None},
    {"actions": [{"action_type": "lead", "value": "three"}]},
    {"actions": [{"action_type": "lead", "value": "2.5"}]},
    {"actions": [{"action_type": "lead"}]},
    {"actions": [{"action_type": "lead", "value": "-1"}]},
    {"actions": [{"value": "3"}]},
    {"actions": ["lead"]},
    {"actions": [{"action_type": "lead", "value": "1"},
                 {"action_type": "lead", "value": "2"}]},
])
def test_09_an_unreadable_action_count_fails_the_read_and_is_never_a_zero(broken):
    with pytest.raises(connector.MetaAdsMalformedResponse):
        connector.meta_reported_lead_actions(broken)


def test_10_an_unreadable_actions_list_fails_the_chunk_and_stores_nothing(
        monkeypatch, store):
    graph = _pair()
    graph.accounts[OLD].adset_rows[(ADSET_A2, "2026-11-16")]["actions"] = [
        {"action_type": "lead", "value": "many"}]
    fakes.configure(monkeypatch, graph)
    campaign, detail, _ = _run()
    old = _by_account(detail, OLD)
    assert old["status"] == "failed" and old["reason"] == "meta_malformed_response"
    assert not [w for w in store.adset_replaced if w["account_id"] == OLD]
    assert campaign["status"] == "success"


def test_11_the_counts_are_named_meta_reported_and_never_a_crm_stage():
    # Every key of a published row that speaks of leads says whose count it is.
    figures = adset_truth.lead_metrics({"rows_without_actions": 0,
                                        "meta_reported_lead_action_counts": {"lead": 1}}, "lead")
    assert set(figures) == {"meta_reported_leads", "meta_reported_lead_actions"}
    forbidden = re.compile(r"\b(mqls?|sqls?|contacts?|hubspot_leads?|crm_leads?"
                           r"|leads_in_crm)\b", re.I)
    for path in ("services/meta_ads_adset_truth_service.py",
                 "services/meta_ads_adset_service.py"):
        tree_keys = re.findall(r'"([a-z_]+)"\s*:', (ROOT / path).read_text())
        for key in tree_keys:
            assert not forbidden.search(key.replace("_", " ")), key
            if "lead" in key:
                assert key.startswith("meta_reported_") or key in (
                    "native_lead_action_type",), key
    # The ad-set modules read no HubSpot table and import no CRM service.
    for path in ("services/meta_ads_adset_truth_service.py",
                 "services/meta_ads_adset_service.py"):
        text = (ROOT / path).read_text()
        imports = re.findall(r"^\s*(?:from|import)\s+([\w.]+)", text, re.M)
        assert not [m for m in imports if re.search(
            r"hubspot|crm|cohort|lifecycle|contact|revenue", m)], imports


# ── the daily step ────────────────────────────────────────────────────────

def test_12_both_levels_sync_for_each_accounts_own_window(monkeypatch, store):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    campaign, detail, errors = _run()
    assert campaign["status"] == "success" and detail["status"] == "success"
    assert errors == []
    assert sync._overall_status({"c": campaign, "a": detail}) == "success"
    old = _by_account(detail, OLD)
    assert (old["chunk_start"], old["chunk_end"]) == ("2026-10-21", "2026-11-17")
    # The new account, a day behind in Los Angeles, with no delivery: a
    # verified nothing at both levels.
    new = _by_account(detail, NEW)
    assert (new["chunk_start"], new["chunk_end"]) == ("2026-10-20", "2026-11-16")
    assert new["status"] == "success" and new["verified_zero"] is True
    # Six ad-set-day rows; the day in progress in Amman was never asked for.
    rows = _adset_rows(store)
    assert len(rows) == 6 and (ADSET_A1, "2026-11-18") not in rows
    assert old["adsets"] == 3 and old["rows_without_actions"] == 1
    # Each account was read with its own token only (the fake enforces it),
    # one ad-set query each.
    assert len(graph.insight_calls("adset", OLD)) == 1
    assert len(graph.insight_calls("adset", NEW)) == 1
    # Two batches: the campaign spend's and the ad-set detail's.
    opened, closed = _batch(store, "meta_adset_spend")
    assert opened["source"] == "meta_ads_api" and opened["sync_type"] == "daily"
    assert (opened["date_from"], opened["date_to"]) == (date(2026, 10, 20),
                                                       OLD_YESTERDAY)
    assert closed["status"] == "success" and closed["row_count"] == 6
    # Proven through the EARLIER of the two accounts' last days.
    assert closed["last_source_date"] == "2026-11-16"
    assert detail["external_writes_performed"] is False


def test_13_adset_rows_that_do_not_sum_to_the_campaign_row_fail_and_store_nothing(
        monkeypatch, store):
    graph = _pair()
    # Meta leaves one ad set out of the ad-set read on the 16th; its 25.00 is
    # in the campaign row and missing from the ad-set rows.
    graph.accounts[OLD].adset_hidden.add((ADSET_A2, "2026-11-16"))
    fakes.configure(monkeypatch, graph)
    campaign, detail, errors = _run()

    old = _by_account(detail, OLD)
    assert old["status"] == "failed" and old["coverage_status"] == "failed"
    assert old["reason"] == "meta_adset_reconciliation_mismatch"
    assert "2026-11-16" in old["error"] and CAMPAIGN_A in old["error"]
    assert "35340000" in old["error"] and "60340000" in old["error"]
    # Nothing was stored for the chunk — not the days that did reconcile.
    assert not [w for w in store.adset_replaced if w["account_id"] == OLD]
    assert [(f["account_id"], f["start"], f["end"], f["error_code"])
            for f in store.adset_failures] == [
        (OLD, "2026-10-21", "2026-11-17", "meta_adset_reconciliation_mismatch")]
    assert any("meta_adset_spend" in e and OLD in e for e in errors)

    # The campaign level is exactly what it would have been without ad sets.
    assert campaign["status"] == "success" and campaign["errors"] == []
    assert store.failures == []
    assert [(s["account_id"], s["status"]) for s in store.synced] == [
        (NEW, "success"), (OLD, "success")]
    _, campaign_batch = _batch(store, "meta_campaign_spend")
    assert campaign_batch["status"] == "success"
    assert campaign_batch["last_source_date"] == "2026-11-16"
    # The other account's ad sets are verified: the step is partial …
    assert _by_account(detail, NEW)["status"] == "success"
    assert detail["status"] == "partial"
    _, closed = _batch(store, "meta_adset_spend")
    assert closed["status"] == "partial" and closed["last_source_date"] is None
    assert closed["error_message"].startswith("meta_adset_reconciliation_mismatch: ")
    # … and so is the run: never a success, never a failure.
    assert sync._overall_status({"c": campaign, "a": detail}) == "partial"


def test_14_an_adset_row_under_a_campaign_with_no_campaign_row_fails(
        monkeypatch, store):
    graph = _pair()
    # An ad-set row whose campaign has no campaign-level row that day.
    graph.accounts[OLD].adset(ADSET_B1, CAMPAIGN_B, "2026-11-16", "4.00",
                              roll_up=False)
    fakes.configure(monkeypatch, graph)
    campaign, detail, _ = _run()
    # The campaign level fails its OWN check (the account total does not hold
    # that spend) — and the ad sets are then not read at all.
    assert _by_account(campaign, OLD)["status"] == "success"
    old = _by_account(detail, OLD)
    assert old["reason"] == "meta_adset_reconciliation_mismatch"
    assert CAMPAIGN_B in old["error"] and "2026-11-16" in old["error"]
    assert not [w for w in store.adset_replaced if w["account_id"] == OLD]


@pytest.mark.parametrize("campaign_spend, reconciles", [
    ("65.34", True), ("65.35", True), ("65.33", True), ("65.36", False),
    ("65.32", False)])
def test_15_the_tolerance_is_one_minor_unit_per_campaign_per_day(
        campaign_spend, reconciles):
    rows = [{"spend_date": "2026-11-16", "campaign_id": CAMPAIGN_A,
             "cost_micros": 35_340_000},
            {"spend_date": "2026-11-16", "campaign_id": CAMPAIGN_A,
             "cost_micros": 30_000_000}]
    base = [{"spend_date": "2026-11-16", "campaign_id": CAMPAIGN_A,
             "cost_micros": connector.spend_to_micros(campaign_spend)}]
    verdict = adsets.reconcile_adsets_to_campaigns(
        rows, base, meta.reconciliation_tolerance_micros("USD"))
    assert verdict["reconciled"] is reconciles
    # Two campaigns whose errors cancel across the DAY still fail: the check
    # is per campaign, not per day.
    crossed = adsets.reconcile_adsets_to_campaigns(
        [{"spend_date": "2026-11-16", "campaign_id": CAMPAIGN_A, "cost_micros": 10_000_000},
         {"spend_date": "2026-11-16", "campaign_id": CAMPAIGN_B, "cost_micros": 20_000_000}],
        [{"spend_date": "2026-11-16", "campaign_id": CAMPAIGN_A, "cost_micros": 20_000_000},
         {"spend_date": "2026-11-16", "campaign_id": CAMPAIGN_B, "cost_micros": 10_000_000}],
        10_000)
    assert crossed["reconciled"] is False and len(crossed["mismatches"]) == 2


def test_16_some_accounts_verified_and_some_not_is_partial_never_collapsed(
        monkeypatch, store):
    graph = _pair()
    # Meta refuses the ad-set read of the old account only.
    graph.accounts[OLD].adset_refusal = error_response(
        403, 200, message="(#200) Ad set insights not permitted.")
    fakes.configure(monkeypatch, graph)
    campaign, detail, errors = _run()
    assert campaign["status"] == "success"
    assert [a["status"] for a in detail["accounts"]] == ["success", "failed"]
    assert detail["status"] == "partial"
    _, closed = _batch(store, "meta_adset_spend")
    assert closed["status"] == "partial"
    # All accounts failing is failed; all verified is success. Three outcomes.
    assert adsets.aggregate([{"status": "failed", "account_id": "1"}])["status"] == "failed"
    assert adsets.aggregate([{"status": "success", "account_id": "1",
                              "coverage_status": "verified",
                              "chunk_end": "2026-11-16"}])["status"] == "success"
    assert adsets.aggregate([])["status"] == "failed"
    # The refusal is an AD-SET failure: the account is not marked unreadable
    # and the campaign ledger records no failure.
    assert store.failures == []
    assert all(s["status"] == "success" and s.get("readable") is True
               for s in store.synced)
    assert len(store.adset_failures) == 1
    assert store.adset_failures[0]["error_code"] == "meta_account_not_readable"


def test_17_every_account_failing_at_adset_level_is_a_partial_run(
        monkeypatch, store):
    graph = _pair()
    for account in graph.accounts.values():
        account.adset_refusal = error_response(500, 1, message="An unknown error")
    sleeps = fakes.configure(monkeypatch, graph)
    campaign, detail, _ = _run()
    assert campaign["status"] == "success" and detail["status"] == "failed"
    assert _by_account(detail, OLD)["reason"] == "meta_retries_exhausted"
    assert len(sleeps) == 6                     # 3 waits per account, bounded
    _, closed = _batch(store, "meta_adset_spend")
    assert closed["status"] == "failed"
    # Campaign spend verified, ad-set detail not: the run is partial.
    assert sync._overall_status({"c": campaign, "a": detail}) == "partial"
    _, campaign_batch = _batch(store, "meta_campaign_spend")
    assert campaign_batch["status"] == "success"


def test_18_an_account_whose_campaign_level_failed_has_its_adsets_left_unread(
        monkeypatch, store):
    graph = _pair()
    # The campaign-level read of the old account does not reconcile.
    graph.accounts[OLD].hidden.add((CAMPAIGN_A, "2026-11-16"))
    fakes.configure(monkeypatch, graph)
    campaign, detail, _ = _run()
    assert _by_account(campaign, OLD)["status"] == "failed"
    old = _by_account(detail, OLD)
    assert old["status"] == "failed"
    assert old["reason"] == "meta_adset_campaign_base_unverified"
    assert "meta_reconciliation_mismatch" in old["error"]
    # No ad-set request was made for it, and nothing was written for it.
    assert graph.insight_calls("adset", OLD) == []
    assert not [w for w in store.adset_replaced if w["account_id"] == OLD]
    assert store.adset_failures[0]["error_code"] == "meta_adset_campaign_base_unverified"
    assert _by_account(detail, NEW)["status"] == "success"
    assert detail["status"] == "partial"


def test_19_a_range_whose_campaign_level_is_not_verified_has_no_base(
        monkeypatch, store):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    _run()
    account = {"account_id": OLD, "currency_code": "USD",
               "time_zone_name": "Asia/Amman"}
    # September was never read at campaign level.
    out = adsets.sync_adset_chunk(account, date(2026, 9, 1), date(2026, 9, 30))
    assert out["status"] == "failed"
    assert out["reason"] == "meta_adset_campaign_base_unverified"
    # A range that starts one day before the verified campaign chunk, too.
    out = adsets.sync_adset_chunk(account, date(2026, 10, 20), date(2026, 11, 17))
    assert out["reason"] == "meta_adset_campaign_base_unverified"
    # The stored campaign rows unreadable is not "no spend".
    store.base_available = False
    out = adsets.sync_adset_chunk(account, date(2026, 10, 21), date(2026, 11, 17))
    assert out["status"] == "failed" and out["reason"] == "meta_persistence_error"
    assert len([w for w in store.adset_replaced if w["account_id"] == OLD]) == 1


def test_20_meta_not_configured_skips_and_a_broken_configuration_fails(
        monkeypatch, store):
    fakes.unconfigure(monkeypatch)
    campaign, detail, errors = _run()
    assert campaign["status"] == "skipped" and detail["status"] == "skipped"
    assert detail["reason"] == "not_configured"
    assert store.batches == [] and errors == []
    assert detail["status"] in sync.NON_VOTING_STATUSES
    # Half a configuration: nothing syncs at either level, and no batch opens.
    monkeypatch.setenv("META_ADS_ACCESS_TOKEN", SENTINEL_TOKEN)
    campaign, detail, errors = _run()
    assert campaign["status"] == "failed" and detail["status"] == "failed"
    assert detail["reason"] == "meta_adset_campaign_base_unverified"
    assert store.batches == []
    assert SENTINEL_TOKEN not in json.dumps([campaign, detail, errors])


def test_21_the_adset_batch_that_cannot_open_fails_before_any_adset_request(
        monkeypatch, store):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    real = db_writers.start_sync_batch
    monkeypatch.setattr(db_writers, "start_sync_batch",
                        lambda **kw: 0 if kw["dataset"] == "meta_adset_spend"
                        else real(**kw))
    campaign, detail, _ = _run()
    assert campaign["status"] == "success"
    assert detail["status"] == "failed" and detail["reason"] == "sync_batch_not_opened"
    assert graph.insight_calls("adset") == []
    assert store.adset_replaced == []


def test_22_a_persistence_failure_fails_the_adset_chunk_only(monkeypatch, store):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    store.adset_replace_error = db_writers.MetaAdsPersistenceError("deadlock")
    campaign, detail, _ = _run()
    assert campaign["status"] == "success"
    assert detail["status"] == "failed"
    assert _by_account(detail, OLD)["reason"] == "meta_persistence_error"
    store.adset_replace_error = db_writers.MetaRestatementImplausible("1 day(s)")
    _, detail, _ = _run()
    assert _by_account(detail, OLD)["reason"] == "meta_restatement_implausible"
    assert store.failures == []


def test_23_no_token_reaches_a_result_a_log_or_a_stored_value(
        monkeypatch, store, caplog):
    caplog.set_level(logging.DEBUG)
    graph = _pair()
    graph.accounts[OLD].adset_refusal = error_response(
        400, 100, message=f"Invalid parameter near access_token={SENTINEL_TOKEN_2}"
                          f" and {SENTINEL_TOKEN}")
    fakes.configure(monkeypatch, graph)
    campaign, detail, errors = _run()
    assert _by_account(detail, OLD)["status"] == "failed"
    # A dropped connection on the ad-set read: `requests` puts the URL in it.
    graph.accounts[OLD].adset_refusal = None
    for _ in range(4):
        graph.queue.append(fakes.transport_error(SENTINEL_TOKEN_2))
    second = sync._sync_meta_ads_adsets(
        run_id=7, errors=errors,
        campaign_result={"status": "success", "accounts": [
            a for a in campaign["accounts"] if a["account_id"] == OLD]})
    assert _by_account(second, OLD)["reason"] == "meta_retries_exhausted"
    everything = json.dumps([campaign, detail, second, errors, store.batches,
                             store.finished, store.adset_failures,
                             store.adset_replaced, store.synced],
                            default=str) + caplog.text
    for token in (SENTINEL_TOKEN, SENTINEL_TOKEN_2):
        assert token not in everything
    assert "access_token=[redacted]" in everything


# ── names are labels ──────────────────────────────────────────────────────

def test_24_a_renamed_adset_keeps_its_id_and_nothing_is_keyed_by_a_name(
        monkeypatch, store):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    _run()
    before = _adset_rows(store)
    assert before[(ADSET_A1, "2026-11-15")]["adset_name"] == "Look Alike — GCC"
    # Renamed in Meta. Insights now returns the NEW name on every row — the
    # days before the rename included.
    graph.accounts[OLD].rename_adset(ADSET_A1, "LAL 1% — Gulf")
    graph.accounts[OLD].rename_campaign(CAMPAIGN_A, "Lead Gen | LAL | 2026")
    store.adset_replaced.clear()
    campaign, detail, _ = _run()
    assert detail["status"] == "success"
    after = _adset_rows(store)
    assert set(after) == set(before)                       # the same ids
    row = after[(ADSET_A1, "2026-11-15")]
    assert row["adset_name"] == "LAL 1% — Gulf"
    assert row["campaign_name"] == "Lead Gen | LAL | 2026"
    assert row["adset_id"] == ADSET_A1 and row["campaign_id"] == CAMPAIGN_A
    # Reconciliation is keyed by ids: two campaigns that SHARE a name and
    # differ in spend do not reconcile each other.
    verdict = adsets.reconcile_adsets_to_campaigns(
        [{"spend_date": "2026-11-16", "campaign_id": CAMPAIGN_A,
          "campaign_name": "Same name", "cost_micros": 5_000_000}],
        [{"spend_date": "2026-11-16", "campaign_id": CAMPAIGN_B,
          "campaign_name": "Same name", "cost_micros": 5_000_000}], 10_000)
    assert verdict["reconciled"] is False
    # No SQL of the ad-set readers or writer compares, joins or groups on a name.
    writer = inspect.getsource(REAL_ADSET_WRITER)
    repo_text = (ROOT / "db/meta_ads_repository.py").read_text()
    reader = repo_text[repo_text.index("# ── PR-ADS-171C"):]
    keyed = re.compile(r"\b(GROUP BY|JOIN|USING|PARTITION BY|DISTINCT ON)\b"
                       r"|\bON [a-z]\.|= ANY|\bAND [a-z]\.\w+ = [a-z]\.")
    for text in (writer, reader):
        keys = [line for line in text.splitlines() if keyed.search(line)]
        assert len(keys) >= 3
        assert not [line for line in keys if "_name" in line], keys
        # A name is only ever tested for being present, never for its value.
        assert not re.search(r"_name\s*(=|<>|!=|IN\b|LIKE\b)", text)
    assert "campaign_name" not in inspect.getsource(
        adsets.reconcile_adsets_to_campaigns).split('"""')[2]


def test_25_a_day_of_zero_spend_between_days_with_spend_is_a_row_not_a_gap(
        monkeypatch, store):
    graph = FakeGraph()
    old = graph.add_account(OLD)
    old.adset(ADSET_A1, CAMPAIGN_A, "2026-11-14", "20.00", actions=actions(lead=1))
    old.adset(ADSET_A1, CAMPAIGN_A, "2026-11-15", "0", impressions=12, clicks=0,
              actions=[])
    old.adset(ADSET_A1, CAMPAIGN_A, "2026-11-16", "18.50", actions=actions(lead=2))
    fakes.configure(monkeypatch, graph)
    _, detail, _ = _run()
    assert detail["status"] == "success"
    rows = _adset_rows(store)
    assert [rows[(ADSET_A1, d)]["cost_micros"] for d in
            ("2026-11-14", "2026-11-15", "2026-11-16")] == [20_000_000, 0, 18_500_000]
    assert rows[(ADSET_A1, "2026-11-15")]["impressions"] == 12
    # A day with no delivery at all (the 17th) has no row; it is inside the
    # verified chunk, so it is a known nothing.
    assert (ADSET_A1, "2026-11-17") not in rows


def test_26_an_archived_adset_keeps_its_history_under_its_id(monkeypatch, store):
    graph = _pair()
    old = graph.accounts[OLD]
    # Archived on the 1st; it spent in late October, inside the window.
    old.adset(ADSET_ARCHIVED, CAMPAIGN_A, "2026-10-28", "15.00",
              name="Look Alike — Egypt (archived)", actions=actions(lead=2))
    fakes.configure(monkeypatch, graph)
    _, detail, _ = _run()
    assert detail["status"] == "success"
    row = _adset_rows(store)[(ADSET_ARCHIVED, "2026-10-28")]
    assert row["cost_micros"] == 15_000_000 and row["meta_reported_lead_actions"] == {"lead": 2}
    # If Meta left an archived ad set OUT of the ad-set read, its spend would
    # be in the campaign row and missing here: a loud failure, not a silent
    # under-count.
    old.adset_hidden.add((ADSET_ARCHIVED, "2026-10-28"))
    store.adset_replaced.clear()
    _, detail, _ = _run()
    old_result = _by_account(detail, OLD)
    assert old_result["reason"] == "meta_adset_reconciliation_mismatch"
    assert "2026-10-28" in old_result["error"]
    assert not [w for w in store.adset_replaced if w["account_id"] == OLD]


def test_27_a_row_in_another_currency_fails_the_chunk(monkeypatch, store):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    campaign, _, _ = _run()
    store.adset_replaced.clear()
    out = adsets.sync_adset_chunk(
        {"account_id": OLD, "currency_code": "GBP", "time_zone_name": "Asia/Amman"},
        date(2026, 10, 21), date(2026, 11, 17))
    assert out["status"] == "failed" and out["reason"] == "meta_malformed_response"
    assert store.adset_replaced == []


# ── the writers this level may call ───────────────────────────────────────

def test_28_the_adset_level_cannot_write_a_campaign_table(monkeypatch, store):
    # Structural: the ad-set writer's SQL names only the two ad-set tables.
    writer = inspect.getsource(REAL_ADSET_WRITER)
    tables = set(re.findall(r"\b(meta_ads_[a-z_]+)\b", writer))
    assert tables == {"meta_ads_adset_daily_spend", "meta_ads_adset_coverage"}
    assert "sync_state" not in writer
    failure = inspect.getsource(REAL_ADSET_FAILURE)
    assert '"meta_ads_adset_coverage"' in failure
    assert "meta_ads_spend_coverage" not in failure.split('"""')[2]
    # The ad-set service calls no campaign-level writer.
    service = (ROOT / "services/meta_ads_adset_service.py").read_text()
    for name in ("replace_meta_campaign_spend_chunk", "upsert_meta_ads_account",
                 "record_meta_spend_coverage_failure", "record_meta_account_sync",
                 "refresh_account"):
        assert name not in service, name
    step = inspect.getsource(sync._sync_meta_ads_adsets)
    for name in ("record_meta_account_sync", "META_ADS_SPEND_DATASET",
                 "record_meta_spend_coverage_failure"):
        assert name not in step, name
    # Behavioural: through every ad-set failure, the campaign recorders see
    # exactly what a run without ad sets gives them.
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    errors: list = []
    sync._sync_meta_ads_spend(run_id=7, now=NOW, errors=errors)
    baseline = json.dumps([store.replaced, store.failures, store.synced,
                           store.accounts, store.finished], default=str)
    for breakage in ("hidden", "refused", "persistence"):
        graph = _pair()
        if breakage == "hidden":
            graph.accounts[OLD].adset_hidden.add((ADSET_A2, "2026-11-16"))
        elif breakage == "refused":
            graph.accounts[OLD].adset_refusal = error_response(403, 200)
        else:
            store.adset_replace_error = db_writers.MetaAdsPersistenceError("x")
        fakes.configure(monkeypatch, graph)
        for bucket in (store.replaced, store.failures, store.synced,
                       store.finished, store.batches):
            bucket.clear()
        campaign = sync._sync_meta_ads_spend(run_id=7, now=NOW, errors=[])
        after_campaign = json.dumps([store.replaced, store.failures, store.synced,
                                     store.accounts, store.finished], default=str)
        assert after_campaign == baseline
        detail = sync._sync_meta_ads_adsets(run_id=7, campaign_result=campaign,
                                            errors=[])
        assert detail["status"] in ("partial", "failed")
        campaign_finished = [f for f in store.finished if f["batch_id"] == 1]
        assert json.dumps([store.replaced, store.failures, store.synced,
                           store.accounts, campaign_finished],
                          default=str) == baseline, breakage


# ── registration ──────────────────────────────────────────────────────────

def test_29_the_adset_pair_is_registered_everywhere_the_campaign_pair_is():
    pair = (dataset_keys.META_ADS_SOURCE, dataset_keys.META_ADS_ADSET_DATASET)
    assert pair == ("meta_ads_api", "meta_adset_spend")
    assert dataset_keys.is_registered_pair(*pair)
    assert pair in sync.ACTIVE_SYNC_PAIRS
    assert sync.LABEL_META_ADS_ADSETS == "meta_ads_api/meta_adset_spend"
    config = DATASET_FRESHNESS_CONFIG["meta_ads_adsets"]
    assert (config["source"], config["dataset"]) == pair
    assert config["table"] == "meta_ads_adset_daily_spend"
    assert config["page"] is None and config["depends_on"] == ["meta_ads_spend"]
    from services import system_status_service as status
    assert status.SOURCE_DEFINITIONS["meta_ads_api"]["datasets"] == [
        "meta_ads_spend", "meta_ads_adsets"]
    assert status.PIPELINE_DEPENDENCIES["meta_ads_adsets"]["page"] is None
    assert "meta_ads_adsets" not in status.CORE_DATASETS
    for impacted in status.PAGE_PIPELINE_IMPACT.values():
        assert "meta_ads_adsets" not in impacted
    assert '"meta_ads_api/meta_adset_spend": "Meta Ads ad-set data"' in (
        ROOT / "static/app.js").read_text()
    # The campaign pair's own registration is untouched.
    spend = DATASET_FRESHNESS_CONFIG["meta_ads_spend"]
    assert spend["dataset"] == "meta_campaign_spend" and spend["depends_on"] == []


def test_30_no_new_run_type_and_no_new_freshness_status():
    assert sync.RUN_TYPE == "daily_incremental_sync"
    assert not [k for k in monitoring.RUN_TYPE_CADENCE if "meta" in k or "adset" in k]
    for word in ("adset", "meta"):
        assert not [s for s in CanonicalFreshnessStatus.ALL if word in s]
    # The step sits right after the campaign step in the daily run.
    body = inspect.getsource(sync.run_daily_incremental_sync)
    campaign_at = body.index("datasets[LABEL_META_ADS_SPEND] = ")
    adsets_at = body.index("datasets[LABEL_META_ADS_ADSETS] = ")
    geo_at = body.index("datasets[LABEL_CANONICAL_GEO] = ")
    assert campaign_at < adsets_at < geo_at


def test_31_no_page_and_no_route_reads_the_adset_figures_yet():
    for path in ("api/server.py", "static/app.js", "static/index.html"):
        file = ROOT / path
        if not file.exists():
            continue
        text = file.read_text()
        assert "meta_ads_adset_truth_service" not in text
        assert "build_meta_adset_breakdown_for_days" not in text
        assert "meta_reported_leads" not in text


# ── backfill ──────────────────────────────────────────────────────────────

def test_32_the_default_backfill_is_the_campaign_level_and_reads_no_adset(
        monkeypatch, store):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    out = meta.run_backfill(date(2026, 11, 1), date(2026, 11, 16), now=NOW)
    assert out["status"] == "success" and "level" not in out
    assert graph.insight_calls("adset") == []
    assert store.adset_replaced == [] and store.adset_failures == []
    assert len(store.replaced) == 2
    from scripts import backfill_meta_ads_spend as script
    assert script._parse(["--from", "2026-11-01"]).level == "campaign"
    assert script._parse(["--from", "2026-11-01", "--level", "adset"]).level == "adset"
    with pytest.raises(SystemExit):
        script._parse(["--from", "2026-11-01", "--level", "ad"])
    bad = meta.run_backfill(date(2026, 11, 1), None, now=NOW, level="ad")
    assert bad["status"] == "failed" and graph.insight_calls("adset") == []


def test_33_an_adset_backfill_plans_resumes_and_takes_one_account(
        monkeypatch, store):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    plan = meta.run_backfill(date(2026, 10, 21), None, dry_run=True, now=NOW,
                             level="adset")
    assert plan["status"] == "success" and plan["level"] == "adset"
    assert plan["dry_run"] is True
    assert [c["status"] for a in plan["accounts"] for c in a["chunks"]] == [
        "planned"] * 4
    assert graph.insight_calls() == []           # a plan reads no spend
    assert store.adset_replaced == [] and store.accounts == {}

    # The campaign level first, then the ad sets of the same months.
    meta.run_backfill(date(2026, 10, 21), None, now=NOW)
    sync_calls = list(store.synced)
    stored_accounts = json.dumps(store.accounts, default=str)
    out = meta.run_backfill(date(2026, 10, 21), None, now=NOW, level="adset",
                            account_ids=[OLD])
    assert out["status"] == "success" and [a["account_id"] for a in out["accounts"]] == [OLD]
    chunks = out["accounts"][0]["chunks"]
    assert [(c["chunk_start"], c["chunk_end"], c["status"]) for c in chunks] == [
        ("2026-10-21", "2026-10-31", "success"),
        ("2026-11-01", "2026-11-17", "success")]
    assert chunks[1]["rows_written"] == 6
    assert graph.insight_calls("adset", NEW) == []
    # It stored nothing about the account, wrote no sync state, opened no batch.
    assert store.synced == sync_calls
    assert json.dumps(store.accounts, default=str) == stored_accounts
    assert store.batches == []

    # --resume skips a month the AD-SET ledger already holds verified.
    from db import meta_ads_repository as repo
    monkeypatch.setattr(repo, "fetch_meta_adset_coverage",
                        lambda start, end, account_id: {
                            "available": True, "chunks": [{
                                "chunk_start": "2026-10-21",
                                "chunk_end": "2026-10-31", "status": "verified"}]})
    # A verified CAMPAIGN month does not make an ad-set month "done".
    monkeypatch.setattr(repo, "fetch_meta_spend_coverage",
                        lambda start, end, account_id: {
                            "available": True, "chunks": [{
                                "chunk_start": "2026-11-01",
                                "chunk_end": "2026-11-17", "status": "verified"}]})
    before = len(graph.insight_calls("adset", OLD))
    out = meta.run_backfill(date(2026, 10, 21), None, now=NOW, level="adset",
                            account_ids=[OLD], resume=True)
    assert [c["status"] for c in out["accounts"][0]["chunks"]] == [
        "already_verified", "success"]
    assert len(graph.insight_calls("adset", OLD)) == before + 1


def test_34_an_adset_backfill_of_a_month_without_a_campaign_base_stops(
        monkeypatch, store):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    # Only November was backfilled at campaign level.
    meta.run_backfill(date(2026, 11, 1), None, now=NOW, account_ids=[OLD])
    out = meta.run_backfill(date(2026, 10, 1), None, now=NOW, level="adset",
                            account_ids=[OLD])
    assert out["status"] == "failed"
    assert out["reason"] == "meta_adset_campaign_base_unverified"
    account = out["accounts"][0]
    assert [c["status"] for c in account["chunks"]] == ["failed"]   # stopped
    assert store.adset_replaced == []
    assert store.adset_failures[0]["error_code"] == "meta_adset_campaign_base_unverified"
    # The script maps the outcome to its exit code and passes the level on.
    from scripts import backfill_meta_ads_spend as script
    seen = {}

    def fake_backfill(date_from, date_to, **kw):
        seen.update(kw)
        return {"status": "partial"}
    monkeypatch.setattr(meta, "run_backfill", fake_backfill)
    assert script.main(["--from", "2026-10-01", "--dry-run", "--level", "adset",
                        "--account", OLD, "--resume"]) == 3
    assert seen == {"account_ids": [OLD], "dry_run": True, "resume": True,
                    "accept_zero_restatement": False, "level": "adset"}
    seen.clear()
    assert script.main(["--from", "2026-10-01", "--dry-run"]) == 3
    assert "level" not in seen               # the default call is unchanged


# ── the smoke test ────────────────────────────────────────────────────────

def test_35_the_smoke_test_prints_the_action_types_this_account_emits(
        monkeypatch, capsys):
    from scripts import meta_ads_smoke_test as smoke
    graph = FakeGraph()
    old = graph.add_account(OLD)
    day = meta.account_local_yesterday("Asia/Amman").isoformat()
    old.adset(ADSET_A1, CAMPAIGN_A, day, "40.00", name="Look Alike — GCC",
              campaign_name=LEAD_GEN, actions=actions(**THREE_LEADS))
    old.adset(ADSET_A2, CAMPAIGN_A, day, "30.00", actions=NO_ACTIONS, clicks=None)
    old.adset(ADSET_B1, CAMPAIGN_B, day, "12.00", campaign_name=AWARENESS,
              actions=actions(video_view=9))
    fakes.configure(monkeypatch, graph)
    assert smoke.main(["--adsets"]) == 0
    out = capsys.readouterr().out
    assert f"--- Ad account {OLD} ---" in out and f"--- Ad account {NEW} ---" in out
    assert "Ad sets found    : 3 in 2 campaign(s), 3 row(s), 1 page(s)" in out
    assert "Ad-set sum       : 82.00 USD" in out
    assert "Campaign total   : 82.00 USD" in out
    assert f"  campaign {CAMPAIGN_A}: ad sets 70.00, campaign row 70.00 USD" in out
    assert "Rows w/o actions : 1 of 3" in out
    assert "Rows w/o clicks  : 1 of 3; w/o impressions: 0" in out
    assert "Attribution      : 7d_click_1d_view" in out
    assert "Lead action types: 3 (Meta-reported; overlapping" in out
    for line in ("  lead = 3", "  leadgen_grouped = 3",
                 "  onsite_conversion.lead_grouped = 3"):
        assert line in out
    assert f"  ad set {ADSET_A2} (campaign {CAMPAIGN_A}): spend 30.00 USD; " \
           "actions absent" in out
    assert f"  ad set {ADSET_B1} (campaign {CAMPAIGN_B}): spend 12.00 USD; " \
           "actions present, no lead type" in out
    assert "RESULT           : OK — every campaign's ad-set rows sum" in out
    assert "=== 2 of 2 account(s) OK ===" in out
    # Never a token, never a name, never a sum across the types.
    for secret in (SENTINEL_TOKEN, SENTINEL_TOKEN_2, "Look Alike", LEAD_GEN,
                   AWARENESS, "= 9"):
        assert secret not in out
    # It wrote nothing: the script touches no database module.
    assert "db." not in (ROOT / "scripts/meta_ads_smoke_test.py").read_text()

    old.adset_hidden.add((ADSET_A2, day))
    assert smoke.main(["--adsets"]) == 1
    out = capsys.readouterr().out
    assert f"MISMATCH {day} campaign {CAMPAIGN_A}: ad sets 40.00, campaign row " \
           "70.00 USD" in out
    assert "do not backfill ad sets" in out
    assert smoke.main(["--adsets", "--month", "2026-03"]) == 2
