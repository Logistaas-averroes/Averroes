"""PR-ADS-171D — the ad-set backfill on a live account, on a real schema.

What production did on 2026-11-23 (owner's Render shell), reproduced against
the fake Graph API (tests/meta_ads_fakes.py), whose old account is the
production one: 944370805110952, USD, Asia/Amman.

1. ``--level adset --from 2025-01-01`` at 04:2x UTC. The backfill ended at the
   account's local yesterday (22 Nov); the campaign level was verified only
   through 21 Nov (the 06:00 UTC daily sync had not run yet). The November
   month failed ``meta_adset_campaign_base_unverified`` and was discarded.
2. ``--to 2026-11-21``: ``meta_adset_reconciliation_mismatch`` on 21 Nov —
   fresh ad-set rows 61,320,000 micros against the STORED campaign row,
   61,300,000 micros, read by the previous morning's sync. Meta had restated
   the day by $0.02 between the two reads.

The fix: each ad-set month first re-reads the campaign level of the same days
through the real campaign chunk sync, and reconciles the ad sets to that live
read; an end after the last day the campaign level can be verified stops
there and says so. Cases 01–03 fail on the parent commit.

Every case needs the database; a skipped module is not merge evidence.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

import scheduler.incremental_sync as sync
from services import fx_service
from services import meta_ads_adset_service as adsets
from services import meta_ads_adset_truth_service as adset_truth
from services import meta_ads_spend_service as meta
from tests import meta_ads_fakes as fakes
from tests.meta_ads_fakes import (
    ACCOUNT_ID, ADSET_A1, ADSET_A2, ADSET_B1, CAMPAIGN_A, CAMPAIGN_B,
    OLD_ACCOUNT_ID, SENTINEL_TOKEN, SENTINEL_TOKEN_2, FakeGraph, actions,
)

NEW, OLD = ACCOUNT_ID, OLD_ACCOUNT_ID
#: The owner's backfill: 04:25 UTC on 23 Nov = 07:25 in Amman. Amman's local
#: yesterday is the 22nd; Los Angeles' is the 21st.
PROD_NOW = datetime(2026, 11, 23, 4, 25, tzinfo=timezone.utc)
#: The daily sync before it (09:00 Amman on 22 Nov): Amman through the 21st.
PREV_SYNC = datetime(2026, 11, 22, 6, 0, tzinfo=timezone.utc)
#: The daily sync after it.
NEXT_SYNC = datetime(2026, 11, 23, 6, 0, tzinfo=timezone.utc)
NOV_1 = date(2026, 11, 1)
D = date(2026, 11, 23)            # Amman's local TODAY at PROD_NOW
D_1 = date(2026, 11, 22)          # Amman's local yesterday: the last verifiable day

#: Native lead forms only: Meta returned these five types with identical
#: counts on every row (production, 2026-11-23).
FIVE = ("lead", "onsite_conversion.lead_grouped",
        "offsite_complete_registration_add_meta_leads",
        "offsite_content_view_add_meta_leads", "offsite_search_add_meta_leads")
ATTRIBUTION = "1d_view_7d_click"


def _leads(n: int, **extra) -> list:
    return [{"action_type": t, "value": str(n)} for t in FIVE] + \
        actions(**extra)


@pytest.fixture()
def live_db(monkeypatch):
    from tests import test_pr_ads_153b_pg_integration as pgmod
    if not pgmod._have_postgres():
        pytest.skip("PostgreSQL not available")
    import psycopg2.sql  # noqa: F401
    cluster = pgmod._PgCluster().start()
    import db.connection as connection
    try:
        monkeypatch.setenv("DATABASE_URL", cluster.url)
        monkeypatch.setattr(connection, "_pool", None, raising=False)
        connection.init_pool()
        from db.schema import init_db
        init_db()
        yield cluster
    finally:
        if getattr(connection, "_pool", None) is not None:
            connection._pool.closeall()
            connection._pool = None
        cluster.stop()


def q(db, sql, params=None):
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _production() -> FakeGraph:
    """The old account's November, as the owner's account has it: two
    campaigns; campaign A's 21 Nov is 61.30 (two ad sets); spend on the 22nd;
    the 23rd in progress."""
    graph = FakeGraph()
    old = graph.add_account(OLD)
    old.adset(ADSET_A1, CAMPAIGN_A, "2026-11-20", "40.00",
              actions=_leads(3, link_click=40), attribution_setting=ATTRIBUTION)
    old.adset(ADSET_A1, CAMPAIGN_A, "2026-11-21", "30.65",
              actions=_leads(2, link_click=31), attribution_setting=ATTRIBUTION)
    old.adset(ADSET_A2, CAMPAIGN_A, "2026-11-21", "30.65",
              actions=_leads(1, link_click=12), attribution_setting=ATTRIBUTION)
    old.adset(ADSET_A1, CAMPAIGN_A, "2026-11-22", "28.10",
              actions=_leads(2, link_click=20), attribution_setting=ATTRIBUTION)
    old.adset(ADSET_B1, CAMPAIGN_B, "2026-11-22", "12.00",
              actions=actions(video_view=900), attribution_setting=ATTRIBUTION)
    old.adset(ADSET_A1, CAMPAIGN_A, "2026-11-23", "9.99",       # in progress
              actions=_leads(1), attribution_setting=ATTRIBUTION)
    return graph


def _campaign_sync(now):
    """The daily CAMPAIGN step alone (the 171A step)."""
    return sync._sync_meta_ads_spend(run_id=None, now=now, errors=[])


def _daily(now):
    errors: list = []
    campaign = sync._sync_meta_ads_spend(run_id=None, now=now, errors=errors)
    detail = sync._sync_meta_ads_adsets(run_id=None, campaign_result=campaign,
                                        errors=errors)
    return campaign, detail


def _adset_rows(db, account_id=OLD):
    return q(db, "SELECT spend_date::text, campaign_id, adset_id, cost_micros "
                 "FROM meta_ads_adset_daily_spend WHERE account_id = %s "
                 "ORDER BY spend_date, campaign_id, adset_id", (account_id,))


def _campaign_rows(db, account_id=OLD):
    return q(db, "SELECT spend_date::text, campaign_id, cost_micros "
                 "FROM meta_ads_campaign_daily_spend WHERE account_id = %s "
                 "ORDER BY spend_date, campaign_id", (account_id,))


def _ledger(db, table, account_id=OLD):
    return q(db, f"SELECT chunk_start::text, chunk_end::text, status, error_code "
                 f"FROM {table} WHERE account_id = %s "
                 "ORDER BY chunk_start, chunk_end", (account_id,))


def _fx(monkeypatch, rate=1.25):
    def fetch(rate_date, base, quote):
        return {"rate_date": rate_date, "base_currency": base,
                "quote_currency": quote, "rate": rate, "provider": "test",
                "source_version": "test", "effective_date": rate_date}
    monkeypatch.setattr(fx_service, "fetch_fx_rate", fetch)


def _no_token(*things):
    text = json.dumps(things, default=str)
    for token in (SENTINEL_TOKEN, SENTINEL_TOKEN_2):
        assert token not in text


# ── production scenario 1: the end is past what the campaign level holds ──

def test_01_asked_through_today_stores_through_yesterday_and_says_where_it_stopped(
        live_db, monkeypatch):
    """Campaign level verifiable through D-1 (Amman's local yesterday, the
    22nd); the owner asks through D (the 23rd). The stored campaign level
    ends on the 21st, as in production before the 06:00 sync. Days through
    D-1 are stored, ``stopped_at`` is D-1, and nothing fails."""
    graph = _production()
    fakes.configure(monkeypatch, graph)
    _campaign_sync(PREV_SYNC)
    assert _ledger(live_db, "meta_ads_spend_coverage")[-1][:3] == \
        ("2026-10-25", "2026-11-21", "verified")
    bookkeeping = {t: q(live_db, f"SELECT COUNT(*) FROM {t}")
                   for t in ("sync_batches", "sync_state", "runs")}

    out = meta.run_backfill(NOV_1, D, now=PROD_NOW, level="adset",
                            account_ids=[OLD])
    assert out["status"] == "success", out
    account = out["accounts"][0]
    assert account["status"] == "success" and account["reason"] is None
    assert account["requested_to"] == D.isoformat()
    assert account["campaign_verifiable_through"] == D_1.isoformat()
    assert account["stopped_at"] == D_1.isoformat()
    assert account["stop_reason"] == meta.REASON_ADSET_STOPPED
    assert "2026-11-23" in account["stop_detail"] and "Asia/Amman" in account["stop_detail"]
    assert account["date_to"] == D_1.isoformat()
    [chunk] = account["chunks"]
    assert (chunk["chunk_start"], chunk["chunk_end"], chunk["status"]) == (
        "2026-11-01", "2026-11-22", "success")
    assert chunk["campaign_reread"]["coverage_status"] == "verified"

    # Every day through D-1 is stored; nothing of D (in progress) is.
    assert _adset_rows(live_db) == [
        ("2026-11-20", CAMPAIGN_A, ADSET_A1, 40_000_000),
        ("2026-11-21", CAMPAIGN_A, ADSET_A1, 30_650_000),
        ("2026-11-21", CAMPAIGN_A, ADSET_A2, 30_650_000),
        ("2026-11-22", CAMPAIGN_A, ADSET_A1, 28_100_000),
        ("2026-11-22", CAMPAIGN_B, ADSET_B1, 12_000_000)]
    assert q(live_db, "SELECT MAX(spend_date)::text FROM meta_ads_campaign_daily_spend"
                      " WHERE account_id = %s", (OLD,)) == [("2026-11-22",)]
    # The campaign level of the month was re-read and verified in this run,
    # and the ad-set month is verified on top of it.
    assert ("2026-11-01", "2026-11-22", "verified", None) in \
        _ledger(live_db, "meta_ads_spend_coverage")
    assert _ledger(live_db, "meta_ads_adset_coverage") == [
        ("2026-11-01", "2026-11-22", "verified", None)]
    # Per day and campaign, the stored levels agree.
    assert q(live_db, """
        SELECT COUNT(*) FROM (
          SELECT spend_date, campaign_id, SUM(cost_micros) m
          FROM meta_ads_adset_daily_spend GROUP BY 1, 2) a
        FULL JOIN (
          SELECT spend_date, campaign_id, SUM(cost_micros) m
          FROM meta_ads_campaign_daily_spend
          WHERE spend_date >= '2026-11-01' GROUP BY 1, 2) c
          USING (spend_date, campaign_id)
        WHERE a.m IS DISTINCT FROM c.m""") == [(0,)]
    # No sync_batches, sync_state or runs row added: still a backfill.
    for table, count in bookkeeping.items():
        assert q(live_db, f"SELECT COUNT(*) FROM {table}") == count, table
    _no_token(out)

    # The plan says the same before anything is read.
    plan = meta.run_backfill(NOV_1, D, now=PROD_NOW, level="adset",
                             account_ids=[OLD], dry_run=True)
    assert plan["accounts"][0]["stopped_at"] == D_1.isoformat()
    assert [(c["chunk_end"], c["status"]) for c in plan["accounts"][0]["chunks"]] \
        == [("2026-11-22", "planned")]


def test_02_the_owners_first_command_loads_november_without_a_to(
        live_db, monkeypatch):
    """The exact command of 04:2x UTC: no ``--to``. The end is Amman's local
    yesterday (the 22nd) and the stored campaign level ends on the 21st. On
    the parent commit the whole month failed
    ``meta_adset_campaign_base_unverified``. Both accounts, as the owner ran
    it: Los Angeles' local yesterday is the 21st."""
    graph = _production()
    fakes.configure(monkeypatch, graph)
    _campaign_sync(PREV_SYNC)
    out = meta.run_backfill(NOV_1, None, now=PROD_NOW, level="adset")
    assert out["status"] == "success", out
    by_id = {a["account_id"]: a for a in out["accounts"]}
    assert (by_id[OLD]["date_to"], by_id[NEW]["date_to"]) == ("2026-11-22", "2026-11-21")
    for account in by_id.values():
        # Nothing was asked beyond what can be verified: nothing stopped.
        assert account["requested_to"] is None and account["stopped_at"] is None
        assert [c["status"] for c in account["chunks"]] == ["success"]
    assert _adset_rows(live_db)[-1] == ("2026-11-22", CAMPAIGN_B, ADSET_B1, 12_000_000)
    assert _ledger(live_db, "meta_ads_adset_coverage", NEW) == [
        ("2026-11-01", "2026-11-21", "verified", None)]


# ── production scenario 2: Meta restated a recent day between two reads ──

@pytest.mark.parametrize("to", [date(2026, 11, 21), None],
                         ids=["owner_rerun_with_to", "no_to"])
def test_03_a_two_cent_restatement_is_re_read_at_campaign_level_and_reconciles(
        live_db, monkeypatch, to):
    graph = _production()
    fakes.configure(monkeypatch, graph)
    _campaign_sync(PREV_SYNC)
    stored = dict(((d, c), m) for d, c, m in _campaign_rows(live_db))
    assert stored[("2026-11-21", CAMPAIGN_A)] == 61_300_000

    # Meta restates 21 Nov by $0.02 (A2: 30.65 -> 30.67; campaign 61.32).
    graph.accounts[OLD].adset(ADSET_A2, CAMPAIGN_A, "2026-11-21", "30.67",
                              actions=_leads(1, link_click=12),
                              attribution_setting=ATTRIBUTION)
    fakes.configure(monkeypatch, graph)

    out = meta.run_backfill(NOV_1, to, now=PROD_NOW, level="adset",
                            account_ids=[OLD])
    assert out["status"] == "success", out
    [chunk] = out["accounts"][0]["chunks"]
    assert chunk["status"] == "success"
    # The re-read saw the restatement and stored it.
    assert chunk["campaign_reread"]["status"] == "success"
    assert chunk["campaign_reread"]["days_restated"] == 1
    assert chunk["campaign_reread"]["max_restated_delta_micros"] == 20_000
    # The campaign row is now the fresh value; the ad-set rows sum to it.
    campaign = dict(((d, c), m) for d, c, m in _campaign_rows(live_db))
    assert campaign[("2026-11-21", CAMPAIGN_A)] == 61_320_000
    assert q(live_db, "SELECT SUM(cost_micros) FROM meta_ads_adset_daily_spend "
                      "WHERE account_id = %s AND spend_date = '2026-11-21' "
                      "AND campaign_id = %s", (OLD, CAMPAIGN_A)) == [(61_320_000,)]
    assert _ledger(live_db, "meta_ads_adset_coverage") == [
        ("2026-11-01", chunk["chunk_end"], "verified", None)]
    # The reconciliation tolerance did not move: one minor unit.
    assert meta.reconciliation_tolerance_micros("USD") == 10_000
    # The read function's own check agrees, in the database.
    from db import meta_ads_repository as repo
    assert repo.fetch_meta_adset_spend(NOV_1, date(2026, 11, 21), OLD,
                                       tolerance_micros=10_000)["mismatches"] == []


# ── the campaign re-read fails ────────────────────────────────────────────

def test_04_a_failed_campaign_re_read_fails_the_adset_month_and_stores_nothing(
        live_db, monkeypatch):
    graph = _production()
    fakes.configure(monkeypatch, graph)
    _campaign_sync(PREV_SYNC)
    campaign_before = _campaign_rows(live_db)
    verified_before = [r for r in _ledger(live_db, "meta_ads_spend_coverage")
                       if r[2] == "verified"]
    # Meta leaves campaign B out of the level=campaign read on the 22nd: the
    # campaign rows no longer sum to the account total. The re-read fails.
    graph.accounts[OLD].hidden.add((CAMPAIGN_B, "2026-11-22"))
    fakes.configure(monkeypatch, graph)
    adset_calls = len(graph.insight_calls("adset"))

    out = meta.run_backfill(NOV_1, None, now=PROD_NOW, level="adset",
                            account_ids=[OLD])
    assert out["status"] == "failed"
    assert out["reason"] == meta.REASON_RECONCILIATION
    [chunk] = out["accounts"][0]["chunks"]
    assert chunk["status"] == "failed" and chunk["rows_written"] == 0
    assert chunk["reason"] == meta.REASON_RECONCILIATION
    assert "ad sets were not read" in chunk["error"]
    assert chunk["campaign_reread"]["coverage_status"] == "failed"
    # No ad set was requested and none stored.
    assert len(graph.insight_calls("adset")) == adset_calls
    assert _adset_rows(live_db) == []
    assert _ledger(live_db, "meta_ads_adset_coverage") == [
        ("2026-11-01", "2026-11-22", "failed", meta.REASON_RECONCILIATION)]
    # The campaign level keeps exactly what it had.
    assert _campaign_rows(live_db) == campaign_before
    assert [r for r in _ledger(live_db, "meta_ads_spend_coverage")
            if r[2] == "verified"] == verified_before


def test_05_a_campaign_re_read_that_would_zero_a_verified_day_is_still_refused(
        live_db, monkeypatch):
    graph = _production()
    fakes.configure(monkeypatch, graph)
    _campaign_sync(PREV_SYNC)
    assert ("2026-11-20", CAMPAIGN_A, 40_000_000) in _campaign_rows(live_db)
    # Meta now answers the 20th as if nothing was spent.
    graph.accounts[OLD].remove_adset(ADSET_A1, "2026-11-20")
    fakes.configure(monkeypatch, graph)

    # Not even with the backfill's override: at --level adset it lifts the
    # refusal for ad-set rows only. The campaign re-read never takes it.
    for accept in (False, True):
        out = meta.run_backfill(NOV_1, None, now=PROD_NOW, level="adset",
                                account_ids=[OLD], accept_zero_restatement=accept)
        assert out["status"] == "failed", accept
        assert out["reason"] == meta.REASON_RESTATEMENT
        [chunk] = out["accounts"][0]["chunks"]
        assert chunk["campaign_reread"]["reason"] == meta.REASON_RESTATEMENT
        assert ("2026-11-20", CAMPAIGN_A, 40_000_000) in _campaign_rows(live_db)
        assert _adset_rows(live_db) == []
        assert graph.insight_calls("adset") == []

    # The campaign-level backfill's explicit override is still the only way;
    # after it, the ad-set month loads.
    assert meta.run_backfill(NOV_1, None, now=PROD_NOW, account_ids=[OLD],
                             accept_zero_restatement=True)["status"] == "success"
    assert meta.run_backfill(NOV_1, None, now=PROD_NOW, level="adset",
                             account_ids=[OLD])["status"] == "success"
    assert [r for r in _adset_rows(live_db) if r[0] == "2026-11-20"] == []


# ── the configured lead type and the attribution setting ──────────────────

def test_06_the_configured_lead_type_publishes_where_the_account_has_observed_it(
        live_db, monkeypatch):
    # The shipped configuration, not a monkeypatched one.
    assert adsets.native_lead_action_type() == "onsite_conversion.lead_grouped"
    graph = _production()
    # The new account delivers on the 21st and has never emitted the type.
    graph.default.adset("120219876543210099", "120211234567890199", "2026-11-21",
                        "10.00", actions=actions(link_click=5),
                        attribution_setting=ATTRIBUTION)
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch)
    assert meta.run_backfill(NOV_1, None, now=PROD_NOW, level="adset")["status"] \
        == "success"

    out = adset_truth.build_meta_adset_breakdown_for_days(
        NOV_1, date(2026, 11, 21), settled=True, now=PROD_NOW)
    assert out["status"] == "published", out
    assert out["native_lead_action_type"] == "onsite_conversion.lead_grouped"
    rows = {r["adset_id"]: r for r in out["rows"]}
    # One type's count — 3 + 2 on A1 — never five types added together.
    assert rows[ADSET_A1]["meta_reported_leads"] == {
        "value": 5, "status": "published", "reason": None,
        "action_type": "onsite_conversion.lead_grouped"}
    assert rows[ADSET_A2]["meta_reported_leads"]["value"] == 1
    assert rows[ADSET_A1]["meta_reported_lead_actions"]["value"] == {
        t: 5 for t in FIVE}
    # The account that never carried the type is unavailable, not 0.
    assert rows["120219876543210099"]["meta_reported_leads"] == {
        "value": None, "status": "unavailable",
        "reason": "meta_lead_action_type_not_observed",
        "action_type": "onsite_conversion.lead_grouped"}
    assert {a["account_id"]: a["meta_reported_leads_type_observed"]
            for a in out["accounts"]} == {NEW: False, OLD: True}
    # The attribution setting the counts are under, from the stored rows.
    assert out["meta_reported_leads_attribution"] == {
        "settings": [ATTRIBUTION], "rows_without_setting": 0,
        "status": "stored", "detail": None}
    assert rows[ADSET_A1]["attribution_settings"] == [ATTRIBUTION]


def test_07_an_attribution_setting_that_was_not_stored_is_said_not_invented(
        live_db, monkeypatch):
    graph = FakeGraph()
    old = graph.add_account(OLD)
    old.adset(ADSET_A1, CAMPAIGN_A, "2026-11-21", "20.00", actions=_leads(1),
              attribution_setting=None)
    fakes.configure(monkeypatch, graph)
    assert meta.run_backfill(NOV_1, None, now=PROD_NOW, level="adset")["status"] \
        == "success"
    out = adset_truth.build_meta_adset_breakdown_for_days(
        NOV_1, date(2026, 11, 21), settled=True, now=PROD_NOW)
    assert out["status"] == "published"
    attribution = out["meta_reported_leads_attribution"]
    assert attribution["status"] == "not_stored" and attribution["settings"] == []
    assert attribution["rows_without_setting"] == 1
    assert attribution["detail"].startswith("not stored")
    # Withheld means absent: no attribution for rows that are not published.
    # (The 22nd is still in progress in Los Angeles.)
    withheld = adset_truth.build_meta_adset_breakdown_for_days(
        NOV_1, date(2026, 11, 22), settled=True, now=PROD_NOW)
    assert withheld["status"] != "published"
    assert withheld["meta_reported_leads_attribution"] is None


# ── the daily step was never affected ─────────────────────────────────────

def test_08_the_daily_step_reads_both_levels_in_one_run_and_absorbs_a_restatement(
        live_db, monkeypatch):
    graph = _production()
    fakes.configure(monkeypatch, graph)
    campaign, detail = _daily(PREV_SYNC)
    assert (campaign["status"], detail["status"]) == ("success", "success")
    graph.accounts[OLD].adset(ADSET_A2, CAMPAIGN_A, "2026-11-21", "30.67",
                              actions=_leads(1), attribution_setting=ATTRIBUTION)
    fakes.configure(monkeypatch, graph)
    first = len(graph.calls)
    campaign, detail = _daily(NEXT_SYNC)
    assert (campaign["status"], detail["status"]) == ("success", "success")
    # In this run, for the old account: the campaign read, then the ad sets —
    # the base the ad sets reconcile to is minutes old, never a day.
    levels = [c["params"].get("level") for c in graph.calls[first:]
              if c["account_id"] == OLD and c["url"].endswith("/insights")]
    assert levels == ["campaign", "account", "adset"]
    assert dict(((d, c), m) for d, c, m in _campaign_rows(live_db))[
        ("2026-11-21", CAMPAIGN_A)] == 61_320_000
