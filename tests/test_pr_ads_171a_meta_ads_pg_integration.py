"""PR-ADS-171A — Meta Ads spend on a real PostgreSQL schema.

The real sync step, writers, repository, truth function and System page
source, against the fake Graph API (tests/meta_ads_fakes.py), whose accounts
are the two production ones as Meta described them on 2026-11-22:

* 4730305973961806 "Logistaas Limited" — GBP, America/Los_Angeles, the new
  account (credential set 1), no delivery yet;
* 944370805110952 "Logistaas" — USD, Asia/Amman, the old account with the
  running ads, under its own token (credential set 2).

Every case needs the database; a skipped module is not merge evidence.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
import requests

import db.writers as db_writers
import scheduler.incremental_sync as sync
from services import fx_service
from services import meta_ads_spend_service as meta
from services import meta_ads_spend_truth_service as truth_service
from services import system_overview_service as overview
from tests import meta_ads_fakes as fakes
from tests.meta_ads_fakes import (
    ACCOUNT_ID, CAMPAIGN_A, CAMPAIGN_B, CAMPAIGN_C, OLD_ACCOUNT_ID,
    SENTINEL_TOKEN, SENTINEL_TOKEN_2, FakeGraph, error_response,
)

# 06:00 UTC, the daily sync. In Los Angeles it is 22:00 on the 17th: the last
# complete day there is the 16th. In Amman it is 09:00 on the 18th: the 17th.
NOW = datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)
YESTERDAY = date(2026, 11, 16)            # Los Angeles
OLD_YESTERDAY = date(2026, 11, 17)        # Amman
WINDOW_START = date(2026, 10, 20)
WEEK = (date(2026, 11, 10), YESTERDAY)
NEW, OLD = ACCOUNT_ID, OLD_ACCOUNT_ID


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


def _seeded(**kwargs) -> FakeGraph:
    """One account — the new one — WITH spend, to exercise the GBP paths."""
    graph = FakeGraph(**kwargs)
    graph.spend(CAMPAIGN_A, "2026-11-15", "120.50", name="Lead Gen — MENA")
    graph.spend(CAMPAIGN_A, "2026-11-16", "98.10", name="Lead Gen — MENA")
    graph.spend(CAMPAIGN_B, "2026-11-16", "0.10", name="Retargeting — EU")
    graph.spend(CAMPAIGN_A, "2026-11-17", "55.00", name="Lead Gen — MENA")  # in progress
    return graph


def _pair() -> FakeGraph:
    """Production: the new account has no delivery; the old one has the ads."""
    graph = FakeGraph()
    old = graph.add_account(OLD)
    old.spend(CAMPAIGN_A, "2026-11-15", "70.00", name="Lead Gen — MENA")
    old.spend(CAMPAIGN_A, "2026-11-16", "60.34", name="Lead Gen — MENA")
    old.spend(CAMPAIGN_B, "2026-11-17", "12.00", name="Retargeting — GCC")
    old.spend(CAMPAIGN_A, "2026-11-18", "9.99", name="Lead Gen — MENA")  # in progress
    return graph


def _fx(monkeypatch, rate=1.25, fail=()):
    """The FX provider: GBP→USD at ``rate``; the dates in ``fail`` error."""
    calls = []

    def fetch(rate_date, base, quote):
        calls.append((rate_date, base, quote))
        if rate_date in fail:
            raise RuntimeError("FX provider unavailable")
        return {"rate_date": rate_date, "base_currency": base,
                "quote_currency": quote, "rate": rate, "provider": "test",
                "source_version": "test", "effective_date": rate_date}
    monkeypatch.setattr(fx_service, "fetch_fx_rate", fetch)
    return calls


def _run(now=NOW):
    errors = []
    return sync._sync_meta_ads_spend(run_id=None, now=now, errors=errors), errors


def _rows(db, account_id=None):
    return q(db, "SELECT campaign_id, spend_date::text, cost_micros, spend_raw, "
                 "campaign_name, currency_code FROM meta_ads_campaign_daily_spend "
                 "WHERE (%s::text IS NULL OR account_id = %s) "
                 "ORDER BY account_id, spend_date, campaign_id",
             (account_id, account_id))


def _coverage(db, account_id=None):
    return q(db, "SELECT chunk_start::text, chunk_end::text, status, rows_written, "
                 "cost_micros_total, account_time_zone, error_code, days_restated, "
                 "max_restated_delta_micros, fetched_at, pages_read, http_status "
                 "FROM meta_ads_spend_coverage "
                 "WHERE (%s::text IS NULL OR account_id = %s) "
                 "ORDER BY account_id, chunk_start, chunk_end",
             (account_id, account_id))


def _batches(db):
    return q(db, "SELECT sync_type, status, row_count, error_message, "
                 "date_from::text, date_to::text FROM sync_batches "
                 "WHERE source = 'meta_ads_api' AND dataset = 'meta_campaign_spend' "
                 "ORDER BY id")


def _sync_state(db):
    return {r[0]: r[1:] for r in q(
        db, "SELECT account_id, last_status, last_reason, last_http_status, "
            "last_error_code, not_readable_since, last_success_at "
            "FROM meta_ads_account_sync_state")}


def _truth(start=WEEK[0], end=WEEK[1], settled=True, now=NOW):
    return truth_service.build_meta_ads_spend_truth_for_days(
        start, end, settled=settled, now=now)


def _dump(db) -> str:
    out = []
    for table in ("meta_ads_account", "meta_ads_campaign_daily_spend",
                  "meta_ads_account_daily_spend", "meta_ads_spend_coverage",
                  "meta_ads_account_sync_state", "sync_batches", "sync_state",
                  "runs"):
        out.append(json.dumps(q(db, f"SELECT * FROM {table}"), default=str))
    return "\n".join(out)


def _no_token(*things):
    text = "\n".join(t if isinstance(t, str) else json.dumps(t, default=str)
                     for t in things)
    for token in (SENTINEL_TOKEN, SENTINEL_TOKEN_2):
        assert token not in text


SEEDED = [
    (CAMPAIGN_A, "2026-11-15", 120_500_000, "120.50", "Lead Gen — MENA", "GBP"),
    (CAMPAIGN_A, "2026-11-16", 98_100_000, "98.10", "Lead Gen — MENA", "GBP"),
    (CAMPAIGN_B, "2026-11-16", 100_000, "0.10", "Retargeting — EU", "GBP"),
]
OLD_ROWS = [
    (CAMPAIGN_A, "2026-11-15", 70_000_000, "70.00", "Lead Gen — MENA", "USD"),
    (CAMPAIGN_A, "2026-11-16", 60_340_000, "60.34", "Lead Gen — MENA", "USD"),
    (CAMPAIGN_B, "2026-11-17", 12_000_000, "12.00", "Retargeting — GCC", "USD"),
]


# ── the two production accounts, end to end ───────────────────────────────

def test_01_the_0600_utc_sync_of_the_two_production_accounts(live_db, monkeypatch):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    fx_calls = _fx(monkeypatch)
    out, errors = _run()
    assert out["status"] == "success" and errors == []

    # Each account's range ends on ITS OWN yesterday.
    assert _rows(live_db, NEW) == []                 # no delivery: nothing stored
    assert _rows(live_db, OLD) == OLD_ROWS           # the 18th (in progress) is absent
    assert q(live_db, "SELECT account_id, account_name, currency_code, "
                      "time_zone_name FROM meta_ads_account ORDER BY account_id") == [
        (NEW, "Logistaas Limited", "GBP", "America/Los_Angeles"),
        (OLD, "Logistaas", "USD", "Asia/Amman")]
    (new_cov,), (old_cov,) = _coverage(live_db, NEW), _coverage(live_db, OLD)
    assert new_cov[:6] == ("2026-10-20", "2026-11-16", "verified", 0, 0,
                           "America/Los_Angeles")
    assert old_cov[:6] == ("2026-10-21", "2026-11-17", "verified", 3, 142_340_000,
                           "Asia/Amman")

    # One batch: the exact status, the database's row count, and a watermark
    # at the EARLIER of the two last days.
    assert _batches(live_db) == [
        ("daily", "success", 3, None, "2026-10-20", "2026-11-17")]
    assert out["rows_written"] == 3 == q(
        live_db, "SELECT COUNT(*) FROM meta_ads_campaign_daily_spend")[0][0]
    assert q(live_db, "SELECT status, last_source_date::text FROM sync_state "
                      "WHERE source = 'meta_ads_api'") == [("success", "2026-11-16")]
    state = _sync_state(live_db)
    assert {k: v[0] for k, v in state.items()} == {NEW: "success", OLD: "success"}
    assert all(v[4] is None and v[5] is not None for v in state.values())
    assert fx_calls == []                  # USD is an identity; zero needs no rate

    # The truth: mixed zones, one total, each account settled in its own zone.
    truth = _truth()
    assert truth["state"] == "verified" and truth["usd_spend"] == 130.34
    assert truth["mixed_time_zones"] is True and truth["mixed_currencies"] is True
    assert truth["time_zones"] == ["America/Los_Angeles", "Asia/Amman"]
    assert [(a["account_id"], a["account_name"], a["currency"], a["time_zone"],
             a["state"], a["native_spend"], a["usd_spend"])
            for a in truth["accounts"]] == [
        (NEW, "Logistaas Limited", "GBP", "America/Los_Angeles",
         "verified_zero_spend", 0.0, 0.0),
        (OLD, "Logistaas", "USD", "Asia/Amman", "verified", 130.34, 130.34)]
    # Only the USD account spent, so the total has a currency of its own.
    assert (truth["native_currency"], truth["native_spend"]) == ("USD", 130.34)
    # Through the 17th: Amman's has ended and is settled; Los Angeles' has
    # not. The old account's own figure is proven; the total is withheld.
    wider = _truth(end=OLD_YESTERDAY)
    assert wider["state"] == "coverage_incomplete" and wider["usd_spend"] is None
    assert [(a["state"], a["usd_spend"]) for a in wider["accounts"]] == [
        ("coverage_incomplete", None), ("verified", 142.34)]

    # The System page: one line, each account on its own date.
    line = overview._meta_spend_source(NOW)
    assert line["status"] == "current" and line["complete_through"] == "2026-11-16"
    assert [a["summary"] for a in line["accounts"]] == [
        f"Logistaas Limited ({NEW}): complete through 2026-11-16 (America/Los_Angeles)",
        f"Logistaas ({OLD}): complete through 2026-11-17 (Asia/Amman)"]
    assert line["last_success_at"] and line["misconfigured_sets"] == []

    # Each account was read with its own token; neither token is stored.
    assert {c["headers"]["Authorization"] for c in graph.calls
            if c["account_id"] == OLD} == {f"Bearer {SENTINEL_TOKEN_2}"}
    _no_token(_dump(live_db), out, errors, truth, line)


def test_02_a_gbp_account_with_spend_is_priced_through_the_gbp_usd_rates(
        live_db, monkeypatch):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    fx_calls = _fx(monkeypatch)
    out, errors = _run()
    assert out["status"] == "success" and errors == []
    assert _rows(live_db) == SEEDED                  # the 17th is not there
    assert q(live_db, "SELECT spend_date::text, cost_micros, currency_code "
                      "FROM meta_ads_account_daily_spend ORDER BY spend_date") == [
        ("2026-11-15", 120_500_000, "GBP"), ("2026-11-16", 98_200_000, "GBP")]
    (cov,) = _coverage(live_db)
    assert cov[:9] == ("2026-10-20", "2026-11-16", "verified", 3, 218_700_000,
                       "America/Los_Angeles", None, 0, 0)
    assert cov[10] == 2 and cov[11] == 200           # two pages read, HTTP 200
    assert sorted(fx_calls) == [("2026-11-15", "GBP", "USD"),
                                ("2026-11-16", "GBP", "USD")]
    truth = _truth()
    assert truth["state"] == "verified"
    assert (truth["native_currency"], truth["native_spend"], truth["usd_spend"]) == (
        "GBP", 218.7, 273.38)                        # 218.70 × 1.25 = 273.375
    assert truth["accounts"][0]["usd_identity"] is False
    # The UTC date's yesterday was never fetched: not published, not zero.
    through_17th = _truth(end=date(2026, 11, 17))
    assert through_17th["state"] == "coverage_incomplete"
    assert through_17th["usd_spend"] is None and through_17th["native_spend"] is None
    line = overview._meta_spend_source(NOW)
    assert line["status"] == "current" and line["complete_through"] == "2026-11-16"
    assert (line["window_start"], line["window_end"]) == ("2026-11-10", "2026-11-16")


def test_03_the_next_mornings_sync_moves_the_window_one_los_angeles_day(
        live_db, monkeypatch):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch)
    _run()
    out, _ = _run(NOW + timedelta(days=1))
    account = out["accounts"][0]
    assert (account["chunk_start"], account["chunk_end"]) == ("2026-10-21", "2026-11-17")
    assert (CAMPAIGN_A, "2026-11-17", 55_000_000, "55.00", "Lead Gen — MENA",
            "GBP") in _rows(live_db)
    line = overview._meta_spend_source(NOW + timedelta(days=1))
    assert line["status"] == "current" and line["complete_through"] == "2026-11-17"
    # The next morning's sync is not due until 09:00 UTC: still current.
    early = overview._meta_spend_source(NOW + timedelta(days=2, hours=2))
    assert early["status"] == "current" and early["complete_through"] == "2026-11-17"
    # A missed morning reads late once that sync was due, and names the Los
    # Angeles day it lacks.
    late = overview._meta_spend_source(NOW + timedelta(days=2, hours=3))
    assert late["status"] == "late" and late["complete_through"] is None
    assert late["missing_ranges"] == [{"start": "2026-11-18", "end": "2026-11-18"}]
    assert late["accounts"][0]["summary"] == (
        f"Logistaas Limited ({NEW}): late — no spend recorded for 2026-11-18 "
        "(America/Los_Angeles)")


# ── replace, not merge ────────────────────────────────────────────────────

def test_04_a_restated_day_and_a_campaign_day_that_disappears(live_db, monkeypatch):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch)
    _run()
    first = _coverage(live_db)[0]

    graph.spend(CAMPAIGN_A, "2026-11-16", "101.00", name="Lead Gen — MENA")  # restated
    graph.remove(CAMPAIGN_B, "2026-11-16")                                   # gone
    graph.spend(CAMPAIGN_C, "2026-11-14", "5.00", name="Video — GCC")        # late
    out, errors = _run()
    account = out["accounts"][0]
    assert out["status"] == "success" and errors == []
    # Merge-only would have kept campaign B's 0.10 on the 16th.
    assert _rows(live_db) == [
        (CAMPAIGN_C, "2026-11-14", 5_000_000, "5.00", "Video — GCC", "GBP"),
        (CAMPAIGN_A, "2026-11-15", 120_500_000, "120.50", "Lead Gen — MENA", "GBP"),
        (CAMPAIGN_A, "2026-11-16", 101_000_000, "101.00", "Lead Gen — MENA", "GBP")]
    assert q(live_db, "SELECT spend_date::text, cost_micros FROM "
                      "meta_ads_account_daily_spend ORDER BY spend_date") == [
        ("2026-11-14", 5_000_000), ("2026-11-15", 120_500_000),
        ("2026-11-16", 101_000_000)]
    (second,) = _coverage(live_db)                   # the same chunk, re-verified
    assert second[2] == "verified" and second[3] == 3 and second[4] == 226_500_000
    # Two days this chunk had already verified changed: the 14th (0 → 5.00)
    # and the 16th (98.20 → 101.00).
    assert (second[7], second[8]) == (2, 5_000_000)
    assert (account["days_restated"], account["max_restated_delta_micros"]) == (
        2, 5_000_000)
    assert account["rows_deleted"] == 3
    # fetched_at is the instant of THIS verified read.
    assert second[9] > first[9]
    assert _truth()["native_spend"] == 226.5
    assert [b[1] for b in _batches(live_db)] == ["success", "success"]


def test_05_a_failure_mid_transaction_leaves_the_stored_range_as_it_was(
        live_db, monkeypatch):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch)
    _run()
    before_rows, before_cov = _rows(live_db), _coverage(live_db)
    before_days = q(live_db, "SELECT * FROM meta_ads_account_daily_spend ORDER BY id")
    # Passes validation, then PostgreSQL refuses it — AFTER the range's DELETE
    # has run inside the transaction.
    poison = [{"account_id": NEW, "campaign_id": CAMPAIGN_C,
               "campaign_name": "x", "spend_date": "2026-11-16",
               "cost_micros": 2 ** 63, "spend_raw": "9223372036854.775808",
               "currency_code": "GBP", "impressions": 1, "clicks": 1}]
    with pytest.raises(db_writers.MetaAdsPersistenceError):
        db_writers.replace_meta_campaign_spend_chunk(
            NEW, WINDOW_START, YESTERDAY, poison, [],
            currency_code="GBP", account_time_zone="America/Los_Angeles")
    assert _rows(live_db) == before_rows
    assert _coverage(live_db) == before_cov
    assert q(live_db, "SELECT * FROM meta_ads_account_daily_spend ORDER BY id") == before_days


@pytest.mark.parametrize("bad", [
    {"account_id": OLD}, {"campaign_id": ""}, {"spend_date": "2026-11-17"},
    {"cost_micros": -1}, {"cost_micros": 1.5}, {"cost_micros": None},
    {"cost_micros": True},
])
def test_06_an_invalid_row_is_refused_before_anything_is_deleted(
        live_db, monkeypatch, bad):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch)
    _run()
    row = {"account_id": NEW, "campaign_id": CAMPAIGN_A,
           "campaign_name": "x", "spend_date": "2026-11-16", "cost_micros": 1,
           "spend_raw": "0.000001", "currency_code": "GBP", **bad}
    with pytest.raises(db_writers.MetaAdsPersistenceError):
        db_writers.replace_meta_campaign_spend_chunk(
            NEW, WINDOW_START, YESTERDAY, [row], [])
    assert _rows(live_db) == SEEDED


def test_07_a_row_without_a_name_keeps_the_stored_name(live_db, monkeypatch):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch)
    _run()

    def row(campaign_id, name, day="2026-11-16"):
        return {"account_id": NEW, "campaign_id": campaign_id,
                "campaign_name": name, "spend_date": day,
                "cost_micros": 1_000_000, "spend_raw": "1.00",
                "currency_code": "GBP"}
    # (The 15th keeps a row: a re-read may not take a verified day to zero.)
    out = db_writers.replace_meta_campaign_spend_chunk(
        NEW, WINDOW_START, YESTERDAY,
        [row(CAMPAIGN_A, "Lead Gen — MENA", "2026-11-15"),
         row(CAMPAIGN_A, None), row(CAMPAIGN_B, "  "), row(CAMPAIGN_C, None)],
        [{"spend_date": "2026-11-15", "cost_micros": 1_000_000},
         {"spend_date": "2026-11-16", "cost_micros": 3_000_000}],
        currency_code="GBP")
    assert out["names_preserved"] == 2 and out["written"] == 4
    assert [(r[0], r[1], r[4]) for r in _rows(live_db)] == [
        (CAMPAIGN_A, "2026-11-15", "Lead Gen — MENA"),
        (CAMPAIGN_A, "2026-11-16", "Lead Gen — MENA"),
        (CAMPAIGN_B, "2026-11-16", "Retargeting — EU"),
        (CAMPAIGN_C, "2026-11-16", None)]            # never named: stays unknown


def test_08_a_replace_of_one_account_never_touches_another(live_db, monkeypatch):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch)
    _run()
    # The same campaign id, the same day, in the OTHER account.
    db_writers.replace_meta_campaign_spend_chunk(
        NEW, WINDOW_START, YESTERDAY,
        [{"account_id": NEW, "campaign_id": CAMPAIGN_A, "campaign_name": None,
          "spend_date": "2026-11-16", "cost_micros": 5_000_000,
          "spend_raw": "5.00", "currency_code": "GBP"}],
        [{"spend_date": "2026-11-16", "cost_micros": 5_000_000}],
        currency_code="GBP")
    assert _rows(live_db, OLD) == OLD_ROWS
    # A name is kept per account: the old account's name is not borrowed.
    assert [(r[0], r[4]) for r in _rows(live_db, NEW)] == [(CAMPAIGN_A, None)]
    db_writers.replace_meta_campaign_spend_chunk(
        NEW, WINDOW_START, YESTERDAY, [], [], allow_zero_restatement=True)
    assert _rows(live_db, NEW) == [] and _rows(live_db, OLD) == OLD_ROWS


# ── zero, and unknown ─────────────────────────────────────────────────────

def test_09_no_delivery_is_a_verified_zero_and_an_unfetched_range_is_not(
        live_db, monkeypatch):
    """Production on 2026-11-22 answered `{'data': []}` for the new account."""
    graph = FakeGraph()
    fakes.configure(monkeypatch, graph)
    fx_calls = _fx(monkeypatch)
    out, errors = _run()
    assert out["status"] == "success" and errors == []
    assert out["accounts"][0]["verified_zero"] is True
    assert _rows(live_db) == []
    assert q(live_db, "SELECT COUNT(*) FROM meta_ads_account_daily_spend") == [(0,)]
    (cov,) = _coverage(live_db)
    assert cov[:5] == ("2026-10-20", "2026-11-16", "verified", 0, 0)
    assert _batches(live_db) == [
        ("daily", "success", 0, None, "2026-10-20", "2026-11-16")]
    assert fx_calls == []
    zero = _truth()
    assert zero["state"] == "verified_zero_spend"
    assert (zero["native_spend"], zero["usd_spend"], zero["native_currency"]) == (
        0.0, 0.0, "GBP")
    # September was never fetched: unknown, not zero.
    unknown = _truth(date(2026, 9, 1), date(2026, 9, 30))
    assert unknown["state"] == "coverage_incomplete"
    assert unknown["native_spend"] is None and unknown["usd_spend"] is None
    assert overview._meta_spend_source(NOW)["status"] == "current"


def test_10_an_empty_200_never_deletes_verified_spend(live_db, monkeypatch):
    """Review item 4. Meta answers 200 with no rows at BOTH levels for an
    account that has verified, non-zero spend stored. Nothing reconciles with
    nothing, so reconciliation passes — and the range would be emptied and
    called verified. It is refused instead; the other account still syncs."""
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch)
    _run()
    rows, ledger = _rows(live_db, OLD), _coverage(live_db, OLD)
    days = q(live_db, "SELECT * FROM meta_ads_account_daily_spend ORDER BY id")
    assert rows == OLD_ROWS

    graph.accounts[OLD].rows.clear()                   # 200, `data: []`, both levels
    out, errors = _run()
    assert out["status"] == "partial" and out["reason"] == "meta_restatement_implausible"
    assert [a["status"] for a in out["accounts"]] == ["success", "failed"]
    assert errors == [
        f"meta_ads_api/meta_campaign_spend: account {OLD}: 3 day(s) with verified "
        "spend would become zero (first 2026-11-15: 70000000 micros stored); "
        "nothing was changed"]
    assert _rows(live_db, OLD) == rows                 # stored spend untouched
    assert _coverage(live_db, OLD) == ledger           # the verified chunk stands
    assert q(live_db, "SELECT * FROM meta_ads_account_daily_spend ORDER BY id") == days
    assert _batches(live_db)[-1][1] == "partial"
    assert _sync_state(live_db)[OLD][:2] == ("failed", "meta_restatement_implausible")
    assert _truth(end=OLD_YESTERDAY - timedelta(days=1))["usd_spend"] == 130.34

    # One day going to zero is refused just the same...
    old = graph.accounts[OLD]
    old.spend(CAMPAIGN_A, "2026-11-15", "70.00").spend(CAMPAIGN_A, "2026-11-16", "60.34")
    out, _ = _run()                                    # the 17th is now absent
    assert out["accounts"][1]["reason"] == "meta_restatement_implausible"
    assert "1 day(s) with verified spend would become zero (first 2026-11-17" in (
        out["accounts"][1]["error"])
    assert _rows(live_db, OLD) == rows
    # ...while an ordinary restatement — amounts changing, a campaign-day
    # going while its day keeps spend — still passes.
    old.spend(CAMPAIGN_A, "2026-11-17", "3.00").spend(CAMPAIGN_A, "2026-11-16", "61.00")
    out, errors = _run()
    assert out["status"] == "success" and errors == []
    assert _rows(live_db, OLD) == [
        (CAMPAIGN_A, "2026-11-15", 70_000_000, "70.00", "Lead Gen — MENA", "USD"),
        (CAMPAIGN_A, "2026-11-16", 61_000_000, "61.00", "Lead Gen — MENA", "USD"),
        (CAMPAIGN_A, "2026-11-17", 3_000_000, "3.00", "Lead Gen — MENA", "USD")]
    assert out["accounts"][1]["days_restated"] == 2
    assert out["accounts"][1]["days_zeroed"] == 0


def test_10b_only_the_backfills_explicit_flag_accepts_a_zero_restatement(
        live_db, monkeypatch, capsys):
    from scripts import backfill_meta_ads_spend as script
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch)
    args = ["--from", "2026-11-01", "--to", "2026-11-17", "--account", OLD]
    assert script.main(args) == 0
    capsys.readouterr()
    graph.accounts[OLD].rows.clear()
    assert script.main(args) == 1                      # refused without the flag
    refused = json.loads(capsys.readouterr().out)
    assert refused["accounts"][0]["reason"] == "meta_restatement_implausible"
    assert _rows(live_db, OLD) == OLD_ROWS
    assert script.main(args + ["--accept-zero-restatement"]) == 0
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["accounts"][0]["chunks"][0]["days_zeroed"] == 3
    assert _rows(live_db, OLD) == []
    assert [c[:5] for c in _coverage(live_db, OLD)] == [
        ("2026-11-01", "2026-11-17", "verified", 0, 0)]


def test_11_days_without_delivery_between_days_with_spend(live_db, monkeypatch):
    graph = FakeGraph()
    graph.spend(CAMPAIGN_A, "2026-10-21", "10.00")
    graph.spend(CAMPAIGN_A, "2026-11-16", "12.00")
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch)
    out, errors = _run()
    assert out["status"] == "success" and errors == []
    assert [r[1] for r in _rows(live_db)] == ["2026-10-21", "2026-11-16"]
    whole = _truth(WINDOW_START, YESTERDAY)
    assert whole["state"] == "verified" and whole["native_spend"] == 22.0
    silent = _truth(date(2026, 10, 25), date(2026, 11, 10))
    assert silent["state"] == "verified_zero_spend" and silent["usd_spend"] == 0.0


# ── failures ──────────────────────────────────────────────────────────────

def test_12_a_failed_fetch_writes_nothing_and_never_demotes_a_verified_chunk(
        live_db, monkeypatch):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch)
    _run()
    verified = _coverage(live_db)

    # The same morning, re-run with a campaign Meta leaves out of the
    # campaign-level query: the chunk fails reconciliation.
    graph.hidden.add((CAMPAIGN_A, "2026-11-15"))
    out, errors = _run()
    assert out["status"] == "failed" and out["reason"] == "meta_reconciliation_mismatch"
    assert out["accounts"][0]["failed_coverage_recorded"] is False  # verified stands
    assert _coverage(live_db) == verified
    assert _rows(live_db) == SEEDED                  # nothing deleted, nothing written
    assert _batches(live_db)[-1][:3] == ("daily", "failed", 0)
    assert _batches(live_db)[-1][3].startswith(
        f"meta_reconciliation_mismatch: account {NEW}: ")
    assert _truth()["state"] == "verified"

    # The next morning (new bounds) the token has expired.
    graph.hidden.clear()
    graph.queue.append(error_response(400, 190, subcode=463))
    out, errors = _run(NOW + timedelta(days=1))
    assert out["status"] == "failed" and out["reason"] == "meta_token_rejected"
    failed = [c for c in _coverage(live_db) if c[2] == "failed"]
    assert [(c[0], c[1], c[6], c[11]) for c in failed] == [
        ("2026-10-21", "2026-11-17", "meta_token_rejected", 400)]
    assert _rows(live_db) == SEEDED
    assert _batches(live_db)[-1][3] == (
        f"meta_token_rejected: account {NEW}: Meta access token expired or invalid "
        "(HTTP 400, code 190, subcode 463)")
    line = overview._meta_spend_source(NOW + timedelta(days=1))
    assert line["status"] == "token_rejected"
    since = _sync_state(live_db)[NEW][4].date().isoformat()
    assert line["accounts"][0]["summary"] == (
        f"Logistaas Limited ({NEW}): Meta rejected the access token since {since} "
        "(HTTP 400, code 190); history through 2026-11-16 is kept")
    # The proven days stay published; the day the failed run would have added
    # is not.
    assert _truth()["state"] == "verified"
    assert _truth(end=date(2026, 11, 17),
                  now=NOW + timedelta(days=1))["state"] == "coverage_incomplete"
    _no_token(_dump(live_db), line)


def test_13_an_account_suspended_mid_life_keeps_its_history(live_db, monkeypatch):
    """The old account is suspended. Its history is never deleted or
    overwritten; the run is partial while it stays configured; the message is
    plain; and once the owner removes it, its history keeps counting."""
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch)
    _run()
    history, proven = _rows(live_db, OLD), _coverage(live_db, OLD)

    graph.accounts[OLD].refusal = error_response(
        403, 200, message="(#200) Permissions error")
    day2 = NOW + timedelta(days=1)
    out, errors = _run(day2)
    assert out["status"] == "partial" and out["reason"] == "meta_account_not_readable"
    assert [a["status"] for a in out["accounts"]] == ["success", "failed"]
    assert errors == [
        f"meta_ads_api/meta_campaign_spend: account {OLD} is not readable; its "
        "stored history is kept (Meta API refused the request: HTTP 403, code 200, "
        "type OAuthException: (#200) Permissions error)"]
    assert _batches(live_db)[-1][1] == "partial"
    # Nothing of the old account's was deleted or overwritten.
    assert _rows(live_db, OLD) == history
    assert [c for c in _coverage(live_db, OLD) if c[2] == "verified"] == proven
    assert [(c[0], c[1], c[6], c[11]) for c in _coverage(live_db, OLD)
            if c[2] == "failed"] == [
        ("2026-10-22", "2026-11-18", "meta_account_not_readable", 403)]
    state = _sync_state(live_db)[OLD]
    assert state[:4] == ("failed", "meta_account_not_readable", 403, "200")
    since = state[4]
    assert since is not None

    # A second refused morning does not move "since".
    day3 = NOW + timedelta(days=2)
    _run(day3)
    assert _sync_state(live_db)[OLD][4] == since
    line = overview._meta_spend_source(day3)
    assert line["status"] == "not_readable"
    assert line["accounts"][1]["summary"] == (
        f"Logistaas ({OLD}): account not readable since {since.date().isoformat()} "
        "(HTTP 403, code 200); history through 2026-11-17 is kept")
    assert line["accounts"][0]["status"] == "current"

    # While it is still configured, a range past its last verified day is
    # withheld; the days it was verified for still publish.
    withheld = _truth(date(2026, 11, 12), date(2026, 11, 18), now=day3)
    assert withheld["state"] == "coverage_incomplete" and withheld["usd_spend"] is None
    kept = _truth(date(2026, 11, 12), date(2026, 11, 17), now=day3)
    assert kept["state"] == "verified" and kept["usd_spend"] == 142.34

    # Dropping it from the configuration does NOT retire it: nobody has said
    # it stopped spending, so later days stay withheld.
    monkeypatch.delenv("META_ADS_ACCESS_TOKEN_2")
    monkeypatch.delenv("META_AD_ACCOUNT_ID_2")
    graph.calls.clear()
    out, errors = _run(day3)
    assert out["status"] == "success" and errors == []
    assert {c["account_id"] for c in graph.calls} == {NEW}
    assert _rows(live_db, OLD) == history            # still there
    week = (date(2026, 11, 12), date(2026, 11, 18))
    dropped = _truth(*week, now=day3)
    assert dropped["state"] == "coverage_incomplete" and dropped["usd_spend"] is None
    assert dropped["owner_declared_retired"] == []
    line = overview._meta_spend_source(day3)
    assert line["status"] == "account_unresolved"
    assert line["accounts"][1]["summary"] == (
        f"Logistaas ({OLD}): not in the configuration, and it is not declared "
        "retired; history through 2026-11-17 is kept; later periods are withheld")

    # A declaration the ledger does not reach is not honoured either.
    monkeypatch.setenv("META_AD_ACCOUNT_RETIRED", f"{OLD}:2026-11-18")
    early = _truth(*week, now=day3)
    assert early["state"] == "coverage_incomplete" and early["usd_spend"] is None
    assert ("declared to have last spent on 2026-11-18, but its verified coverage "
            "ends 2026-11-17") in early["withheld_detail"]
    assert overview._meta_spend_source(day3)["status"] == "account_unresolved"

    # The owner declares its last spend day; the ledger bears it out.
    monkeypatch.setenv("META_AD_ACCOUNT_RETIRED", f"{OLD}:2026-11-17")
    after = _truth(*week, now=day3)
    assert after["state"] == "verified" and after["usd_spend"] == 142.34
    assert after["owner_declared_retired"] == [
        {"account_id": OLD, "last_spend_day": "2026-11-17"}]
    assert after["owner_declared_retired_disclosure"] == (
        f"This total assumes ad account {OLD} spent nothing after 2026-11-17 — "
        "the owner's declaration (META_AD_ACCOUNT_RETIRED), not a reading from "
        "Meta.")
    old = after["accounts"][1]
    assert (old["configured"], old["required_through"], old["usd_spend"]) == (
        False, "2026-11-17", 142.34)
    line = overview._meta_spend_source(day3)
    assert line["status"] == "current"
    assert line["accounts"][1]["summary"] == (
        f"Logistaas ({OLD}): declared retired by the owner (last spend day "
        "2026-11-17); history through 2026-11-17 is kept")
    # A declared day BEFORE stored spend is contradicted by the data.
    monkeypatch.setenv("META_AD_ACCOUNT_RETIRED", f"{OLD}:2026-11-16")
    wrong = _truth(*week, now=day3)
    assert wrong["state"] == "coverage_incomplete"
    assert "has stored spend on 2026-11-17" in wrong["withheld_detail"]
    _no_token(_dump(live_db), line, after)


def test_13b_a_lost_token_does_not_turn_a_spending_account_into_a_removed_one(
        live_db, monkeypatch):
    """Review item 1. Both accounts sync; then META_ADS_ACCESS_TOKEN_2 is lost
    while META_AD_ACCOUNT_ID_2 still lists the old account. The old account
    is still spending; nothing may be published past its last verified day."""
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch)
    _run()
    monkeypatch.delenv("META_ADS_ACCESS_TOKEN_2")
    later = NOW + timedelta(days=9)                    # the 27th, 06:00 UTC
    graph.spend(CAMPAIGN_A, "2026-11-24", "8.00")      # the new account spends
    out, errors = _run(later)
    assert out["status"] == "partial" and out["reason"] == "meta_ads_misconfigured"
    assert {c["account_id"] for c in graph.calls
            if c["url"].endswith("/insights")} >= {NEW}
    assert _coverage(live_db, OLD)[-1][1] == "2026-11-17"   # old: no new chunk
    for start in (date(2026, 11, 12), date(2026, 11, 18)):
        truth = _truth(start, date(2026, 11, 25), now=later)
        assert truth["state"] == "coverage_incomplete", start
        assert truth["state"] not in ("verified", "verified_zero_spend")
        assert truth["usd_spend"] is None and truth["native_spend"] is None
        old = truth["accounts"][1]
        assert (old["account_id"], old["configured"], old["syncing"]) == (
            OLD, True, False)
    # Its verified days still publish.
    assert _truth(date(2026, 11, 12), date(2026, 11, 16), now=later)["usd_spend"] == 130.34
    line = overview._meta_spend_source(later)
    assert line["status"] == "misconfigured"
    assert line["accounts"][1]["summary"] == (
        f"Logistaas ({OLD}): credential set 2 is incomplete (access_token_missing), "
        "so it is not updated; history through 2026-11-17 is kept; later periods "
        "are withheld")
    # An id typo that empties the list: the configured accounts are unknown.
    monkeypatch.setenv("META_AD_ACCOUNT_ID_2", "94437O805110952")
    typo = _truth(date(2026, 11, 12), date(2026, 11, 16), now=later)
    assert typo["state"] == "source_unavailable" and typo["usd_spend"] is None
    _no_token(line, out, errors)


def test_14_a_readable_attempt_clears_not_readable_since(live_db):
    assert db_writers.record_meta_account_sync(OLD, "failed", reason="x", readable=False)
    since = _sync_state(live_db)[OLD][4]
    assert since is not None and _sync_state(live_db)[OLD][5] is None
    # A throttle says nothing about the account: "since" stands.
    db_writers.record_meta_account_sync(OLD, "failed", reason="meta_retries_exhausted")
    assert _sync_state(live_db)[OLD][4] == since
    db_writers.record_meta_account_sync(OLD, "success", readable=True)
    state = _sync_state(live_db)[OLD]
    assert state[0] == "success" and state[4] is None and state[5] is not None
    assert db_writers.record_meta_account_sync(OLD, "unknown") is False
    assert db_writers.record_meta_account_sync("", "failed") is False


def test_15_a_currency_or_zone_change_is_refused_once_spend_is_stored(
        live_db, monkeypatch):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch)
    _run()
    covered = _coverage(live_db)
    graph.calls.clear()
    graph.account["timezone_name"] = "Europe/London"
    out, errors = _run()
    assert out["status"] == "failed" and out["reason"] == "account_identity_changed"
    assert ("time zone on record is America/Los_Angeles, the API reports "
            "Europe/London, and the account has stored spend") in out["error"]
    assert graph.insight_calls() == []
    assert q(live_db, "SELECT currency_code, time_zone_name FROM meta_ads_account") == [
        ("GBP", "America/Los_Angeles")]
    assert _rows(live_db) == SEEDED and _coverage(live_db)[0][:3] == covered[0][:3]

    with pytest.raises(db_writers.MetaAccountIdentityChanged, match="currency"):
        db_writers.upsert_meta_ads_account({
            "account_id": NEW, "currency_code": "USD",
            "time_zone_name": "America/Los_Angeles"})
    # A fetch that carries no name, currency or zone blanks nothing.
    stored = db_writers.upsert_meta_ads_account({"account_id": NEW})
    assert (stored["account_name"], stored["currency_code"],
            stored["time_zone_name"]) == ("Logistaas Limited", "GBP",
                                          "America/Los_Angeles")
    assert stored["fetched_at"] > stored["first_seen_at"]
    assert stored["identity_changed"] == {}


def test_16_a_zone_change_is_accepted_while_the_account_has_no_spend(
        live_db, monkeypatch):
    """The owner moves the NEW account (no delivery yet) to UK time. There is
    nothing to relabel; its zero-coverage, dated in Los Angeles days, is
    dropped and every day is read again in London days."""
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch)
    _run()
    assert _coverage(live_db, NEW)[0][:2] == ("2026-10-20", "2026-11-16")

    graph.account["timezone_name"] = "Europe/London"
    out, errors = _run()
    assert out["status"] == "success" and errors == []
    new = out["accounts"][0]
    assert new["identity_changed"] == {"time zone": {
        "from": "America/Los_Angeles", "to": "Europe/London"}}
    assert q(live_db, "SELECT time_zone_name FROM meta_ads_account "
                      "WHERE account_id = %s", (NEW,)) == [("Europe/London",)]
    # London's yesterday at 06:00 UTC is the 17th; the Los Angeles chunk is gone.
    assert [c[:3] + (c[5],) for c in _coverage(live_db, NEW)] == [
        ("2026-10-21", "2026-11-17", "verified", "Europe/London")]
    # The other account was not touched.
    assert _rows(live_db, OLD) == OLD_ROWS and len(_coverage(live_db, OLD)) == 1
    truth = _truth(end=OLD_YESTERDAY)
    assert truth["state"] == "verified" and truth["usd_spend"] == 142.34
    assert truth["time_zones"] == ["Asia/Amman", "Europe/London"]
    line = overview._meta_spend_source(NOW)
    assert line["status"] == "current" and line["complete_through"] == "2026-11-17"


def test_17_a_deprecated_api_version_on_the_first_run(live_db, monkeypatch):
    graph = FakeGraph()
    fakes.configure(monkeypatch, graph, version="v22.0")
    out, errors = _run()
    assert out["status"] == "failed" and out["reason"] == "meta_api_version_deprecated"
    text = ("Meta API version v22.0 is deprecated; Meta says upgrade to v27.0 "
            "(HTTP 400, code 2635)")
    assert errors == [f"meta_ads_api/meta_campaign_spend: account {NEW}: {text}"]
    # No account on record, so no window: the batch has no dates and no
    # coverage row is invented.
    assert _batches(live_db) == [
        ("daily", "failed", 0,
         f"meta_api_version_deprecated: account {NEW}: {text}", None, None)]
    assert _coverage(live_db) == [] and _rows(live_db) == []
    line = overview._meta_spend_source(NOW)
    assert line["status"] == "not_proven"
    assert line["reason"] == "meta_api_version_deprecated"
    assert line["reason_detail"] == text
    # Configured, and never read: not proven — not "not connected".
    first = _truth()
    assert first["state"] == "coverage_incomplete" and first["usd_spend"] is None
    assert first["accounts"][0]["never_read"] is True


# ── FX ────────────────────────────────────────────────────────────────────

def test_18_gbp_spend_with_a_missing_rate_is_partial_then_verified(
        live_db, monkeypatch):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch, fail=("2026-11-16",))
    out, errors = _run()
    assert out["status"] == "partial" and out["reason"] == "fx_rates_missing"
    assert out["accounts"][0]["fx_missing_dates"] == ["2026-11-16"]
    # The spend is stored and the chunk stays verified.
    assert _rows(live_db) == SEEDED and _coverage(live_db)[0][2] == "verified"
    # `partial` is `partial` on the batch — not success, not failed.
    assert _batches(live_db) == [
        ("daily", "partial", 3, f"fx_rates_missing: account {NEW}: the spend is "
         "stored; no USD rate for 1 spend day(s): 2026-11-16",
         "2026-10-20", "2026-11-16")]
    assert q(live_db, "SELECT COUNT(*) FROM sync_state WHERE source = 'meta_ads_api' "
                      "AND status = 'success'") == [(0,)]
    truth = _truth()
    assert truth["state"] == "fx_incomplete" and truth["fx_reason"] == "fx_rates_missing"
    assert truth["native_spend"] == 218.7 and truth["usd_spend"] is None
    # 120.50 × 1.25 = 150.625 exactly; the published figure is that to 2 dp.
    assert _truth(end=date(2026, 11, 15))["usd_spend"] == pytest.approx(
        150.625, abs=0.0051)

    _fx(monkeypatch)
    out, errors = _run()
    assert out["status"] == "success"
    assert _truth()["state"] == "verified" and _truth()["usd_spend"] == 273.38


def test_19_each_day_converts_at_its_own_days_rate(live_db, monkeypatch):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch)
    _run()
    with live_db.connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE fx_rates SET rate = 2 WHERE rate_date = '2026-11-16' "
                    "AND base_currency = 'GBP' AND quote_currency = 'USD'")
        conn.commit()
    # 120.50 × 1.25 + 98.20 × 2 = 150.625 + 196.40
    assert _truth()["usd_spend"] == pytest.approx(347.025, abs=0.0051)
    from db import meta_ads_repository as repo
    spend = repo.fetch_meta_campaign_spend(WEEK[0], WEEK[1], NEW)
    assert {r["campaign_id"]: round(r["spend_usd"], 3) for r in spend["rows"]} == {
        CAMPAIGN_A: 346.825, CAMPAIGN_B: 0.2}
    assert spend["currency_code"] == "GBP" and spend["fx_complete"] is True


def test_20_two_accounts_each_convert_with_their_own_currency(live_db, monkeypatch):
    graph = _pair()
    graph.spend(CAMPAIGN_C, "2026-11-16", "40.00", name="UK Launch")   # GBP account
    fakes.configure(monkeypatch, graph)
    fx_calls = _fx(monkeypatch)
    out, _ = _run()
    assert out["status"] == "success"
    assert fx_calls == [("2026-11-16", "GBP", "USD")]      # never asked for USD
    assert q(live_db, "SELECT base_currency, COUNT(*) FROM fx_rates "
                      "GROUP BY base_currency") == [("GBP", 1)]
    truth = _truth()
    assert truth["state"] == "verified"
    assert truth["usd_spend"] == 180.34              # 40.00 × 1.25 + 130.34 × 1
    assert truth["native_spend"] is None and truth["native_currency"] is None
    assert [(a["currency"], a["native_spend"], a["usd_spend"], a["usd_identity"])
            for a in truth["accounts"]] == [
        ("GBP", 40.0, 50.0, False), ("USD", 130.34, 130.34, True)]


def test_21_a_usd_account_converts_at_one_with_no_rate_on_record(live_db, monkeypatch):
    graph = _seeded(currency="USD")
    fakes.configure(monkeypatch, graph)
    fx_calls = _fx(monkeypatch)
    out, _ = _run()
    assert out["status"] == "success" and fx_calls == []
    assert q(live_db, "SELECT COUNT(*) FROM fx_rates") == [(0,)]
    truth = _truth()
    assert truth["state"] == "verified" and truth["accounts"][0]["usd_identity"] is True
    assert truth["native_spend"] == truth["usd_spend"] == 218.7


def test_22_a_currency_the_provider_does_not_price_is_never_converted(
        live_db, monkeypatch):
    graph = _seeded(currency="JOD")
    fakes.configure(monkeypatch, graph)
    fx_calls = _fx(monkeypatch)
    out, errors = _run()
    assert out["status"] == "partial" and out["reason"] == "fx_currency_not_supported"
    assert fx_calls == [] and q(live_db, "SELECT COUNT(*) FROM fx_rates") == [(0,)]
    assert _coverage(live_db)[0][2] == "verified"
    truth = _truth()
    assert truth["state"] == "fx_incomplete"
    assert truth["fx_reason"] == "fx_currency_not_supported"
    assert (truth["native_currency"], truth["native_spend"], truth["usd_spend"]) == (
        "JOD", 218.7, None)


# ── registration ──────────────────────────────────────────────────────────

def test_23_the_schema_holds_what_the_freshness_registry_names(live_db):
    from db.schema import init_db
    from services.freshness_service import DATASET_FRESHNESS_CONFIG
    init_db()                                        # idempotent
    config = DATASET_FRESHNESS_CONFIG["meta_ads_spend"]
    columns = set(q(live_db, "SELECT table_name, column_name FROM "
                             "information_schema.columns WHERE table_schema = 'public'"))
    assert (config["table"], config["date_column"]) in columns
    # Every Meta table is keyed by account.
    for table in ("meta_ads_account", "meta_ads_campaign_daily_spend",
                  "meta_ads_account_daily_spend", "meta_ads_spend_coverage",
                  "meta_ads_account_sync_state"):
        assert (table, "account_id") in columns
    # The ledger accepts the two statuses the gates know, and no third.
    with pytest.raises(Exception):
        q(live_db, "INSERT INTO meta_ads_spend_coverage (account_id, chunk_start, "
                   "chunk_end, status) VALUES ('1', '2026-01-01', '2026-01-02', "
                   "'bounded') RETURNING id")


def test_24_the_freshness_endpoint_reports_the_dataset(live_db, monkeypatch):
    import api.server as server
    user = {"username": "tester", "role": "admin"}

    def row():
        payload = server.api_datasets_freshness(user=user, days=60)
        return {d["dataset_key"]: d for d in payload["datasets"]}[
            "meta_ads_api/meta_campaign_spend"]
    fakes.unconfigure(monkeypatch)
    before = row()
    assert before["rows_in_window"] == 0 and before["canonical_status"] == "not_run"

    # Today, so the endpoint's own window (the last 60 days) holds the rows.
    now = datetime.now(tz=timezone.utc).replace(hour=6, minute=0)
    graph = FakeGraph()
    day = meta.account_local_yesterday("America/Los_Angeles", now)
    graph.spend(CAMPAIGN_A, day, "20.00").spend(CAMPAIGN_B, day, "1.00")
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch)
    out, _ = _run(now)
    assert out["status"] == "success"
    after = row()
    assert after["rows_in_window"] == 2 and after["last_batch_row_count"] == 2
    assert after["canonical_status"] == "fresh_with_data"


# ── the System page ───────────────────────────────────────────────────────

def test_25_with_empty_tables_meta_is_not_connected_and_claims_nothing(
        live_db, monkeypatch):
    graph = _pair()
    fakes.unconfigure(monkeypatch)
    monkeypatch.setattr(requests, "get", graph.get)
    out, errors = _run()
    assert out["status"] == "skipped" and errors == []
    assert graph.calls == []
    for table in ("meta_ads_account", "meta_ads_campaign_daily_spend",
                  "meta_ads_account_daily_spend", "meta_ads_spend_coverage",
                  "meta_ads_account_sync_state"):
        assert q(live_db, f"SELECT COUNT(*) FROM {table}") == [(0,)]
    assert _batches(live_db) == []
    line = overview._meta_spend_source(NOW)
    assert line["status"] == "not_connected" and line["accounts"] == []
    for key in ("complete_through", "last_success_at", "age_hours", "window_end",
                "reason"):
        assert line[key] is None, key
    page = overview.build_system_overview(NOW)
    assert [s["key"] for s in page["sources"]] == [
        "hubspot_contacts", "hubspot_deals", "google_ads_spend", "meta_ads_spend"]
    assert page["sources"][3]["status"] == "not_connected"
    assert page["external_calls_performed"] is False and graph.calls == []
    assert _truth()["state"] == "not_connected"


def test_26_half_set_up_set_up_but_never_run_and_one_broken_set(live_db, monkeypatch):
    fakes.unconfigure(monkeypatch)
    monkeypatch.setenv("META_AD_ACCOUNT_ID", NEW)
    line = overview._meta_spend_source(NOW)
    assert (line["status"], line["reason"]) == ("misconfigured", "access_token_missing")
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    line = overview._meta_spend_source(NOW)
    assert (line["status"], line["reason"]) == ("not_proven", "no_account_on_record")
    assert [a["summary"] for a in line["accounts"]] == [
        f"Account ({NEW}): no successful update yet",
        f"Account ({OLD}): no successful update yet"]
    assert graph.calls == []                         # the page never calls Meta

    # An account listed in two sets: nothing syncs, and the page names the id.
    monkeypatch.setenv("META_AD_ACCOUNT_ID_2", f"{OLD},{NEW}")
    line = overview._meta_spend_source(NOW)
    assert line["status"] == "misconfigured"
    assert line["reason_detail"] == (
        f"ad_account_id_in_two_sets: account {NEW} is listed in two credential sets")
    out, errors = _run()
    assert out["status"] == "failed" and NEW in errors[0] and graph.calls == []

    # Set 2 loses its token: set 1 syncs and is current, the run is partial,
    # and the page says a set is broken rather than "current".
    monkeypatch.setenv("META_AD_ACCOUNT_ID_2", OLD)
    monkeypatch.delenv("META_ADS_ACCESS_TOKEN_2")
    _fx(monkeypatch)
    out, errors = _run()
    assert out["status"] == "partial" and out["reason"] == "meta_ads_misconfigured"
    assert _batches(live_db)[-1][1] == "partial"
    line = overview._meta_spend_source(NOW)
    assert line["status"] == "misconfigured"
    assert line["reason_detail"] == "credential set 2: access_token_missing"
    assert line["accounts"][0]["status"] == "current"
    assert line["accounts"][1]["status"] == "misconfigured"
    # ...and it is shown whatever the other account says: here, late.
    late = overview._meta_spend_source(NOW + timedelta(days=3))
    assert late["accounts"][0]["status"] == "late"
    assert late["status"] == "misconfigured"
    assert late["reason_detail"] == "credential set 2: access_token_missing"
    _no_token(line, out, errors, _dump(live_db))


def test_27_with_nothing_configured_stored_accounts_are_still_required(
        live_db, monkeypatch):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch)
    _run()
    assert _truth()["usd_spend"] == 130.34
    fakes.unconfigure(monkeypatch)
    truth = _truth()
    assert truth["state"] == "verified" and truth["usd_spend"] == 130.34
    assert [a["configured"] for a in truth["accounts"]] == [False, False]
    assert truth["owner_declared_retired"] == []
    # A range after every account's last verified day: withheld. Nobody has
    # said these accounts stopped spending.
    later = _truth(date(2026, 12, 1), date(2026, 12, 7),
                   now=NOW + timedelta(days=30))
    assert later["state"] == "coverage_incomplete" and later["usd_spend"] is None
    line = overview._meta_spend_source(NOW)
    assert line["status"] == "account_unresolved"
    assert [a["status"] for a in line["accounts"]] == ["account_unresolved"] * 2
    # Declared, and borne out: then Meta is simply not connected.
    monkeypatch.setenv("META_AD_ACCOUNT_RETIRED",
                       f"{NEW}:2026-11-16,{OLD}:2026-11-17")
    gone = _truth(date(2026, 12, 1), date(2026, 12, 7), now=NOW + timedelta(days=30))
    assert gone["state"] == "not_connected" and gone["usd_spend"] is None
    line = overview._meta_spend_source(NOW)
    assert line["status"] == "not_connected"
    assert [a["summary"] for a in line["accounts"]] == [
        f"Logistaas Limited ({NEW}): declared retired by the owner (last spend "
        "day 2026-11-16); history through 2026-11-16 is kept",
        f"Logistaas ({OLD}): declared retired by the owner (last spend day "
        "2026-11-17); history through 2026-11-17 is kept"]
    assert _rows(live_db, OLD) == OLD_ROWS           # nothing is deleted


def test_27b_a_clean_sync_never_reads_late_at_any_hour_of_the_day(
        live_db, monkeypatch):
    """Review item 5. Each account is judged against the last day its latest
    DUE sync could settle in its own zone — not against page-view time. At
    06:00 UTC Los Angeles' yesterday has two hours left to run; judged at
    page-view time that account read "late" from 08:00 UTC until the next
    morning, after a perfectly clean sync."""
    graph = _pair()
    graph.spend(CAMPAIGN_A, "2026-11-16", "5.00")
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch)
    synced = NOW + timedelta(minutes=5)                # 06:05 UTC on the 18th
    out, _ = _run(synced)
    assert out["status"] == "success"
    seen = set()
    for minutes in range(0, 24 * 60 + 1, 30):          # 06:05 → 06:05 next day
        at = synced + timedelta(minutes=minutes)
        line = overview._meta_spend_source(at)
        by = {a["account_id"]: a for a in line["accounts"]}
        assert line["status"] == "current", (at, line["accounts"])
        assert by[NEW]["status"] == by[OLD]["status"] == "current", at
        seen.add((by[NEW]["complete_through"], by[OLD]["complete_through"]))
    # All day: Los Angeles through the 16th, Amman through the 17th.
    assert seen == {("2026-11-16", "2026-11-17")}
    # The next morning's sync never ran. Once it was due (09:00 UTC), both
    # accounts read late — each naming its own missing day.
    missed = overview._meta_spend_source(synced + timedelta(days=1, hours=3))
    by = {a["account_id"]: a for a in missed["accounts"]}
    assert missed["status"] == "late"
    assert by[NEW]["missing_ranges"] == [{"start": "2026-11-17", "end": "2026-11-17"}]
    assert by[OLD]["missing_ranges"] == [{"start": "2026-11-18", "end": "2026-11-18"}]
    # It ran: current again, each one day further on.
    _run(synced + timedelta(days=1))
    line = overview._meta_spend_source(synced + timedelta(days=1, hours=3))
    assert line["status"] == "current"
    assert [a["complete_through"] for a in line["accounts"]] == [
        "2026-11-17", "2026-11-18"]


def test_27c_complete_through_is_not_said_while_usd_is_unavailable(
        live_db, monkeypatch):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch, fail=("2026-11-16",))
    out, _ = _run()
    assert out["status"] == "partial" and out["reason"] == "fx_rates_missing"
    line = overview._meta_spend_source(NOW)
    assert line["status"] == "usd_unavailable" and line["complete_through"] is None
    assert line["reason"] == "fx_rates_missing"
    assert line["accounts"][0]["summary"] == (
        f"Logistaas Limited ({NEW}): spend recorded through 2026-11-16 "
        "(America/Los_Angeles); no US-dollar value for 1 day(s) in GBP")
    _fx(monkeypatch)
    _run()
    assert overview._meta_spend_source(NOW)["status"] == "current"


# ── backfill ──────────────────────────────────────────────────────────────

def test_28_the_backfill_writes_monthly_chunks_per_account_and_no_run(
        live_db, monkeypatch):
    graph = _pair()
    old = graph.accounts[OLD]
    old.spend(CAMPAIGN_C, "2026-09-12", "40.00", name="Video — GCC")
    old.spend(CAMPAIGN_C, "2026-10-03", "41.00", name="Video — GCC")
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch)
    dry = meta.run_backfill(date(2026, 9, 10), None, dry_run=True, now=NOW)
    assert all(c["status"] == "planned" for a in dry["accounts"] for c in a["chunks"])
    assert _coverage(live_db) == [] and _batches(live_db) == []
    assert q(live_db, "SELECT COUNT(*) FROM meta_ads_account") == [(0,)]

    # One account first.
    out = meta.run_backfill(date(2026, 9, 10), None, now=NOW, account_ids=[OLD])
    assert out["status"] == "success"
    assert [(c[0], c[1], c[2], c[3]) for c in _coverage(live_db, OLD)] == [
        ("2026-09-10", "2026-09-30", "verified", 1),
        ("2026-10-01", "2026-10-31", "verified", 1),
        ("2026-11-01", "2026-11-17", "verified", 3)]   # capped at Amman's yesterday
    assert _coverage(live_db, NEW) == []
    # The new account is configured and not yet covered: the total is withheld.
    assert _truth(date(2026, 9, 10), YESTERDAY)["state"] == "coverage_incomplete"

    # Then every account, resuming: the old one is skipped, the new one read.
    graph.calls.clear()
    out = meta.run_backfill(date(2026, 9, 10), None, now=NOW, resume=True)
    assert out["status"] == "success"
    assert graph.insight_calls(account_id=OLD) == []
    assert [(c[0], c[1], c[2], c[3]) for c in _coverage(live_db, NEW)] == [
        ("2026-09-10", "2026-09-30", "verified", 0),
        ("2026-10-01", "2026-10-31", "verified", 0),
        ("2026-11-01", "2026-11-16", "verified", 0)]   # capped at LA's yesterday
    # No sync batch, no sync_state row and no run: only the daily step moves
    # this dataset's freshness.
    assert _batches(live_db) == []
    assert q(live_db, "SELECT COUNT(*) FROM sync_state "
                      "WHERE source = 'meta_ads_api'") == [(0,)]
    assert q(live_db, "SELECT COUNT(*) FROM runs") == [(0,)]
    whole = _truth(date(2026, 9, 10), YESTERDAY)
    assert whole["state"] == "verified" and whole["usd_spend"] == 211.34
    # The days before the backfill's first day were never fetched.
    assert _truth(date(2026, 9, 1), YESTERDAY)["state"] == "coverage_incomplete"
    _no_token(_dump(live_db), out)


def test_28b_a_backfill_cannot_make_a_failed_daily_feed_read_fresh(
        live_db, monkeypatch):
    """Review item 6, through the freshness the product publishes
    (`compute_canonical_freshness`, via /api/datasets/freshness)."""
    import api.server as server
    from datetime import datetime as _dt

    def freshness():
        payload = server.api_datasets_freshness(
            user={"username": "tester", "role": "admin"}, days=60)
        row = {d["dataset_key"]: d for d in payload["datasets"]}[
            "meta_ads_api/meta_campaign_spend"]
        state = q(live_db, "SELECT status, last_source_date::text, last_batch_id "
                           "FROM sync_state WHERE source = 'meta_ads_api'")
        return row["canonical_status"], row.get("status"), state

    now = _dt.now(tz=timezone.utc).replace(hour=6, minute=0)
    graph = FakeGraph()
    old = graph.add_account(OLD)
    for account, zone in ((graph.default, "America/Los_Angeles"), (old, "Asia/Amman")):
        day = meta.account_local_yesterday(zone, now)
        account.spend(CAMPAIGN_A, day, "10.00")
        account.spend(CAMPAIGN_A, day - timedelta(days=40), "4.00")
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch)
    out, _ = _run(now)
    assert out["status"] == "success"
    good = freshness()
    assert good[0] == "fresh_with_data"

    # The next daily run fails for BOTH accounts (the token for each is
    # refused), so the daily feed is failed.
    for account in graph.accounts.values():
        account.refusal = error_response(400, 190, subcode=463)
    out, _ = _run(now + timedelta(days=1))
    assert out["status"] == "failed"
    failed = freshness()
    assert failed[0] != "fresh_with_data" and failed[2][0][0] == "failed"
    assert failed[2][0][1] == good[2][0][1]            # the watermark did not move

    # A backfill of ONE account, which succeeds.
    graph.default.refusal = None
    result = meta.run_backfill(now.date() - timedelta(days=50), None,
                               account_ids=[NEW], now=now + timedelta(days=1))
    assert result["status"] == "success"
    assert len(_coverage(live_db, NEW)) >= 3           # it did write its ledger
    assert freshness() == failed                       # and changed nothing here

    # A partial backfill (the old account still refused) changes nothing either.
    result = meta.run_backfill(now.date() - timedelta(days=50), None,
                               now=now + timedelta(days=1), resume=True)
    assert result["status"] == "partial"
    assert freshness() == failed
    assert len(_batches(live_db)) == 2                 # the two daily runs only


def test_29_the_backfill_script_takes_an_account_and_skips_when_not_configured(
        live_db, monkeypatch, capsys):
    from scripts import backfill_meta_ads_spend as script
    fakes.unconfigure(monkeypatch)
    out = meta.run_backfill(date(2026, 9, 10), None, now=NOW)
    assert (out["status"], out["reason"]) == ("skipped", "not_configured")
    assert script.main(["--from", "2026-09-10"]) == 2
    assert _batches(live_db) == []

    graph = _pair()
    fakes.configure(monkeypatch, graph)
    _fx(monkeypatch)
    capsys.readouterr()
    assert script.main(["--from", "2026-11-01", "--to", "2026-11-10",
                        "--account", f"act_{OLD}"]) == 0
    printed = capsys.readouterr().out
    assert json.loads(printed)["accounts"][0]["account_id"] == OLD
    assert {c["account_id"] for c in graph.calls} == {OLD}
    assert script.main(["--from", "2026-11-01", "--account", "1111111111"]) == 1
    assert script.main(["--from", "2026-11-01", "--account", "not-an-id"]) == 1
    _no_token(printed, capsys.readouterr().out)


def test_30_the_smoke_test_prints_each_account_and_never_a_token(
        live_db, monkeypatch, capsys):
    """Runs against the fake at today's date; reads no database."""
    from scripts import meta_ads_smoke_test as smoke
    graph = FakeGraph()
    old = graph.add_account(OLD)
    today = datetime.now(tz=timezone.utc)
    day = meta.account_local_yesterday("Asia/Amman", today)
    old.spend(CAMPAIGN_A, day, "61.20").spend(CAMPAIGN_B, day, "8.80")
    old.spend(CAMPAIGN_A, day - timedelta(days=10), "1839.34")
    old.spend(CAMPAIGN_A, day - timedelta(days=40), "999.00")     # outside 30 days
    fakes.configure(monkeypatch, graph)
    before = _dump(live_db)
    assert smoke.main([]) == 0
    text = capsys.readouterr().out
    assert f"--- Ad account {NEW} ---" in text and f"--- Ad account {OLD} ---" in text
    for expected in (
            "API version      : v27.0 (from env)",
            f"Credential set 1 : token set; accounts {NEW}; app secret proof not sent",
            f"Credential set 2 : token set; accounts {OLD}; app secret proof not sent",
            "Name             : Logistaas Limited", "Currency         : GBP",
            "Time zone        : America/Los_Angeles", "Status           : ACTIVE (1)",
            "Active campaigns : 0", "Last 30 days     : 0.00 GBP on 0 day(s) with spend",
            "Name             : Logistaas", "Currency         : USD",
            "Time zone        : Asia/Amman", "Active campaigns : 2",
            "Last 30 days     : 1909.34 USD on 2 day(s) with spend",
            "Campaign sum     : 70.00 USD", "Account total    : 70.00 USD",
            "USD conversion   : not needed (USD account)",
            "USD conversion   : available",
            "RESULT           : OK — the campaign rows sum to the account total",
            "=== 2 of 2 account(s) OK ==="):
        assert expected in text, expected
    _no_token(text)
    assert _dump(live_db) == before                  # it wrote nothing

    # One account refused: it says which, plainly, and exits 1.
    old.refusal = error_response(403, 200, message="(#200) Permissions error")
    assert smoke.main([]) == 1
    text = capsys.readouterr().out
    assert "FAILED           : meta_api_refused: Meta API refused the request: " \
           "HTTP 403, code 200" in text
    assert "=== 1 of 2 account(s) OK ===" in text
    fakes.unconfigure(monkeypatch)
    assert smoke.main([]) == 2
    _no_token(text, capsys.readouterr().out)
