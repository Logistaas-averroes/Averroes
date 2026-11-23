"""PR-ADS-171C — Meta ad-set detail on a real PostgreSQL schema.

The real sync steps, writers, repository and read function against the fake
Graph API (tests/meta_ads_fakes.py), whose accounts are the two production
ones: 4730305973961806 (GBP, America/Los_Angeles, no delivery) and
944370805110952 (USD, Asia/Amman, two campaigns with native lead forms).

What only a database can show: that a failed ad-set read leaves every
campaign table byte-identical; that the replace is atomic; that JSONB keeps
"no `actions`" (NULL) apart from "no lead type" ('{}'); and that the read
function withholds ad-set rows that no longer add up to the campaign rows.

Every case needs the database; a skipped module is not merge evidence.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

import db.writers as db_writers
import scheduler.incremental_sync as sync
from db import meta_ads_repository as repo
from services import fx_service
from services import meta_ads_adset_service as adsets
from services import meta_ads_adset_truth_service as adset_truth
from services import meta_ads_spend_service as meta
from services import meta_ads_spend_truth_service as truth_service
from tests import meta_ads_fakes as fakes
from tests.meta_ads_fakes import (
    ACCOUNT_ID, ADSET_A1, ADSET_A2, ADSET_B1, CAMPAIGN_A, CAMPAIGN_B,
    NO_ACTIONS, OLD_ACCOUNT_ID, SENTINEL_TOKEN, SENTINEL_TOKEN_2, FakeGraph,
    actions, error_response,
)

# 06:00 UTC, the daily sync. Los Angeles: the last complete day is the 16th.
# Amman: the 17th.
NOW = datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)
NEW, OLD = ACCOUNT_ID, OLD_ACCOUNT_ID
WEEK = (date(2026, 11, 10), date(2026, 11, 16))
LEAD_GEN = "Lead Generation | Look Alike | 2026"
AWARENESS = "Awareness Campaign | 2026"
THREE_LEADS = dict(lead=3, onsite_conversion__lead_grouped=3,
                   leadgen_grouped=3, link_click=40)

CAMPAIGN_TABLES = ("meta_ads_account", "meta_ads_campaign_daily_spend",
                   "meta_ads_account_daily_spend", "meta_ads_spend_coverage",
                   "meta_ads_account_sync_state")


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


def _pair() -> FakeGraph:
    graph = FakeGraph()
    old = graph.add_account(OLD)
    old.adset(ADSET_A1, CAMPAIGN_A, "2026-11-15", "40.00", name="Look Alike — GCC",
              campaign_name=LEAD_GEN, actions=actions(**THREE_LEADS))
    old.adset(ADSET_A2, CAMPAIGN_A, "2026-11-15", "30.00",
              name="Look Alike — Levant", actions=actions(link_click=12))
    old.adset(ADSET_A1, CAMPAIGN_A, "2026-11-16", "35.34", actions=NO_ACTIONS)
    old.adset(ADSET_A2, CAMPAIGN_A, "2026-11-16", "25.00",
              actions=actions(lead=1, onsite_conversion__lead_grouped=1))
    old.adset(ADSET_A2, CAMPAIGN_A, "2026-11-17", "0", impressions=40, clicks=0,
              actions=[])
    old.adset(ADSET_B1, CAMPAIGN_B, "2026-11-17", "12.00", name="Awareness — MENA",
              campaign_name=AWARENESS, actions=actions(video_view=900))
    old.adset(ADSET_A1, CAMPAIGN_A, "2026-11-18", "9.99")          # in progress
    return graph


def _fx(monkeypatch, rate=1.25, fail=()):
    def fetch(rate_date, base, quote):
        if rate_date in fail:
            raise RuntimeError("FX provider unavailable")
        return {"rate_date": rate_date, "base_currency": base,
                "quote_currency": quote, "rate": rate, "provider": "test",
                "source_version": "test", "effective_date": rate_date}
    monkeypatch.setattr(fx_service, "fetch_fx_rate", fetch)


def _campaigns(now=NOW):
    errors: list = []
    return sync._sync_meta_ads_spend(run_id=None, now=now, errors=errors), errors


def _run(now=NOW):
    """The two Meta steps, in the order the daily run calls them."""
    campaign, errors = _campaigns(now)
    detail = sync._sync_meta_ads_adsets(run_id=None, campaign_result=campaign,
                                        errors=errors)
    return campaign, detail, errors


def _rows(db, account_id=OLD):
    return q(db, "SELECT adset_id, spend_date::text, cost_micros, campaign_id, "
                 "adset_name, meta_reported_lead_actions FROM meta_ads_adset_daily_spend "
                 "WHERE account_id = %s ORDER BY spend_date, adset_id",
             (account_id,))


def _ledger(db, account_id=None):
    return q(db, "SELECT account_id, chunk_start::text, chunk_end::text, status, "
                 "rows_written, cost_micros_total, error_code, "
                 "rows_without_actions FROM meta_ads_adset_coverage "
                 "WHERE (%s::text IS NULL OR account_id = %s) "
                 "ORDER BY account_id, chunk_start, chunk_end",
             (account_id, account_id))


def _batches(db, dataset="meta_adset_spend"):
    return q(db, "SELECT status, row_count, error_message, date_from::text, "
                 "date_to::text FROM sync_batches WHERE source = 'meta_ads_api' "
                 "AND dataset = %s ORDER BY id", (dataset,))


def _campaign_side(db) -> str:
    """Everything the campaign level owns, as stored. Byte for byte."""
    out = [json.dumps(q(db, f"SELECT * FROM {table} ORDER BY 1, 2"), default=str)
           for table in CAMPAIGN_TABLES]
    out.append(json.dumps(q(
        db, "SELECT * FROM sync_batches WHERE dataset = 'meta_campaign_spend' "
            "ORDER BY id"), default=str))
    out.append(json.dumps(q(
        db, "SELECT * FROM sync_state WHERE dataset = 'meta_campaign_spend'"),
        default=str))
    return "\n".join(out)


def _detail(start=WEEK[0], end=WEEK[1], settled=True, now=NOW):
    return adset_truth.build_meta_adset_breakdown_for_days(
        start, end, settled=settled, now=now)


def _truth(start=WEEK[0], end=WEEK[1], settled=True, now=NOW):
    return truth_service.build_meta_ads_spend_truth_for_days(
        start, end, settled=settled, now=now)


def _by_adset(result):
    return {r["adset_id"]: r for r in result["rows"]}


def _no_token(*things):
    text = "\n".join(t if isinstance(t, str) else json.dumps(t, default=str)
                     for t in things)
    for token in (SENTINEL_TOKEN, SENTINEL_TOKEN_2):
        assert token not in text


# ── the daily sync ────────────────────────────────────────────────────────

def test_01_the_two_production_accounts_at_both_levels(live_db, monkeypatch):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    campaign, detail, errors = _run()
    assert campaign["status"] == "success" and detail["status"] == "success"
    assert errors == []

    assert _rows(live_db) == [
        (ADSET_A1, "2026-11-15", 40_000_000, CAMPAIGN_A, "Look Alike — GCC",
         {"lead": 3, "leadgen_grouped": 3, "onsite_conversion.lead_grouped": 3}),
        # `actions` present, no lead type: an empty object.
        (ADSET_A2, "2026-11-15", 30_000_000, CAMPAIGN_A, "Look Alike — Levant", {}),
        # No `actions` key: SQL NULL — unknown.
        (ADSET_A1, "2026-11-16", 35_340_000, CAMPAIGN_A, "Look Alike — GCC", None),
        (ADSET_A2, "2026-11-16", 25_000_000, CAMPAIGN_A, "Look Alike — Levant",
         {"lead": 1, "onsite_conversion.lead_grouped": 1}),
        (ADSET_A2, "2026-11-17", 0, CAMPAIGN_A, "Look Alike — Levant", {}),
        (ADSET_B1, "2026-11-17", 12_000_000, CAMPAIGN_B, "Awareness — MENA", {}),
    ]
    # NULL and '{}' are different stored values, not two spellings of zero.
    assert q(live_db, "SELECT COUNT(*) FILTER (WHERE meta_reported_lead_actions IS NULL), "
                      "COUNT(*) FILTER (WHERE meta_reported_lead_actions = '{}'::jsonb) "
                      "FROM meta_ads_adset_daily_spend") == [(1, 3)]
    assert _rows(live_db, NEW) == []
    assert _ledger(live_db) == [
        (NEW, "2026-10-20", "2026-11-16", "verified", 0, 0, None, 0),
        (OLD, "2026-10-21", "2026-11-17", "verified", 6, 142_340_000, None, 1),
    ]
    # Per day and campaign, the ad-set rows are the campaign rows.
    assert q(live_db, """
        SELECT COUNT(*) FROM (
          SELECT spend_date, campaign_id, SUM(cost_micros) AS m
          FROM meta_ads_adset_daily_spend GROUP BY 1, 2) a
        FULL JOIN (
          SELECT spend_date, campaign_id, SUM(cost_micros) AS m
          FROM meta_ads_campaign_daily_spend GROUP BY 1, 2) c
          USING (spend_date, campaign_id)
        WHERE a.m IS DISTINCT FROM c.m""") == [(0,)]
    # Its own batch; the campaign spend's batch is its own too.
    assert _batches(live_db) == [("success", 6, None, "2026-10-20", "2026-11-17")]
    assert _batches(live_db, "meta_campaign_spend")[0][0] == "success"
    assert q(live_db, "SELECT dataset, status, last_source_date::text "
                      "FROM sync_state WHERE source = 'meta_ads_api' "
                      "ORDER BY dataset") == [
        ("meta_adset_spend", "success", "2026-11-16"),
        ("meta_campaign_spend", "success", "2026-11-16")]
    _no_token(_campaign_side(live_db), campaign, detail,
              json.dumps(q(live_db, "SELECT * FROM meta_ads_adset_coverage"),
                         default=str))


def test_02_the_read_function_publishes_each_figure_with_its_status(
        live_db, monkeypatch):
    # No action type configured — the shipped state until PR-ADS-171D, which
    # set one (its own module asserts what the configured type publishes).
    monkeypatch.setattr(adsets, "native_lead_action_type", lambda: None)
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    _run()
    out = _detail()
    assert out["status"] == "published" and out["reason"] is None
    assert out["metric_family"] == "meta_reported_adset_delivery"
    assert out["window_basis"] == "meta_account_local_spend_date"
    assert out["names_are_labels"] is True
    assert out["campaign_spend_state"] == "verified"
    assert out["native_lead_action_type"] is None
    assert [(a["account_id"], a["adset_coverage"]) for a in out["accounts"]] == [
        (NEW, "verified"), (OLD, "verified")]
    rows = _by_adset(out)
    assert set(rows) == {ADSET_A1, ADSET_A2}       # the 17th is outside the week
    a1, a2 = rows[ADSET_A1], rows[ADSET_A2]
    assert (a1["account_id"], a1["campaign_id"]) == (OLD, CAMPAIGN_A)
    assert a1["adset_name"] == "Look Alike — GCC" and a1["campaign_name"] == LEAD_GEN
    assert a1["spend"] == {"value": 75.34, "status": "published", "reason": None,
                           "currency": "USD"}
    assert a1["spend_usd"] == {"value": 75.34, "status": "published",
                               "reason": None, "currency": "USD"}
    assert a1["impressions"]["value"] == 2000 and a1["clicks"]["value"] == 50
    assert a1["attribution_settings"] == ["7d_click_1d_view"]
    # No action type is confirmed as the native-form count: unavailable.
    for row in (a1, a2):
        assert row["meta_reported_leads"] == {
            "value": None, "status": "unavailable",
            "reason": "meta_lead_action_type_unverified", "action_type": None}
    # A1's row of the 16th carried no `actions`: its lead counts are unknown
    # for the range — not the 3 of the day that did carry them.
    assert a1["meta_reported_lead_actions"] == {
        "value": None, "status": "unavailable",
        "reason": "meta_actions_missing_on_some_days",
        "rows_without_actions": 1, "overlapping_types": True}
    assert a2["meta_reported_lead_actions"]["value"] == {
        "lead": 1, "onsite_conversion.lead_grouped": 1}
    # The ad-set spend of the range IS the campaign spend of the range.
    assert round(sum(r["spend"]["value"] for r in out["rows"]), 2) == \
        _truth()["native_spend"] == 130.34

    # With one type confirmed, that type's count — and only that type's.
    monkeypatch.setattr(adsets, "native_lead_action_type", lambda: "lead")
    confirmed = _by_adset(_detail())
    assert confirmed[ADSET_A2]["meta_reported_leads"] == {
        "value": 1, "status": "published", "reason": None, "action_type": "lead"}
    assert confirmed[ADSET_A1]["meta_reported_leads"]["value"] is None
    assert confirmed[ADSET_A1]["meta_reported_leads"]["reason"] == \
        "meta_actions_missing_on_some_days"
    # The 15th alone, where A1 carried actions: 3 — not 9.
    one_day = _by_adset(_detail(date(2026, 11, 15), date(2026, 11, 15)))
    assert one_day[ADSET_A1]["meta_reported_leads"]["value"] == 3
    assert one_day[ADSET_A1]["meta_reported_lead_actions"]["value"] == {
        "lead": 3, "leadgen_grouped": 3, "onsite_conversion.lead_grouped": 3}
    # A2 on the 15th: actions present, no lead type. Zero is what Meta said.
    assert one_day[ADSET_A2]["meta_reported_leads"]["value"] == 0


def test_03_a_mismatch_stores_nothing_and_leaves_the_campaign_level_untouched(
        live_db, monkeypatch):
    graph = _pair()
    graph.accounts[OLD].adset_hidden.add((ADSET_A2, "2026-11-16"))
    fakes.configure(monkeypatch, graph)
    campaign, errors = _campaigns()
    assert campaign["status"] == "success"
    before = _campaign_side(live_db)

    detail = sync._sync_meta_ads_adsets(run_id=None, campaign_result=campaign,
                                        errors=errors)
    assert detail["status"] == "partial"
    old = next(a for a in detail["accounts"] if a["account_id"] == OLD)
    assert old["reason"] == "meta_adset_reconciliation_mismatch"
    assert old["failed_coverage_recorded"] is True
    # Nothing stored for the chunk, and the range is recorded failed.
    assert _rows(live_db) == []
    assert _ledger(live_db, OLD) == [
        (OLD, "2026-10-21", "2026-11-17", "failed", 0, 0,
         "meta_adset_reconciliation_mismatch", None)]
    status, count, message, *_ = _batches(live_db)[0]
    assert (status, count) == ("partial", 0)
    assert message.startswith(
        f"meta_adset_reconciliation_mismatch: account {OLD} ad sets: 1 "
        "campaign-day(s) do not reconcile; first 2026-11-16 campaign "
        f"{CAMPAIGN_A}: ad-set rows 35340000 micros, campaign row 60340000")
    # Every campaign table, the campaign batch and its sync_state row are
    # byte-identical to what they were before the ad-set step ran.
    assert _campaign_side(live_db) == before
    assert _truth()["state"] == "verified" and _truth()["usd_spend"] == 130.34
    # The ad-set detail is withheld — for every account.
    out = _detail()
    assert out["status"] == "withheld" and out["rows"] == []
    assert out["reason"] == "meta_adset_coverage_incomplete"
    assert [(a["account_id"], a["adset_coverage"]) for a in out["accounts"]] == [
        (NEW, "verified"), (OLD, "incomplete")]
    assert sync._overall_status({"c": campaign, "a": detail}) == "partial"


def test_04_a_failed_adset_read_never_demotes_either_ledger(live_db, monkeypatch):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    _run()
    rows, ledger = _rows(live_db), _ledger(live_db)

    # The same morning Meta refuses the ad-set read.
    graph.accounts[OLD].adset_refusal = error_response(
        403, 200, message="(#200) Requires ads_read on the ad sets.")
    campaign, errors = _campaigns()
    before = _campaign_side(live_db)
    detail = sync._sync_meta_ads_adsets(run_id=None, campaign_result=campaign,
                                        errors=errors)
    old = next(a for a in detail["accounts"] if a["account_id"] == OLD)
    assert old["status"] == "failed" and old["reason"] == "meta_account_not_readable"
    # The verified ad-set chunk stands; its rows are as they were.
    assert old["failed_coverage_recorded"] is False
    assert _ledger(live_db) == ledger and _rows(live_db) == rows
    # The campaign level: nothing moved, and the account is NOT marked
    # unreadable — it was read, at campaign level, a moment ago.
    assert _campaign_side(live_db) == before
    assert q(live_db, "SELECT last_status, not_readable_since "
                      "FROM meta_ads_account_sync_state WHERE account_id = %s",
             (OLD,)) == [("success", None)]
    assert _detail()["status"] == "published"

    # The next morning the window moves a day and the read fails again: the
    # new range is recorded failed in the AD-SET ledger, and only there.
    tomorrow = NOW + timedelta(days=1)
    campaign, detail, _ = _run(tomorrow)
    assert campaign["status"] == "success" and detail["status"] == "partial"
    assert (OLD, "2026-10-22", "2026-11-18", "failed", 0, 0,
            "meta_account_not_readable", None) in _ledger(live_db, OLD)
    assert q(live_db, "SELECT status FROM meta_ads_spend_coverage "
                      "WHERE account_id = %s ORDER BY chunk_start", (OLD,)) == [
        ("verified",), ("verified",)]
    # Campaign spend through the 18th is published; ad-set detail for that
    # day is not, and the days verified yesterday still are.
    late = (date(2026, 11, 10), date(2026, 11, 18))
    assert truth_service.build_meta_ads_spend_truth_for_days(
        late[0], date(2026, 11, 17), settled=True, now=tomorrow)["state"] == "verified"
    out = _detail(late[0], date(2026, 11, 17), now=tomorrow)
    assert out["status"] == "published"
    graph.accounts[NEW].adset_refusal = None
    day_after = NOW + timedelta(days=2)
    _run(day_after)
    out = _detail(late[0], late[1], now=day_after)
    assert out["status"] == "withheld" and out["rows"] == []
    assert out["reason"] == "meta_adset_coverage_incomplete"
    assert truth_service.build_meta_ads_spend_truth_for_days(
        late[0], late[1], settled=True, now=day_after)["state"] == "verified"


def test_05_stale_adset_rows_are_withheld_when_the_campaign_level_is_restated(
        live_db, monkeypatch):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    _run()
    assert _detail()["status"] == "published"

    # Meta restates the 16th (A1: 35.34 -> 36.00). The campaign level reads
    # it; the ad-set read fails that morning.
    old = graph.accounts[OLD]
    old.adset(ADSET_A1, CAMPAIGN_A, "2026-11-16", "36.00", actions=NO_ACTIONS)
    old.adset_refusal = error_response(500, 2, message="Service unavailable")
    fakes.configure(monkeypatch, graph)
    campaign, detail, _ = _run()
    assert campaign["status"] == "success" and detail["status"] == "partial"
    assert _truth()["native_spend"] == 131.0
    # The ad-set ledger still says `verified` — for rows that describe
    # yesterday's campaign figures. The database notices; the ledger cannot.
    assert _ledger(live_db, OLD)[0][3] == "verified"
    out = _detail()
    assert out["status"] == "withheld" and out["rows"] == []
    assert out["reason"] == "meta_adset_reconciliation_mismatch"
    assert "2026-11-16" in out["detail"] and CAMPAIGN_A in out["detail"]
    mismatch = repo.fetch_meta_adset_spend(WEEK[0], WEEK[1], OLD,
                                           tolerance_micros=10_000)["mismatches"]
    assert mismatch == [{"spend_date": "2026-11-16", "campaign_id": CAMPAIGN_A,
                         "adset_micros": 60_340_000,
                         "campaign_micros": 61_000_000}]
    # A range that does not touch the restated day is still published.
    assert _detail(date(2026, 11, 15), date(2026, 11, 15))["status"] == "published"

    # The next good read replaces the range and the detail publishes again.
    old.adset_refusal = None
    _, detail, _ = _run()
    assert detail["status"] == "success"
    out = _detail()
    assert out["status"] == "published"
    assert _by_adset(out)[ADSET_A1]["spend"]["value"] == 76.0
    assert q(live_db, "SELECT days_restated, max_restated_delta_micros "
                      "FROM meta_ads_adset_coverage WHERE account_id = %s",
             (OLD,)) == [(1, 660_000)]


def test_06_a_rename_changes_the_label_and_never_the_identity(live_db, monkeypatch):
    graph = _pair()
    old = graph.accounts[OLD]
    # Spend in early October, before the daily window (which starts 10-21).
    old.adset(ADSET_A1, CAMPAIGN_A, "2026-10-05", "22.00", actions=actions(lead=2))
    fakes.configure(monkeypatch, graph)
    for level in ("campaign", "adset"):
        out = meta.run_backfill(date(2026, 10, 1), None, now=NOW, level=level)
        assert out["status"] == "success", out

    # Renamed in Meta — the ad set and its campaign. The daily sync then
    # re-reads only the last 28 days; Insights returns the NEW names there.
    old.rename_adset(ADSET_A1, "LAL 1% — Gulf")
    old.rename_campaign(CAMPAIGN_A, "Lead Gen | LAL | 2026")
    _, detail, _ = _run()
    assert detail["status"] == "success"
    stored = q(live_db, "SELECT spend_date::text, adset_name, campaign_name "
                        "FROM meta_ads_adset_daily_spend WHERE adset_id = %s "
                        "ORDER BY spend_date", (ADSET_A1,))
    # The row outside the window keeps the name it was last fetched with: a
    # stored name is "what Meta called it when this row was read", no more.
    assert stored == [
        ("2026-10-05", "Look Alike — GCC", LEAD_GEN),
        ("2026-11-15", "LAL 1% — Gulf", "Lead Gen | LAL | 2026"),
        ("2026-11-16", "LAL 1% — Gulf", "Lead Gen | LAL | 2026")]
    # One id, one row in the read function, under its newest label — the
    # spend under the old name is not a second ad set and is not lost.
    out = _detail(date(2026, 10, 1), date(2026, 11, 16))
    assert out["status"] == "published"
    rows = [r for r in out["rows"] if r["adset_id"] == ADSET_A1]
    assert len(rows) == 1
    assert rows[0]["adset_name"] == "LAL 1% — Gulf"
    assert rows[0]["campaign_name"] == "Lead Gen | LAL | 2026"
    assert rows[0]["spend"]["value"] == 97.34            # 22.00 + 40.00 + 35.34
    assert (rows[0]["first_day"], rows[0]["last_day"]) == ("2026-10-05", "2026-11-16")
    # The sibling ad set, never renamed, sits under the same campaign id.
    assert _by_adset(out)[ADSET_A2]["campaign_id"] == CAMPAIGN_A
    assert _by_adset(out)[ADSET_A2]["campaign_name"] == "Lead Gen | LAL | 2026"

    # A row that arrives with no name keeps the newest stored one for its id.
    old.adset_names[ADSET_A1] = None
    _run()
    assert q(live_db, "SELECT DISTINCT adset_name FROM meta_ads_adset_daily_spend "
                      "WHERE adset_id = %s AND spend_date >= '2026-11-01'",
             (ADSET_A1,)) == [("LAL 1% — Gulf",)]


def test_07_a_range_is_replaced_not_merged(live_db, monkeypatch):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    _run()
    # Meta moves the 15th's spend from A2 onto A1 (the day keeps its spend).
    old = graph.accounts[OLD]
    old.remove_adset(ADSET_A2, "2026-11-15")
    old.adset(ADSET_A1, CAMPAIGN_A, "2026-11-15", "70.00",
              actions=actions(**THREE_LEADS))
    _, detail, _ = _run()
    assert detail["status"] == "success"
    day = [r for r in _rows(live_db) if r[1] == "2026-11-15"]
    assert [(r[0], r[2]) for r in day] == [(ADSET_A1, 70_000_000)]
    assert len(_rows(live_db)) == 5


def test_08_the_writer_is_atomic_and_refuses_what_it_cannot_store(
        live_db, monkeypatch):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    _run()
    rows, ledger = _rows(live_db), _ledger(live_db)
    bounds = (date(2026, 10, 21), date(2026, 11, 17))

    def row(**kw):
        return {"account_id": OLD, "adset_id": ADSET_A1, "campaign_id": CAMPAIGN_A,
                "spend_date": "2026-11-15", "cost_micros": 1_000_000,
                "meta_reported_lead_actions": {}, **kw}

    for bad in ([row(), row()],                              # the same day twice
                [row(spend_date="2026-12-01")],              # outside the range
                [row(cost_micros=-1)], [row(cost_micros=1.5)],
                [row(account_id=NEW)], [row(adset_id="")], [row(campaign_id=None)],
                [row(meta_reported_lead_actions={"lead": "3"})], [row(meta_reported_lead_actions={"lead": -1})],
                [row(meta_reported_lead_actions=["lead"])], [row(meta_reported_lead_actions={"lead": True})]):
        with pytest.raises(db_writers.MetaAdsPersistenceError):
            db_writers.replace_meta_adset_chunk(OLD, *bounds, bad)
        assert _rows(live_db) == rows and _ledger(live_db) == ledger

    # Passes validation, then PostgreSQL refuses it — AFTER the range's
    # DELETE has run inside the transaction. The delete is rolled back.
    with pytest.raises(db_writers.MetaAdsPersistenceError):
        db_writers.replace_meta_adset_chunk(
            OLD, *bounds, [row(cost_micros=2 ** 63)], allow_zero_restatement=True)
    assert _rows(live_db) == rows and _ledger(live_db) == ledger

    # A re-read that would take a day with verified ad-set spend to nothing
    # is refused; only the explicit override accepts it.
    with pytest.raises(db_writers.MetaRestatementImplausible):
        db_writers.replace_meta_adset_chunk(OLD, *bounds, [])
    assert _rows(live_db) == rows and _ledger(live_db) == ledger
    written = db_writers.replace_meta_adset_chunk(OLD, *bounds, [],
                                                  allow_zero_restatement=True)
    assert written["deleted"] == 6 and written["days_zeroed"] == 3
    assert _rows(live_db) == []
    # The stored value must be an object or NULL — never a bare number.
    with pytest.raises(Exception):
        q(live_db, "INSERT INTO meta_ads_adset_daily_spend (account_id, adset_id, "
                   "campaign_id, spend_date, cost_micros, meta_reported_lead_actions) "
                   "VALUES ('1', '2', '3', '2026-01-01', 0, '0'::jsonb)")


def test_09_one_account_short_withholds_every_account(live_db, monkeypatch):
    # Both accounts deliver: the new one in GBP, the old one in USD.
    graph = _pair()
    new = graph.accounts[NEW]
    new.adset(ADSET_B1, CAMPAIGN_B, "2026-11-15", "80.00", name="UK — Prospecting",
              campaign_name="UK Leads", actions=actions(lead=2), clicks=None)
    new.adset(ADSET_B1, CAMPAIGN_B, "2026-11-16", "20.00", actions=actions(lead=1))
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch, rate=1.25)
    campaign, detail, _ = _run()
    assert campaign["status"] == "success" and detail["status"] == "success"
    out = _detail()
    assert out["status"] == "published" and out["mixed_currencies"] is True
    uk = next(r for r in out["rows"] if r["account_id"] == NEW)
    # Native in GBP; USD at each day's own rate.
    assert uk["spend"] == {"value": 100.0, "status": "published", "reason": None,
                           "currency": "GBP"}
    assert uk["spend_usd"]["value"] == 125.0 and uk["spend_usd"]["currency"] == "USD"
    # One day's row carried no `clicks`: unknown for the range, never 25.
    assert uk["clicks"] == {"value": None, "status": "unavailable",
                            "reason": "meta_count_missing_on_some_days"}
    assert uk["impressions"]["value"] == 2000
    assert uk["meta_reported_lead_actions"]["value"] == {"lead": 3}

    # A spend day with no USD rate: the native figure stands, USD does not.
    q(live_db, "DELETE FROM fx_rates WHERE rate_date = '2026-11-16' RETURNING 1")
    uk = next(r for r in _detail()["rows"] if r["account_id"] == NEW)
    assert uk["spend"]["value"] == 100.0
    assert uk["spend_usd"] == {"value": None, "status": "unavailable",
                               "reason": "fx_rates_missing", "currency": "USD"}

    # The next two mornings the OLD account's ad-set read fails. Its ad sets
    # stay verified through the 17th (the first run's window); its campaign
    # level moves on to the 18th and 19th.
    _fx(monkeypatch, rate=1.25)
    graph.accounts[OLD].adset_refusal = error_response(500, 2)
    tomorrow = NOW + timedelta(days=1)
    campaign, detail, _ = _run(tomorrow)
    assert campaign["status"] == "success" and detail["status"] == "partial"
    through = date(2026, 11, 18)
    assert _detail(WEEK[0], date(2026, 11, 17), now=tomorrow)["status"] == "published"
    # Through the 18th the new account is verified at both levels and the old
    # one is short at ad-set level only: NOTHING is returned, for either.
    day_after = NOW + timedelta(days=2)
    graph.accounts[OLD].adset_refusal = error_response(500, 2)
    _run(day_after)
    out = _detail(WEEK[0], through, now=day_after)
    assert truth_service.build_meta_ads_spend_truth_for_days(
        WEEK[0], through, settled=True, now=day_after)["state"] == "verified"
    assert out["status"] == "withheld" and out["rows"] == []
    assert {a["account_id"]: a["adset_coverage"] for a in out["accounts"]} == {
        NEW: "verified", OLD: "incomplete"}
    assert OLD in out["detail"]


def test_10_which_accounts_a_range_needs_is_the_campaign_truths_decision(
        live_db, monkeypatch):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    _run()
    # The campaign truth withholds a range that reaches past what is read —
    # and the ad-set detail then publishes nothing, whatever its own ledger.
    out = _detail(WEEK[0], date(2026, 11, 20))
    assert out["status"] == "withheld"
    assert out["reason"] == "meta_campaign_spend_not_published"
    assert out["campaign_spend_state"] == "coverage_incomplete" and out["rows"] == []
    # The old account dropped from the configuration is still required.
    fakes.unconfigure(monkeypatch)
    monkeypatch.setenv("META_API_VERSION", fakes.VERSION)
    monkeypatch.setenv("META_ADS_ACCESS_TOKEN", SENTINEL_TOKEN)
    monkeypatch.setenv("META_AD_ACCOUNT_ID", NEW)
    later = NOW + timedelta(days=40)
    asked = (date(2026, 12, 1), date(2026, 12, 5))
    base = truth_service.build_meta_ads_spend_truth_for_days(*asked, now=later)
    assert base["state"] == "coverage_incomplete"
    assert _detail(*asked, settled=False, now=later)["status"] == "withheld"
    # Declared retired by the owner (it is no longer read): a range within its
    # verified days is published as measured, with the same accounts and the
    # same required days the campaign truth names.
    monkeypatch.setenv("META_AD_ACCOUNT_RETIRED", f"{OLD}:2026-11-17")
    base = _truth()
    out = _detail()
    assert out["status"] == "published"
    assert [(a["account_id"], a["required_through"]) for a in out["accounts"]] == [
        (a["account_id"], a["required_through"]) for a in base["accounts"]]
    assert out["owner_declared_retired"] == base["owner_declared_retired"]
    assert out["owner_declared_retired_disclosure"] == \
        base["owner_declared_retired_disclosure"]
    # Unreadable storage is unavailable — not an empty breakdown.
    monkeypatch.setattr(repo, "fetch_meta_adset_coverage",
                        lambda *a, **k: {"available": False, "chunks": []})
    out = _detail()
    assert out["status"] == "unavailable" and out["rows"] == []
    assert out["reason"] == "meta_adset_source_unavailable"


def test_11_all_time_counts_from_the_first_campaign_day_not_the_first_adset_day(
        live_db, monkeypatch):
    graph = _pair()
    graph.accounts[OLD].adset(ADSET_A1, CAMPAIGN_A, "2026-10-05", "22.00")
    fakes.configure(monkeypatch, graph)
    assert meta.run_backfill(date(2026, 10, 1), None, now=NOW)["status"] == "success"
    # Ad sets backfilled for November only.
    out = meta.run_backfill(date(2026, 11, 1), None, now=NOW, level="adset")
    assert out["status"] == "success"
    assert _truth(None, date(2026, 11, 16), settled=False)["state"] == "verified"
    # All time: the campaign history starts in October; the ad-set history
    # does not. The ad-set rows would not add up to the all-time spend.
    whole = _detail(None, date(2026, 11, 16), settled=False)
    assert whole["status"] == "withheld" and whole["rows"] == []
    assert whole["reason"] == "meta_adset_coverage_incomplete"
    assert _detail(date(2026, 11, 1), date(2026, 11, 16),
                   settled=False)["status"] == "published"
    assert meta.run_backfill(date(2026, 10, 1), None, now=NOW, level="adset",
                             resume=True)["status"] == "success"
    whole = _detail(None, date(2026, 11, 16), settled=False)
    assert whole["status"] == "published"
    assert _by_adset(whole)[ADSET_A1]["spend"]["value"] == 97.34


def test_12_an_adset_backfill_writes_campaign_tables_only_through_the_campaign_sync(
        live_db, monkeypatch):
    # PR-ADS-171D changed this contract. Under 171C the ad-set backfill wrote
    # ONLY the two ad-set tables and a month without a stored campaign base
    # failed `meta_adset_campaign_base_unverified`. Now each month re-reads
    # its campaign level first through the campaign chunk sync (the same
    # reconciliation, atomic replace and zero-restatement refusal), so the
    # campaign tables change exactly as a campaign backfill of the same month
    # would change them — and in no other way.
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    out = meta.run_backfill(date(2026, 11, 1), None, now=NOW, level="adset",
                            account_ids=[OLD])
    assert out["status"] == "success", out
    assert len(_rows(live_db)) == 6
    assert _ledger(live_db) == [(OLD, "2026-11-01", "2026-11-17", "verified", 6,
                                 142_340_000, None, 1)]
    # The account is stored as the campaign backfill stores it (identity
    # guard); no account sync state is written on success.
    assert q(live_db, "SELECT account_id, currency_code, time_zone_name "
                      "FROM meta_ads_account") == [(OLD, "USD", "Asia/Amman")]
    assert q(live_db, "SELECT COUNT(*) FROM meta_ads_account_sync_state") == [(0,)]

    # What the campaign side holds after an ad-set backfill is what a
    # campaign backfill of the same months holds: same rows, same ledger.
    def campaign_content():
        return (q(live_db, "SELECT account_id, campaign_id, spend_date::text, "
                           "cost_micros FROM meta_ads_campaign_daily_spend "
                           "ORDER BY 1, 2, 3"),
                q(live_db, "SELECT account_id, spend_date::text, cost_micros "
                           "FROM meta_ads_account_daily_spend ORDER BY 1, 2"),
                q(live_db, "SELECT account_id, chunk_start::text, chunk_end::text, "
                           "status, rows_written, cost_micros_total "
                           "FROM meta_ads_spend_coverage ORDER BY 1, 2, 3"))
    assert meta.run_backfill(date(2026, 10, 1), None, now=NOW)["status"] == "success"
    before = campaign_content()
    plan = meta.run_backfill(date(2026, 10, 1), None, now=NOW, level="adset",
                             dry_run=True)
    assert [c["status"] for a in plan["accounts"] for c in a["chunks"]] == [
        "planned"] * 4
    assert len(_ledger(live_db)) == 1

    out = meta.run_backfill(date(2026, 10, 1), None, now=NOW, level="adset")
    assert out["status"] == "success" and out["level"] == "adset"
    assert [(r[0], r[1], r[2], r[3], r[4]) for r in _ledger(live_db)] == [
        (NEW, "2026-10-01", "2026-10-31", "verified", 0),
        (NEW, "2026-11-01", "2026-11-16", "verified", 0),
        (OLD, "2026-10-01", "2026-10-31", "verified", 0),
        (OLD, "2026-11-01", "2026-11-17", "verified", 6)]
    assert campaign_content() == before
    # Still a backfill: no batch, no sync_state, no run.
    assert q(live_db, "SELECT COUNT(*) FROM sync_batches") == [(0,)]
    assert q(live_db, "SELECT COUNT(*) FROM sync_state") == [(0,)]
    assert q(live_db, "SELECT COUNT(*) FROM runs") == [(0,)]

    calls = len(graph.insight_calls("adset"))
    again = meta.run_backfill(date(2026, 10, 1), None, now=NOW, level="adset",
                              resume=True)
    assert [c["status"] for a in again["accounts"] for c in a["chunks"]] == [
        "already_verified"] * 4
    assert len(graph.insight_calls("adset")) == calls
    # The default command is still the campaign level and touches no ad set.
    ledger = _ledger(live_db)
    assert meta.run_backfill(date(2026, 11, 1), None, now=NOW)["status"] == "success"
    assert _ledger(live_db) == ledger and len(graph.insight_calls("adset")) == calls
    _no_token(out, plan, again)


def test_13_a_zone_change_with_no_stored_spend_clears_the_adset_ledger_too(
        live_db, monkeypatch):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    _run()
    # The new account has no spend row; its ad-set ledger says "nothing", on
    # days of the Los Angeles zone.
    assert _ledger(live_db, NEW)[0][3] == "verified"
    graph.accounts[NEW].account["timezone_name"] = "Europe/London"
    meta.refresh_account(NEW)
    assert _ledger(live_db, NEW) == []
    assert len(_ledger(live_db, OLD)) == 1 and len(_rows(live_db)) == 6


def test_14_the_schema_holds_what_the_registry_names(live_db):
    from services.freshness_service import DATASET_FRESHNESS_CONFIG
    config = DATASET_FRESHNESS_CONFIG["meta_ads_adsets"]
    columns = {r[0]: r[1] for r in q(
        live_db, "SELECT column_name, data_type FROM information_schema.columns "
                 "WHERE table_name = %s", (config["table"],))}
    assert columns[config["date_column"]] == "date"
    assert columns["meta_reported_lead_actions"] == "jsonb" and columns["cost_micros"] == "bigint"
    assert columns["adset_id"] == "text" and columns["campaign_id"] == "text"
    assert "meta_reported_leads" not in columns      # no named lead column yet
    unique = q(live_db, "SELECT indexdef FROM pg_indexes WHERE tablename = %s "
                        "AND indexdef LIKE '%%UNIQUE%%'", (config["table"],))
    assert any("(account_id, adset_id, spend_date)" in r[0] for r in unique)
    assert q(live_db, "SELECT COUNT(*) FROM information_schema.tables "
                      "WHERE table_name = 'meta_ads_adset_coverage'") == [(1,)]


# ── review round 1: a configured type that is never carried is not a zero ──

@pytest.mark.parametrize("configured", ["Lead", "offsite_conversion.custom.123"])
def test_15_a_type_the_stored_rows_never_carry_never_publishes_zero(
        live_db, monkeypatch, configured):
    # The old account stores {"lead": …}. "Lead" (wrong case) and a custom
    # conversion id (never stored: no "lead" in it) are both absent from
    # every row — which says nothing about what Meta reported.
    fakes.configure(monkeypatch, _pair())
    _run()
    monkeypatch.setattr(adsets, "native_lead_action_type", lambda: configured)
    out = _detail()
    assert out["status"] == "published" and out["rows"]
    for row in out["rows"]:
        assert row["meta_reported_leads"]["value"] is None, row["adset_id"]
        assert row["meta_reported_leads"]["status"] == "unavailable"
    a2 = _by_adset(out)[ADSET_A2]["meta_reported_leads"]
    if configured == "Lead":
        assert a2 == {"value": None, "status": "unavailable",
                      "reason": "meta_lead_action_type_not_observed",
                      "action_type": "Lead"}
        assert out["config_problems"] == []
        assert [a["meta_reported_leads_type_observed"] for a in out["accounts"]
                if a["account_id"] == OLD] == [False]
    else:
        assert a2["reason"] == "meta_lead_action_type_not_stored"
        assert [p["reason"] for p in out["config_problems"]] == [
            "meta_lead_action_type_not_stored"]
        # Refused, and never echoed: a fixed message only.
        assert out["native_lead_action_type"] is None
        assert configured not in json.dumps(out, default=str)


def test_16_a_type_observed_on_one_day_is_a_zero_on_a_day_it_is_absent(
        live_db, monkeypatch):
    graph = FakeGraph()
    old = graph.add_account(OLD)
    old.adset(ADSET_A1, CAMPAIGN_A, "2026-11-15", "40.00",
              actions=actions(lead=2, link_click=9))
    old.adset(ADSET_A1, CAMPAIGN_A, "2026-11-16", "35.00",
              actions=actions(link_click=7))
    fakes.configure(monkeypatch, graph)
    _run()
    monkeypatch.setattr(adsets, "native_lead_action_type", lambda: "lead")
    day2 = _detail(date(2026, 11, 16), date(2026, 11, 16))
    assert day2["status"] == "published"
    assert _by_adset(day2)[ADSET_A1]["meta_reported_leads"] == {
        "value": 0, "status": "published", "reason": None, "action_type": "lead"}
    assert _by_adset(_detail())[ADSET_A1]["meta_reported_leads"]["value"] == 2


def test_17_an_account_that_never_emitted_the_type_is_withheld_alone(
        live_db, monkeypatch):
    # Two accounts in one range. The old one has emitted "lead"; the new one
    # has `actions` on every row and never "lead". Its ad sets are not 0.
    graph = _pair()
    new_adset, new_campaign = "120219876543210099", "120211234567890199"
    graph.default.adset(new_adset, new_campaign, "2026-11-15", "10.00",
                        actions=actions(link_click=5))
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch)
    _run()
    monkeypatch.setattr(adsets, "native_lead_action_type", lambda: "lead")
    out = _detail()
    assert out["status"] == "published"
    rows = _by_adset(out)
    assert rows[new_adset]["account_id"] == NEW
    assert rows[new_adset]["meta_reported_leads"] == {
        "value": None, "status": "unavailable",
        "reason": "meta_lead_action_type_not_observed", "action_type": "lead"}
    assert rows[ADSET_A2]["meta_reported_leads"]["value"] == 1
    observed = {a["account_id"]: a["meta_reported_leads_type_observed"]
                for a in out["accounts"]}
    assert observed == {NEW: False, OLD: True}
