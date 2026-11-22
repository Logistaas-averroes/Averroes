"""PR-ADS-171A — the read-only Meta Ads spend connector.

Every case drives the real connector and the real sync step against a fake
Graph API installed at ``requests.get`` (tests/meta_ads_fakes.py). The fake
honours the query it is sent, pages with a ``paging.next`` URL that embeds the
access token, and fails the request if the token is anywhere but the
``Authorization`` header.

The database-backed cases are in test_pr_ads_171a_meta_ads_pg_integration.py.
"""
from __future__ import annotations

import ast
import json
import logging
import re
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import connectors.meta_ads as connector
import db.writers as db_writers
import scheduler.incremental_sync as sync
from api import monitoring
from services import dataset_keys
from services import meta_ads_spend_service as meta
from services import meta_ads_spend_truth_service as truth_service
from services.freshness_service import (
    DATASET_FRESHNESS_CONFIG, CanonicalFreshnessStatus,
)
from tests import meta_ads_fakes as fakes
from tests.meta_ads_fakes import (
    ACCOUNT_ID, CAMPAIGN_A, CAMPAIGN_B, CAMPAIGN_C, OLD_ACCOUNT_ID,
    SENTINEL_TOKEN, SENTINEL_TOKEN_2, FakeGraph, FakeResponse, error_response,
)

ROOT = Path(__file__).resolve().parent.parent
# The production account (owner's Render shell, 2026-11-22): currency GBP,
# time zone America/Los_Angeles. The daily sync runs at 06:00 UTC, which is
# 22:00 the PREVIOUS day in Los Angeles: the UTC date's yesterday (the 17th)
# is still in progress there, so the last complete day is the 16th.
NOW = datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)
YESTERDAY = date(2026, 11, 16)            # in America/Los_Angeles at NOW
IN_PROGRESS = date(2026, 11, 17)          # not over in Los Angeles at NOW
WINDOW_START = date(2026, 10, 20)         # 28 days ending YESTERDAY


def _seeded(**kwargs) -> FakeGraph:
    graph = FakeGraph(**kwargs)
    graph.spend(CAMPAIGN_A, "2026-11-15", "120.50", name="Lead Gen — MENA")
    graph.spend(CAMPAIGN_A, "2026-11-16", "98.10", name="Lead Gen — MENA")
    graph.spend(CAMPAIGN_B, "2026-11-16", "0.10", name="Retargeting — EU")
    # Spend so far on the day still in progress in Los Angeles.
    graph.spend(CAMPAIGN_A, "2026-11-17", "55.00", name="Lead Gen — MENA")
    return graph


def _pair() -> FakeGraph:
    """The two production accounts (owner's Render shell, 2026-11-22).

    Set 1: 4730305973961806 "Logistaas Limited", GBP, America/Los_Angeles —
    the new account, no campaigns, no delivery. Set 2 (its own token, another
    Business portfolio): 944370805110952 "Logistaas", USD, Asia/Amman — the
    old account, with the running ads.
    """
    graph = FakeGraph()
    old = graph.add_account(OLD_ACCOUNT_ID)
    old.spend(CAMPAIGN_A, "2026-11-15", "70.00", name="Lead Gen — MENA")
    old.spend(CAMPAIGN_A, "2026-11-16", "60.34", name="Lead Gen — MENA")
    old.spend(CAMPAIGN_B, "2026-11-17", "12.00", name="Retargeting — GCC")
    old.spend(CAMPAIGN_A, "2026-11-18", "9.99", name="Lead Gen — MENA")  # in progress
    return graph


@pytest.fixture()
def store(monkeypatch):
    """The database, replaced by recorders, for cases that are about the API."""
    s = SimpleNamespace(batches=[], finished=[], replaced=[], failures=[],
                        accounts={}, synced=[], fx_missing=[], fx_calls=[],
                        replace_error=None, identity_error=None)

    def start(**kw):
        s.batches.append(kw)
        return len(s.batches)

    def finish(**kw):
        s.finished.append(kw)
        return True

    def upsert(account):
        if s.identity_error:
            raise s.identity_error
        s.accounts[account["account_id"]] = dict(account)
        return {**account, "identity_changed": {}}

    def replace(account_id, start, end, rows, account_days, **kw):
        if s.replace_error:
            raise s.replace_error
        s.replaced.append({"account_id": account_id, "start": start, "end": end,
                           "rows": list(rows), "account_days": list(account_days),
                           **kw})
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

    monkeypatch.setattr(db_writers, "start_sync_batch", start)
    monkeypatch.setattr(db_writers, "finish_sync_batch", finish)
    monkeypatch.setattr(db_writers, "upsert_meta_ads_account", upsert)
    monkeypatch.setattr(db_writers, "replace_meta_campaign_spend_chunk", replace)
    monkeypatch.setattr(db_writers, "record_meta_spend_coverage_failure", failure)
    monkeypatch.setattr(db_writers, "record_meta_account_sync", synced)
    from db import meta_ads_repository as repo
    from services import fx_service
    monkeypatch.setattr(repo, "fetch_meta_account", lambda account_id: {
        "available": True, "account": s.accounts.get(account_id)})
    monkeypatch.setattr(repo, "fetch_meta_fx_missing_dates",
                        lambda account_id, start, end: {
                            "available": True, "dates": list(s.fx_missing)})

    def ensure(start, end, **kw):
        s.fx_calls.append((start, end, kw))
        return {"rows_written": 0, "fetched": 0, "failed": []}
    monkeypatch.setattr(fx_service, "ensure_fx_rates", ensure)
    return s


def _acct(out, index=0):
    """One account's own result inside the step's result."""
    return out["accounts"][index]


def _run(errors=None):
    errors = [] if errors is None else errors
    return sync._sync_meta_ads_spend(run_id=7, now=NOW, errors=errors), errors


# ── configuration ─────────────────────────────────────────────────────────

def test_01_unset_credentials_are_not_configured_and_not_misconfigured(monkeypatch):
    fakes.unconfigure(monkeypatch)
    status = connector.config_status()
    assert status["configured"] is False and status["misconfigured"] is False
    assert status["reason"] is None and status["account_ids"] == []
    assert status["sets"] == []


@pytest.mark.parametrize("env, reason", [
    ({"META_ADS_ACCESS_TOKEN": SENTINEL_TOKEN}, "ad_account_id_missing"),
    ({"META_AD_ACCOUNT_ID": ACCOUNT_ID}, "access_token_missing"),
    ({"META_ADS_ACCESS_TOKEN": SENTINEL_TOKEN,
      "META_AD_ACCOUNT_ID": "my-account"}, "ad_account_id_invalid"),
    ({"META_ADS_ACCESS_TOKEN": SENTINEL_TOKEN, "META_AD_ACCOUNT_ID": ACCOUNT_ID,
      "META_API_VERSION": "latest"}, "api_version_unresolved"),
])
def test_02_a_half_configuration_is_misconfigured_and_names_why(monkeypatch, env, reason):
    fakes.unconfigure(monkeypatch)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    status = connector.config_status()
    assert status["configured"] is False and status["misconfigured"] is True
    assert status["reason"] == reason
    assert SENTINEL_TOKEN not in json.dumps(status)


def test_03_the_api_version_comes_from_config_and_the_env_overrides_it(monkeypatch):
    fakes.unconfigure(monkeypatch)
    monkeypatch.setattr(connector, "_config_file", lambda: {"api_version": "v24.0"})
    assert connector.resolve_api_version() == ("v24.0", "config")
    monkeypatch.setenv("META_API_VERSION", "v26.0")
    assert connector.resolve_api_version() == ("v26.0", "env")
    # An invalid override does not fall back to the configured version.
    monkeypatch.setenv("META_API_VERSION", "26")
    assert connector.resolve_api_version() == (None, "env")
    monkeypatch.delenv("META_API_VERSION")
    monkeypatch.setattr(connector, "_config_file", lambda: {})
    assert connector.resolve_api_version() == (None, "unresolved")
    monkeypatch.setattr(connector, "_config_file", lambda: {"api_version": "v24"})
    assert connector.resolve_api_version() == (None, "config")


def test_04_no_api_version_is_written_in_the_connector():
    source = (ROOT / "connectors/meta_ads.py").read_text()
    literals = [n.value for n in ast.walk(ast.parse(source))
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and re.fullmatch(r"v\d+\.\d+", n.value.strip())]
    assert literals == []
    # ...and the shipped config resolves to one.
    import yaml
    cfg = yaml.safe_load((ROOT / "config/thresholds.yaml").read_text())
    assert re.fullmatch(r"v\d+\.\d+", cfg["meta_ads"]["api_version"])
    assert cfg["sync"]["daily_incremental"]["lookback_days"]["meta_ads"] == 28
    assert meta.lookback_days() == 28


def test_05_an_act_prefix_on_the_account_id_is_removed(monkeypatch):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    monkeypatch.setenv("META_AD_ACCOUNT_ID", f"act_{ACCOUNT_ID}")
    assert connector.config_status()["account_ids"] == [ACCOUNT_ID]
    assert connector.fetch_account(ACCOUNT_ID)["account_id"] == ACCOUNT_ID
    assert graph.calls[0]["url"].endswith(f"/act_{ACCOUNT_ID}")


# ── the request primitive ─────────────────────────────────────────────────

@pytest.mark.parametrize("method", ["POST", "post", "DELETE", "PUT", "PATCH"])
def test_06_any_method_but_get_is_refused_before_a_request_is_made(monkeypatch, method):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    with pytest.raises(connector.MetaAdsError, match="read-only"):
        connector._request(ACCOUNT_ID, "insights", {}, method=method)
    assert graph.calls == []


def test_07_the_connector_calls_requests_get_and_nothing_else():
    """No POST (so no asynchronous report run), no session, no other verb."""
    tree = ast.parse((ROOT / "connectors/meta_ads.py").read_text())
    used = sorted({n.attr for n in ast.walk(tree)
                   if isinstance(n, ast.Attribute)
                   and isinstance(n.value, ast.Name) and n.value.id == "requests"})
    assert used == ["exceptions", "get"]


def test_08_the_token_travels_in_the_authorization_header_only(monkeypatch):
    graph = _seeded()
    fakes.configure(monkeypatch, graph, app_secret="app-secret-3f9a")
    connector.fetch_account(ACCOUNT_ID)
    list(connector.iter_campaign_daily_spend(
        ACCOUNT_ID, "2026-11-16", "2026-11-17"))
    assert graph.calls                       # the fake asserted on every one
    for call in graph.calls:
        assert call["headers"]["Authorization"] == f"Bearer {SENTINEL_TOKEN}"
        assert SENTINEL_TOKEN not in call["url"]
        assert SENTINEL_TOKEN not in json.dumps(call["params"])
        assert call["kwargs"].get("allow_redirects") is False
        # The optional proof is an HMAC of the token, never the token.
        assert re.fullmatch(r"[0-9a-f]{64}", call["params"]["appsecret_proof"])
        assert "app-secret-3f9a" not in json.dumps(call["params"])


def test_09_a_token_put_in_the_query_is_caught_by_the_fake(monkeypatch):
    """The guard's own counterfactual: the fake refuses a request built the
    way Meta's examples build it."""
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    with pytest.raises(AssertionError, match="access token is in the query"):
        requests.get(f"https://graph.facebook.com/{fakes.VERSION}/act_{ACCOUNT_ID}",
                     params={"access_token": SENTINEL_TOKEN},
                     headers={"Authorization": f"Bearer {SENTINEL_TOKEN}"},
                     timeout=5)


# ── spend amounts ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("spend, micros", [
    ("0.10", 100_000), ("1234.565", 1_234_565_000), ("0", 0),
    ("0.000001", 1), ("19.99", 19_990_000), ("1e2", 100_000_000),
    ("8.20", 8_200_000), ("2.01", 2_010_000),
])
def test_10_spend_becomes_micros_exactly(spend, micros):
    assert connector.spend_to_micros(spend) == micros
    # The float route loses a micro on these two, which is why it is not used.
    assert int(float("8.20") * 1_000_000) == 8_199_999
    assert int(float("2.01") * 1_000_000) == 2_009_999


@pytest.mark.parametrize("spend", [None, "", "abc", "-1.00", "NaN", "Infinity",
                                    0.1, True, "0.0000001"])
def test_11_an_unreadable_amount_is_an_error_never_a_zero(spend):
    with pytest.raises(connector.MetaAdsMalformedResponse):
        connector.spend_to_micros(spend)


# ── paging ────────────────────────────────────────────────────────────────

def _many(graph, campaigns=45, days=28):
    start = YESTERDAY - timedelta(days=days - 1)
    for c in range(campaigns):
        for d in range(days):
            graph.spend(f"1202112345678{c:05d}", start + timedelta(days=d),
                        f"{c + 1}.{d:02d}", name=f"Campaign {c}")
    return campaigns * days


def test_12_every_page_is_read_and_paging_next_is_never_followed(monkeypatch):
    graph = FakeGraph()
    total = _many(graph)                                   # 1,260 rows
    fakes.configure(monkeypatch, graph)
    stats: dict = {}
    pages = list(connector.iter_campaign_daily_spend(
        ACCOUNT_ID, WINDOW_START.isoformat(), YESTERDAY.isoformat(), stats))
    assert [len(p) for p in pages] == [500, 500, 260]
    rows = [r for p in pages for r in p]
    assert len(rows) == total
    assert len({(r["campaign_id"], r["spend_date"]) for r in rows}) == total
    assert stats["pages_read"] == 3
    calls = graph.insight_calls("campaign")
    assert [c["params"].get("after") for c in calls] == [None, "500", "1000"]
    # Each page is our own query plus `after` — the same URL, the same fields.
    assert len({c["url"] for c in calls}) == 1
    assert len({c["params"]["time_range"] for c in calls}) == 1
    assert json.loads(calls[0]["params"]["time_range"]) == {
        "since": WINDOW_START.isoformat(), "until": YESTERDAY.isoformat()}
    assert calls[0]["params"]["limit"] == 500
    assert calls[0]["params"]["level"] == "campaign"
    assert calls[0]["params"]["time_increment"] == 1
    # The fake handed out two `next` URLs, each with the token in it.
    assert len(graph.next_urls) == 2 and all(SENTINEL_TOKEN in u for u in graph.next_urls)
    assert not [c for c in graph.calls if c["url"] in graph.next_urls]


def test_13_paging_ends_only_when_a_page_has_no_next(monkeypatch):
    graph = FakeGraph()
    _many(graph, campaigns=3, days=4)                      # 12 rows
    graph.max_limit = 5                                    # the API caps the page
    fakes.configure(monkeypatch, graph)
    pages = list(connector.iter_campaign_daily_spend(
        ACCOUNT_ID, "2026-11-13", "2026-11-16"))
    # A short page is not the end: 5, 5, 2.
    assert [len(p) for p in pages] == [5, 5, 2]


def test_14_paging_that_never_ends_raises(monkeypatch):
    graph = _seeded()
    graph.endless = True
    fakes.configure(monkeypatch, graph)
    monkeypatch.setattr(connector, "MAX_PAGES", 6)
    with pytest.raises(connector.MetaAdsPageOverrun):
        list(connector.iter_campaign_daily_spend(
        ACCOUNT_ID, "2026-11-16", "2026-11-17"))
    assert len(graph.insight_calls("campaign")) == 6


def test_15_a_200_without_a_data_list_raises(monkeypatch):
    graph = _seeded()
    graph.no_data_key = True
    fakes.configure(monkeypatch, graph)
    with pytest.raises(connector.MetaAdsMalformedResponse, match="without a list"):
        list(connector.iter_campaign_daily_spend(
        ACCOUNT_ID, "2026-11-16", "2026-11-17"))


def test_16_a_campaign_day_returned_twice_across_pages_raises(monkeypatch):
    graph = FakeGraph()
    _many(graph, campaigns=3, days=4)
    graph.max_limit = 5
    graph.repeat_first_row = True
    fakes.configure(monkeypatch, graph)
    with pytest.raises(connector.MetaAdsMalformedResponse, match="returned twice"):
        list(connector.iter_campaign_daily_spend(
        ACCOUNT_ID, "2026-11-13", "2026-11-16"))


def test_17_a_next_page_without_a_cursor_raises(monkeypatch):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    graph.queue.append(FakeResponse(200, {"data": [], "paging": {
        "next": f"https://graph.facebook.com/x?access_token={SENTINEL_TOKEN}"}}))
    with pytest.raises(connector.MetaAdsMalformedResponse) as caught:
        list(connector.iter_campaign_daily_spend(
        ACCOUNT_ID, "2026-11-16", "2026-11-17"))
    assert SENTINEL_TOKEN not in str(caught.value)


def test_18_rows_outside_the_range_or_spanning_days_raise(monkeypatch):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    row = {"campaign_id": CAMPAIGN_A, "spend": "1.00",
           "date_start": "2026-11-18", "date_stop": "2026-11-18"}
    graph.queue.append(FakeResponse(200, {"data": [row], "paging": {}}))
    with pytest.raises(connector.MetaAdsMalformedResponse, match="outside"):
        list(connector.iter_campaign_daily_spend(
        ACCOUNT_ID, "2026-11-16", "2026-11-17"))
    graph.queue.append(FakeResponse(200, {"data": [
        {**row, "date_start": "2026-11-16", "date_stop": "2026-11-17"}],
        "paging": {}}))
    with pytest.raises(connector.MetaAdsMalformedResponse, match="one day"):
        list(connector.iter_campaign_daily_spend(
        ACCOUNT_ID, "2026-11-16", "2026-11-17"))


# ── retries and refusals ──────────────────────────────────────────────────

def test_19_a_throttle_as_http_400_code_17_is_retried_then_succeeds(monkeypatch):
    graph = _seeded()
    sleeps = fakes.configure(monkeypatch, graph)
    graph.queue.append(error_response(400, 17, subcode=2446079,
                                      message="User request limit reached"))
    account = connector.fetch_account(ACCOUNT_ID)
    assert account["currency_code"] == "GBP"
    assert account["time_zone_name"] == "America/Los_Angeles"
    assert len(graph.calls) == 2 and sleeps == [2.0]


@pytest.mark.parametrize("code", sorted(connector.THROTTLE_CODES))
def test_20_a_hard_throttle_stops_after_four_attempts(monkeypatch, code):
    graph = _seeded()
    sleeps = fakes.configure(monkeypatch, graph)
    graph.queue.extend(error_response(400, code) for _ in range(10))
    with pytest.raises(connector.MetaAdsRetriesExhausted) as caught:
        connector.fetch_account(ACCOUNT_ID)
    assert len(graph.calls) == connector.MAX_ATTEMPTS == 4
    assert sleeps == [2.0, 4.0, 8.0]                 # no wait after the last
    assert f"HTTP 400, code {code}" in str(caught.value)
    assert caught.value.reason == "meta_retries_exhausted"


def test_21_the_wait_honours_metas_estimate_up_to_the_cap(monkeypatch):
    graph = _seeded()
    sleeps = fakes.configure(monkeypatch, graph)
    usage = json.dumps({ACCOUNT_ID: [{"type": "ads_insights", "call_count": 100,
                                      "estimated_time_to_regain_access": 0.5}]})
    graph.queue.append(error_response(400, 80000, headers={
        "x-business-use-case-usage": usage}))
    graph.queue.append(error_response(400, 80000, headers={
        "x-business-use-case-usage": usage.replace("0.5", "45")}))
    connector.fetch_account(ACCOUNT_ID)
    assert sleeps == [30.0, float(connector.MAX_WAIT_SECONDS)]   # 30s, then capped


@pytest.mark.parametrize("response", [
    FakeResponse(500, None, text="<html>Internal Server Error</html>"),
    FakeResponse(503, {"error": {"message": "Service temporarily unavailable",
                                 "code": 2, "is_transient": True}}),
    FakeResponse(429, None, text="Too Many Requests"),
    FakeResponse(400, {"error": {"message": "Please retry", "code": 1,
                                 "is_transient": True}}),
])
def test_22_5xx_429_and_transient_errors_are_retried_and_bounded(monkeypatch, response):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    graph.queue.extend([response] * 10)
    with pytest.raises(connector.MetaAdsRetriesExhausted) as caught:
        connector.fetch_account(ACCOUNT_ID)
    assert len(graph.calls) == 4
    assert f"HTTP {response.status_code}" in str(caught.value)


def test_23_a_transport_error_is_retried_and_named_by_class_only(monkeypatch, caplog):
    graph = _seeded()
    sleeps = fakes.configure(monkeypatch, graph)
    caplog.set_level(logging.DEBUG)
    graph.queue.append(fakes.transport_error())
    assert connector.fetch_account(ACCOUNT_ID)["account_id"] == ACCOUNT_ID
    assert len(graph.calls) == 2 and sleeps == [2.0]
    graph.calls.clear()
    graph.queue.extend(fakes.transport_error() for _ in range(10))
    with pytest.raises(connector.MetaAdsRetriesExhausted) as caught:
        connector.fetch_account(ACCOUNT_ID)
    assert len(graph.calls) == 4
    assert str(caught.value) == ("Meta API still failing after 4 attempts: "
                                 "transport error (ConnectionError)")
    # The requests exception (whose text holds the URL) is not chained on.
    assert caught.value.__cause__ is None and caught.value.__context__ is None \
        or SENTINEL_TOKEN not in repr(caught.value.__cause__)
    assert "graph.facebook.com" not in str(caught.value)
    assert SENTINEL_TOKEN not in caplog.text and "access_token" not in caplog.text


@pytest.mark.parametrize("status, code, subcode", [
    (400, 100, 33), (400, 2500, None), (403, 200, None), (403, 10, None),
    (404, 803, None), (400, 3018, None),
])
def test_24_every_other_4xx_is_refused_at_once(monkeypatch, status, code, subcode):
    graph = _seeded()
    sleeps = fakes.configure(monkeypatch, graph)
    graph.queue.append(error_response(status, code, subcode=subcode,
                                      message="(#%s) Not permitted"))
    with pytest.raises(connector.MetaAdsRefused) as caught:
        connector.fetch_account(ACCOUNT_ID)
    assert len(graph.calls) == 1 and sleeps == []
    text = str(caught.value)
    assert text.startswith(f"Meta API refused the request: HTTP {status}, code {code}")
    if subcode:
        assert f"subcode {subcode}" in text
    assert caught.value.reason == "meta_api_refused"


def test_25_a_4xx_with_no_error_body_is_refused_with_its_status(monkeypatch):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    graph.queue.append(FakeResponse(404, None, text="not found"))
    with pytest.raises(connector.MetaAdsRefused) as caught:
        connector.fetch_account(ACCOUNT_ID)
    assert str(caught.value) == "Meta API refused the request: HTTP 404, code None"
    assert len(graph.calls) == 1


@pytest.mark.parametrize("status, subcode", [(400, 463), (401, 460), (400, None)])
def test_26_an_expired_token_is_an_auth_error_and_never_retried(monkeypatch, status, subcode):
    graph = _seeded()
    sleeps = fakes.configure(monkeypatch, graph)
    graph.queue.append(error_response(
        status, 190, subcode=subcode,
        message="Error validating access token: Session has expired"))
    with pytest.raises(connector.MetaAdsAuthError) as caught:
        connector.fetch_account(ACCOUNT_ID)
    assert str(caught.value) == (
        f"Meta access token expired or invalid (HTTP {status}, code 190, "
        f"subcode {subcode})")
    assert len(graph.calls) == 1 and sleeps == []
    assert caught.value.reason == "meta_token_rejected"


def test_27_an_error_message_that_echoes_the_token_is_scrubbed(monkeypatch):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    graph.queue.append(error_response(
        400, 100, message=f"Invalid parameter access_token={SENTINEL_TOKEN} near "
                          f"{SENTINEL_TOKEN}"))
    with pytest.raises(connector.MetaAdsRefused) as caught:
        connector.fetch_account(ACCOUNT_ID)
    assert SENTINEL_TOKEN not in str(caught.value)
    assert "[redacted]" in str(caught.value)


def test_28_the_account_must_be_the_configured_one_with_currency_and_zone(monkeypatch):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    graph.queue.append(FakeResponse(200, {"account_id": "999", "currency": "USD",
                                          "timezone_name": "Europe/London"}))
    with pytest.raises(connector.MetaAdsMalformedResponse, match="not the account asked for"):
        connector.fetch_account(ACCOUNT_ID)
    graph.queue.append(FakeResponse(200, {"account_id": ACCOUNT_ID,
                                          "timezone_name": "Europe/London"}))
    with pytest.raises(connector.MetaAdsMalformedResponse, match="no currency"):
        connector.fetch_account(ACCOUNT_ID)


# ── the sync step: not configured ─────────────────────────────────────────

def test_29_unset_is_skipped_with_no_http_call_and_no_write(monkeypatch, store):
    graph = _seeded()
    fakes.unconfigure(monkeypatch)
    monkeypatch.setattr(requests, "get", graph.get)
    out, errors = _run()
    assert out["status"] == "skipped" and out["reason"] == "not_configured"
    assert errors == []
    assert graph.calls == []
    assert (store.batches, store.finished, store.replaced, store.failures,
            store.synced) == ([], [], [], [], [])
    assert store.accounts == {}
    # No figure of any kind: not a zero.
    assert not ({"rows_written", "cost_micros", "accounts"} & set(out))


def _no_history(monkeypatch, available=True):
    from db import meta_ads_repository as repo
    monkeypatch.setattr(repo, "fetch_meta_accounts", lambda: {
        "available": available, "accounts": []})
    monkeypatch.setattr(repo, "fetch_meta_account_sync_states", lambda: {
        "available": available, "states": {}})


def test_30_unset_reads_as_not_connected_with_every_figure_none(monkeypatch):
    fakes.unconfigure(monkeypatch)
    _no_history(monkeypatch)
    truth = truth_service.build_meta_ads_spend_truth_for_days(
        date(2026, 11, 1), date(2026, 11, 17), settled=True, now=NOW)
    assert truth["state"] == "not_connected"
    assert truth["spend_coverage_status"] == "not_connected"
    for key in ("native_spend", "usd_spend", "native_currency", "fx_reason",
                "unsettled_reason"):
        assert truth[key] is None, key
    assert truth["accounts"] == [] and truth["account_ids"] == []
    assert truth["meta_ads_spend_source"] == "unavailable"
    # A database that cannot be read is not "not connected": whether history
    # exists is unknown.
    _no_history(monkeypatch, available=False)
    unknown = truth_service.build_meta_ads_spend_truth_for_days(
        date(2026, 11, 1), date(2026, 11, 17))
    assert unknown["state"] == "source_unavailable" and unknown["usd_spend"] is None


@pytest.mark.parametrize("env", [
    {"META_ADS_ACCESS_TOKEN": SENTINEL_TOKEN},
    {"META_AD_ACCOUNT_ID": ACCOUNT_ID},
    {"META_ADS_ACCESS_TOKEN": SENTINEL_TOKEN, "META_AD_ACCOUNT_ID": ACCOUNT_ID,
     "META_API_VERSION": "newest"},
    {"META_ADS_ACCESS_TOKEN": SENTINEL_TOKEN, "META_AD_ACCOUNT_ID": ACCOUNT_ID,
     "META_ADS_ACCESS_TOKEN_2": SENTINEL_TOKEN_2,
     "META_AD_ACCOUNT_ID_2": f"{OLD_ACCOUNT_ID}, act_{ACCOUNT_ID}"},
])
def test_31_a_configuration_nothing_can_sync_from_fails_without_a_call(
        monkeypatch, store, env):
    graph = _seeded()
    fakes.unconfigure(monkeypatch)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(requests, "get", graph.get)
    out, errors = _run()
    assert out["status"] == "failed" and out["reason"] == "meta_ads_misconfigured"
    assert len(errors) == 1 and "meta_ads_misconfigured" in errors[0]
    assert graph.calls == [] and store.batches == [] and store.replaced == []
    for token in (SENTINEL_TOKEN, SENTINEL_TOKEN_2):
        assert token not in json.dumps([out, errors, connector.config_status()])


def test_32_a_broken_configuration_is_unavailable_not_not_connected(monkeypatch):
    fakes.unconfigure(monkeypatch)
    monkeypatch.setenv("META_ADS_ACCESS_TOKEN", SENTINEL_TOKEN)
    truth = truth_service.build_meta_ads_spend_truth_for_days(
        date(2026, 11, 1), date(2026, 11, 17))
    assert truth["state"] == "source_unavailable"
    assert truth["native_spend"] is None and truth["usd_spend"] is None


# ── the sync step: the two production accounts ────────────────────────────

def test_33_the_two_production_accounts_each_sync_in_their_own_zone(
        monkeypatch, store):
    """06:00 UTC. The old account (USD, Asia/Amman): Amman's yesterday, the
    17th, ended at 21:00 UTC. The new one (GBP, America/Los_Angeles): it is
    22:00 on the 17th there, so its last complete day is the 16th."""
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    out, errors = _run()
    assert out["status"] == "success" and errors == []
    new, old = _acct(out, 0), _acct(out, 1)
    assert (new["account_id"], new["chunk_start"], new["chunk_end"]) == (
        ACCOUNT_ID, "2026-10-20", "2026-11-16")
    assert (old["account_id"], old["chunk_start"], old["chunk_end"]) == (
        OLD_ACCOUNT_ID, "2026-10-21", "2026-11-17")
    assert (new["account_time_zone"], old["account_time_zone"]) == (
        "America/Los_Angeles", "Asia/Amman")
    assert (new["currency"], old["currency"]) == ("GBP", "USD")
    # One batch for the step, spanning both windows; proven through the
    # EARLIER of the two last days.
    assert store.batches == [{
        "source": "meta_ads_api", "dataset": "meta_campaign_spend",
        "sync_type": "daily", "date_from": date(2026, 10, 20),
        "date_to": date(2026, 11, 17), "run_id": 7}]
    assert store.finished == [{"batch_id": 1, "status": "success", "row_count": 3,
                               "error_message": None,
                               "last_source_date": "2026-11-16"}]
    # The new account has no delivery: a verified zero. The old one has spend.
    by_account = {r["account_id"]: r for r in store.replaced}
    assert by_account[ACCOUNT_ID]["rows"] == [] and new["verified_zero"] is True
    assert sorted((r["campaign_id"], r["spend_date"], r["cost_micros"])
                  for r in by_account[OLD_ACCOUNT_ID]["rows"]) == [
        (CAMPAIGN_A, "2026-11-15", 70_000_000), (CAMPAIGN_A, "2026-11-16", 60_340_000),
        (CAMPAIGN_B, "2026-11-17", 12_000_000)]
    assert by_account[OLD_ACCOUNT_ID]["currency_code"] == "USD"
    assert by_account[OLD_ACCOUNT_ID]["account_time_zone"] == "Asia/Amman"
    assert store.fx_calls == []            # USD is an identity; zero needs no rate
    # Neither account's day in progress was asked for.
    for account_id, until in ((ACCOUNT_ID, "2026-11-16"), (OLD_ACCOUNT_ID, "2026-11-17")):
        calls = graph.insight_calls(account_id=account_id)
        assert len(calls) == 2
        assert {json.loads(c["params"]["time_range"])["until"] for c in calls} == {until}
    # Each account was called with its own credential set's token, only.
    assert {c["headers"]["Authorization"] for c in graph.calls
            if c["account_id"] == ACCOUNT_ID} == {f"Bearer {SENTINEL_TOKEN}"}
    assert {c["headers"]["Authorization"] for c in graph.calls
            if c["account_id"] == OLD_ACCOUNT_ID} == {f"Bearer {SENTINEL_TOKEN_2}"}
    assert [(s["account_id"], s["status"], s["readable"]) for s in store.synced] == [
        (ACCOUNT_ID, "success", True), (OLD_ACCOUNT_ID, "success", True)]
    assert out["external_writes_performed"] is False


def test_33b_a_gbp_account_with_spend_is_priced_for_its_spend_days(monkeypatch, store):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    out, errors = _run()
    assert out["status"] == "success" and errors == []
    written = store.replaced[0]
    assert (written["start"], written["end"]) == (WINDOW_START, YESTERDAY)
    assert sorted((r["campaign_id"], r["spend_date"], r["cost_micros"], r["spend_raw"])
                  for r in written["rows"]) == [
        (CAMPAIGN_A, "2026-11-15", 120_500_000, "120.50"),
        (CAMPAIGN_A, "2026-11-16", 98_100_000, "98.10"),
        (CAMPAIGN_B, "2026-11-16", 100_000, "0.10")]
    assert sorted((d["spend_date"], d["cost_micros"]) for d in written["account_days"]) == [
        ("2026-11-15", 120_500_000), ("2026-11-16", 98_200_000)]
    # GBP goes through the existing GBP→USD rates, for the spend days only.
    start, end, kw = store.fx_calls[0]
    assert (start, end) == (date(2026, 11, 15), date(2026, 11, 16))
    assert kw["base_currency"] == "GBP" and kw["quote_currency"] == "USD"
    assert _acct(out)["fx_status"] == "ok"
    assert IN_PROGRESS.isoformat() not in json.dumps(written, default=str)


def test_34_an_empty_verified_response_is_a_verified_zero(monkeypatch, store):
    """Production on 2026-11-22: `{'data': []}` — no delivery. No campaign
    rows and no account-level rows reconcile (nothing equals nothing), the
    range is replaced with nothing and recorded verified: a verified zero."""
    graph = FakeGraph()
    fakes.configure(monkeypatch, graph)
    out, errors = _run()
    account = _acct(out)
    assert out["status"] == "success" and account["verified_zero"] is True
    assert account["rows_written"] == 0 and account["cost_micros"] == 0
    assert account["days_reconciled"] == 0
    assert store.replaced[0]["rows"] == [] and store.replaced[0]["account_days"] == []
    assert store.failures == [] and errors == []
    assert store.fx_calls == []                  # nothing to price
    assert store.finished[0]["status"] == "success"


def test_34b_a_long_stretch_without_delivery_between_two_days_of_spend(
        monkeypatch, store):
    graph = FakeGraph()
    graph.spend(CAMPAIGN_A, "2026-10-21", "10.00")
    graph.spend(CAMPAIGN_A, "2026-11-16", "12.00")       # 25 silent days between
    fakes.configure(monkeypatch, graph)
    out, errors = _run()
    assert out["status"] == "success" and errors == []
    assert out["rows_written"] == 2 and _acct(out)["days_reconciled"] == 2
    assert [d["spend_date"] for d in store.replaced[0]["account_days"]] == [
        "2026-10-21", "2026-11-16"]


@pytest.mark.parametrize("currency, missing, reason, asked", [
    ("GBP", ["2026-11-16"], "fx_rates_missing", True),
    ("JOD", [], "fx_currency_not_supported", False),
    ("AED", [], "fx_currency_not_supported", False),
])
def test_35_verified_spend_without_usd_rates_is_partial_and_stays_verified(
        monkeypatch, store, currency, missing, reason, asked):
    graph = _seeded(currency=currency)
    fakes.configure(monkeypatch, graph)
    store.fx_missing = missing
    out, errors = _run()
    assert out["status"] == "partial" and out["reason"] == reason
    assert _acct(out)["coverage_status"] == "verified" and out["rows_written"] == 3
    assert len(store.replaced) == 1 and store.failures == []
    # `partial` reaches the batch as `partial` — not success, not failed.
    assert store.finished[0]["status"] == "partial"
    assert store.finished[0]["error_message"].startswith(f"{reason}: account {ACCOUNT_ID}")
    assert store.finished[0]["row_count"] == 3
    assert store.finished[0]["last_source_date"] is None
    assert bool(store.fx_calls) is asked         # no rate is requested or invented
    assert len(errors) == 1 and ACCOUNT_ID in errors[0]
    assert store.synced[0]["status"] == "partial" and store.synced[0]["reason"] == reason


def test_36_a_usd_account_needs_no_rate_at_all(monkeypatch, store):
    graph = _seeded(currency="USD")
    fakes.configure(monkeypatch, graph)
    out, _ = _run()
    assert out["status"] == "success" and _acct(out)["fx_status"] == "ok"
    assert store.fx_calls == []                  # identity, not a rate lookup


DEPRECATED = ("(#2635) You are calling a deprecated version of the Ads API. "
              "Please upgrade to the latest version: v27.0.")


def test_36b_a_deprecated_api_version_is_named_and_never_retried(monkeypatch, store):
    """Production, 2026-11-22: HTTP 400, code 2635."""
    graph = _seeded()
    sleeps = fakes.configure(monkeypatch, graph, version="v22.0")
    with pytest.raises(connector.MetaAdsVersionDeprecated) as caught:
        connector.fetch_account(ACCOUNT_ID)
    assert str(caught.value) == ("Meta API version v22.0 is deprecated; Meta says "
                                 "upgrade to v27.0 (HTTP 400, code 2635)")
    assert caught.value.upgrade_to == "v27.0"
    assert caught.value.reason == "meta_api_version_deprecated"
    assert len(graph.calls) == 1 and sleeps == []
    # ...and through the sync step: the run error says it in those words.
    graph.calls.clear()
    out, errors = _run()
    assert out["status"] == "failed" and out["reason"] == "meta_api_version_deprecated"
    assert (f"account {ACCOUNT_ID}: Meta API version v22.0 is deprecated; Meta says "
            "upgrade to v27.0") in errors[0]
    assert store.finished[-1]["error_message"].startswith(
        f"meta_api_version_deprecated: account {ACCOUNT_ID}: Meta API version v22.0")
    assert len(graph.calls) == 1 and store.replaced == []
    # It says nothing about whether the ACCOUNT is readable.
    assert store.synced[-1]["readable"] is None
    # A message that names no version is still a plain, distinct refusal.
    graph.queue.append(error_response(400, 2635, message="(#2635) Deprecated."))
    with pytest.raises(connector.MetaAdsVersionDeprecated,
                       match="did not name a version"):
        connector.fetch_account(ACCOUNT_ID)


def _assert_failed(out, errors, store, reason, *, coverage=True):
    assert out["status"] == "failed" and out["reason"] == reason
    assert _acct(out)["status"] == "failed" and _acct(out)["reason"] == reason
    assert store.replaced == []                              # nothing replaced
    assert store.finished[-1]["status"] == "failed"
    assert store.finished[-1]["error_message"].startswith(f"{reason}: account {ACCOUNT_ID}")
    assert store.finished[-1]["row_count"] == 0
    assert store.finished[-1]["last_source_date"] is None
    assert len(errors) == 1 and ACCOUNT_ID in errors[0]      # names the account
    assert store.synced[-1]["status"] == "failed" and store.synced[-1]["reason"] == reason
    if coverage:
        assert len(store.failures) == 1
        assert store.failures[0]["error_code"] == reason
        assert (store.failures[0]["start"], store.failures[0]["end"]) == (
            WINDOW_START, YESTERDAY)
    for token in (SENTINEL_TOKEN, SENTINEL_TOKEN_2):
        assert token not in json.dumps([out, errors, store.finished, store.failures,
                                        store.synced], default=str)


def test_37_an_expired_token_on_the_first_run_fails_with_no_window(monkeypatch, store):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    graph.queue.append(error_response(400, 190, subcode=463))
    out, errors = _run()
    # No account has ever been read, so there is no zone to date a window by:
    # the batch carries no dates and no coverage row is invented.
    _assert_failed(out, errors, store, "meta_token_rejected", coverage=False)
    assert store.batches[0]["date_from"] is None and store.batches[0]["date_to"] is None
    assert store.failures == []
    assert "code 190, subcode 463" in out["error"]
    assert len(graph.calls) == 1


def test_38_an_expired_token_later_is_dated_by_the_zone_on_record(monkeypatch, store):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    store.accounts[ACCOUNT_ID] = {"account_id": ACCOUNT_ID, "currency_code": "GBP",
                                  "time_zone_name": "America/Los_Angeles"}
    graph.queue.append(error_response(400, 190, subcode=460))
    out, errors = _run()
    _assert_failed(out, errors, store, "meta_token_rejected")
    assert store.batches[0]["date_to"] == YESTERDAY
    assert store.synced[-1]["readable"] is False


@pytest.mark.parametrize("arrange, reason", [
    (lambda g: g.queue.extend([None, error_response(403, 200)]),
     "meta_account_not_readable"),
    (lambda g: g.queue.extend([None] + [error_response(400, 17)] * 4),
     "meta_retries_exhausted"),
    (lambda g: g.queue.extend([None] + [fakes.transport_error()] * 4),
     "meta_retries_exhausted"),
    (lambda g: setattr(g, "no_data_key", True), "meta_malformed_response"),
    (lambda g: setattr(g, "endless", True), "meta_page_overrun"),
    (lambda g: g.hidden.add((CAMPAIGN_B, "2026-11-16")), "meta_reconciliation_mismatch"),
])
def test_39_every_failure_replaces_nothing_and_records_a_failed_chunk(
        monkeypatch, store, caplog, arrange, reason):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    monkeypatch.setattr(connector, "MAX_PAGES", 5)
    caplog.set_level(logging.DEBUG)
    account_response = graph.get(
        f"https://graph.facebook.com/{fakes.VERSION}/act_{ACCOUNT_ID}",
        params={"fields": connector.ACCOUNT_FIELDS},
        headers={"Authorization": f"Bearer {SENTINEL_TOKEN}"}, timeout=5)
    graph.calls.clear()
    arrange(graph)
    # `None` in the queue stands for "the account request succeeds".
    graph.queue = [account_response if item is None else item for item in graph.queue]
    out, errors = _run()
    _assert_failed(out, errors, store, reason)
    assert SENTINEL_TOKEN not in caplog.text and "access_token" not in caplog.text


def test_40_a_campaign_missing_from_the_campaign_query_fails_reconciliation(
        monkeypatch, store):
    """The reconciliation gate. Meta returns an account total that includes a
    campaign-day the campaign-level query left out; without the gate the
    smaller sum would be stored and called verified."""
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    graph.hidden.add((CAMPAIGN_A, "2026-11-15"))
    out, errors = _run()
    _assert_failed(out, errors, store, "meta_reconciliation_mismatch")
    assert "2026-11-15" in out["error"] and "120500000" in out["error"]


def test_41_a_difference_of_one_minor_unit_reconciles_and_two_do_not():
    rows = [{"spend_date": "2026-11-17", "cost_micros": 98_100_000}]
    def days(micros):
        return [{"spend_date": "2026-11-17", "cost_micros": micros}]
    usd = meta.reconciliation_tolerance_micros("USD")
    jod = meta.reconciliation_tolerance_micros("JOD")
    assert (usd, jod) == (10_000, 1_000)
    assert meta.reconciliation_tolerance_micros("JPY") == 10_000
    assert meta.reconcile_days(rows, days(98_110_000), usd)["reconciled"] is True
    assert meta.reconcile_days(rows, days(98_120_000), usd)["reconciled"] is False
    assert meta.reconcile_days(rows, days(98_101_000), jod)["reconciled"] is True
    assert meta.reconcile_days(rows, days(98_102_000), jod)["reconciled"] is False
    # A day on one side only is compared with zero.
    assert meta.reconcile_days(rows, [], usd)["reconciled"] is False
    assert meta.reconcile_days([], days(5_000_000), usd)["mismatches"][0][
        "delta_micros"] == -5_000_000
    assert meta.reconcile_days([], [], usd) == {
        "reconciled": True, "mismatches": [], "days_compared": 0}


def test_42_a_persistence_error_fails_the_chunk(monkeypatch, store):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    store.replace_error = db_writers.MetaAdsPersistenceError("inserted 2 of 3 rows")
    out, errors = _run()
    _assert_failed(out, errors, store, "meta_persistence_error")


def test_43_a_changed_account_identity_fails_that_account(monkeypatch, store):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    store.accounts[ACCOUNT_ID] = {"account_id": ACCOUNT_ID, "currency_code": "USD",
                                  "time_zone_name": "America/Los_Angeles"}
    store.identity_error = db_writers.MetaAccountIdentityChanged(
        "account_identity_changed: currency on record is USD, the API reports GBP")
    out, errors = _run()
    _assert_failed(out, errors, store, "account_identity_changed")
    assert graph.insight_calls() == []               # no spend was even requested


def test_44_a_row_in_another_currency_than_the_account_fails(monkeypatch, store):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    real = graph._insights

    def other_currency(account, params):
        response = real(account, params)
        for row in response.json()["data"]:
            row["account_currency"] = "EUR"
        return response
    monkeypatch.setattr(graph, "_insights", other_currency)
    out, errors = _run()
    _assert_failed(out, errors, store, "meta_malformed_response")


def test_45_more_rows_than_one_chunk_may_hold_is_refused(monkeypatch, store):
    graph = FakeGraph()
    _many(graph, campaigns=3, days=4)
    fakes.configure(monkeypatch, graph)
    monkeypatch.setattr(meta, "MAX_ROWS_PER_CHUNK", 10)
    out, errors = _run()
    _assert_failed(out, errors, store, "meta_chunk_row_limit")


def test_46_a_batch_that_cannot_be_opened_fails_before_any_spend_request(
        monkeypatch, store):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)
    monkeypatch.setattr(db_writers, "start_sync_batch", lambda **kw: 0)
    out, errors = _run()
    assert out["status"] == "failed" and out["reason"] == "sync_batch_not_opened"
    assert graph.insight_calls() == [] and store.replaced == []


def test_47_an_unexpected_exception_is_named_by_class_and_isolated(monkeypatch, store):
    graph = _seeded()
    fakes.configure(monkeypatch, graph)

    def boom(*a, **k):
        raise RuntimeError(f"GET https://graph.facebook.com/?access_token={SENTINEL_TOKEN}")
    monkeypatch.setattr(meta, "sync_prepared", boom)
    out, errors = _run()
    assert out == {"status": "failed", "reason": "meta_sync_error",
                   "error": "unexpected RuntimeError",
                   "external_writes_performed": False}
    assert store.finished[-1]["status"] == "failed"
    assert SENTINEL_TOKEN not in json.dumps([out, errors, store.finished])


# ── several accounts, several credential sets ─────────────────────────────

def test_47a_one_account_becoming_unreadable_does_not_stop_the_other(
        monkeypatch, store, caplog):
    """The old account is suspended: Meta refuses every read of it. The new
    account still syncs; the run is partial; nothing of the old account's is
    touched; the message is plain and names the account."""
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    caplog.set_level(logging.DEBUG)
    store.accounts[OLD_ACCOUNT_ID] = {"account_id": OLD_ACCOUNT_ID,
                                      "currency_code": "USD",
                                      "time_zone_name": "Asia/Amman"}
    graph.accounts[OLD_ACCOUNT_ID].refusal = error_response(
        403, 200, message="(#200) Ad account owner has NOT grant ads_read permission")
    out, errors = _run()
    assert out["status"] == "partial" and out["reason"] == "meta_account_not_readable"
    new, old = _acct(out, 0), _acct(out, 1)
    assert new["status"] == "success" and old["status"] == "failed"
    assert [r["account_id"] for r in store.replaced] == [ACCOUNT_ID]
    assert len(errors) == 1
    assert errors[0].startswith(
        f"meta_ads_api/meta_campaign_spend: account {OLD_ACCOUNT_ID} is not "
        "readable; its stored history is kept (Meta API refused the request: "
        "HTTP 403, code 200")
    assert store.finished == [{
        "batch_id": 1, "status": "partial", "row_count": 0,
        "error_message": store.finished[0]["error_message"],
        "last_source_date": None}]
    assert store.finished[0]["error_message"].startswith(
        f"meta_account_not_readable: account {OLD_ACCOUNT_ID} is not readable")
    # A failed coverage row dated in the old account's own zone; no delete.
    assert [(f["account_id"], f["start"], f["end"], f["http_status"])
            for f in store.failures] == [
        (OLD_ACCOUNT_ID, date(2026, 10, 21), date(2026, 11, 17), 403)]
    assert [(s["account_id"], s["status"], s["readable"]) for s in store.synced] == [
        (ACCOUNT_ID, "success", True), (OLD_ACCOUNT_ID, "failed", False)]
    assert graph.insight_calls(account_id=OLD_ACCOUNT_ID) == []
    for token in (SENTINEL_TOKEN, SENTINEL_TOKEN_2):
        assert token not in caplog.text
        assert token not in json.dumps([out, errors, store.finished, store.failures,
                                        store.synced], default=str)


def test_47b_every_account_failing_is_failed_and_mixed_outcomes_are_partial():
    ok = {"account_id": "1", "status": "success", "coverage_status": "verified",
          "chunk_end": "2026-11-16", "rows_written": 2}
    fx = {"account_id": "2", "status": "partial", "coverage_status": "verified",
          "chunk_end": "2026-11-17", "rows_written": 1, "reason": "fx_rates_missing",
          "error": "no USD rate"}
    bad = {"account_id": "3", "status": "failed", "reason": "meta_token_rejected",
           "error": "expired", "rows_written": 0}
    assert meta.aggregate([ok, dict(ok, account_id="9")])["status"] == "success"
    assert meta.aggregate([ok, bad])["status"] == "partial"
    assert meta.aggregate([ok, fx])["status"] == "partial"
    assert meta.aggregate([fx])["status"] == "partial"
    assert meta.aggregate([bad, dict(bad, account_id="4")])["status"] == "failed"
    assert meta.aggregate([])["status"] == "failed"      # nothing ran: not a success
    mixed = meta.aggregate([ok, fx, bad])
    assert mixed["reason"] == "fx_rates_missing" and mixed["rows_written"] == 3
    assert mixed["errors"] == ["account 2: no USD rate", "account 3: expired"]
    # Proven-through is given only when EVERY account was verified.
    assert mixed["last_source_date"] is None
    assert meta.aggregate([ok, fx])["last_source_date"] == "2026-11-16"


@pytest.mark.parametrize("raw, ids, invalid", [
    ("4730305973961806", ["4730305973961806"], []),
    ("act_4730305973961806", ["4730305973961806"], []),
    (" act_4730305973961806 , 944370805110952 ",
     ["4730305973961806", "944370805110952"], []),
    ("944370805110952,4730305973961806,act_944370805110952,,",
     ["944370805110952", "4730305973961806"], []),          # order kept, once each
    ("ACT_944370805110952", ["944370805110952"], []),
    ("4730305973961806;944370805110952", [], ["4730305973961806;944370805110952"]),
    ("4730305973961806, my account", ["4730305973961806"], ["my account"]),
    ("", [], []), (" , ", [], []),
])
def test_47c_the_account_list_is_parsed(raw, ids, invalid):
    assert connector.parse_account_ids(raw) == (ids, invalid)


def test_47d_one_token_may_read_several_accounts(monkeypatch, store):
    graph = FakeGraph()
    graph.add_account(OLD_ACCOUNT_ID, token=SENTINEL_TOKEN)      # the same token
    fakes.configure(monkeypatch, graph)
    status = connector.config_status()
    assert status["account_ids"] == [ACCOUNT_ID, OLD_ACCOUNT_ID]
    assert status["sets"] == [{"set": 1, "has_access_token": True,
                               "has_app_secret": False,
                               "account_ids": [ACCOUNT_ID, OLD_ACCOUNT_ID],
                               "configured": True, "reason": None}]
    out, _ = _run()
    assert out["status"] == "success" and out["accounts_synced"] == 2


def test_47e_credential_sets_are_reported_without_a_token(monkeypatch):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    monkeypatch.setenv("META_APP_SECRET_2", "second-app-secret-77aa")
    status = connector.config_status()
    assert status["configured"] is True and status["misconfigured"] is False
    assert status["account_ids"] == [ACCOUNT_ID, OLD_ACCOUNT_ID]
    assert status["sets"] == [
        {"set": 1, "has_access_token": True, "has_app_secret": False,
         "account_ids": [ACCOUNT_ID], "configured": True, "reason": None},
        {"set": 2, "has_access_token": True, "has_app_secret": True,
         "account_ids": [OLD_ACCOUNT_ID], "configured": True, "reason": None}]
    text = json.dumps(status)
    for secret in (SENTINEL_TOKEN, SENTINEL_TOKEN_2, "second-app-secret-77aa"):
        assert secret not in text
    # The app secret follows the suffix: only set 2's calls carry a proof,
    # and it is the HMAC of set 2's own token.
    import hashlib, hmac
    connector.fetch_account(ACCOUNT_ID)
    connector.fetch_account(OLD_ACCOUNT_ID)
    assert "appsecret_proof" not in graph.calls[0]["params"]
    assert graph.calls[1]["params"]["appsecret_proof"] == hmac.new(
        b"second-app-secret-77aa", SENTINEL_TOKEN_2.encode(), hashlib.sha256).hexdigest()


def test_47f_an_account_is_never_called_with_another_sets_token(monkeypatch):
    """The fake refuses a mismatch; swap the tokens and it does."""
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    connector.fetch_account(OLD_ACCOUNT_ID)              # its own token: fine
    monkeypatch.setenv("META_ADS_ACCESS_TOKEN", SENTINEL_TOKEN_2)
    monkeypatch.setenv("META_ADS_ACCESS_TOKEN_2", SENTINEL_TOKEN)
    with pytest.raises(AssertionError, match="not called with its own token"):
        connector.fetch_account(OLD_ACCOUNT_ID)
    # An account that is not configured is not called at all.
    graph.calls.clear()
    with pytest.raises(connector.MetaAdsNotConfigured, match="not a configured account"):
        connector.fetch_account("1111111111")
    assert graph.calls == []


def test_47g_a_half_set_is_reported_and_the_complete_sets_still_sync(
        monkeypatch, store):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    monkeypatch.delenv("META_ADS_ACCESS_TOKEN_2")        # set 2: accounts, no token
    status = connector.config_status()
    assert status["configured"] is True
    assert status["account_ids"] == [ACCOUNT_ID]         # set 2's account does not sync
    assert status["misconfigured_sets"] == [
        {"set": 2, "has_access_token": False, "has_app_secret": False,
         "account_ids": [OLD_ACCOUNT_ID], "configured": False,
         "reason": "access_token_missing"}]
    out, errors = _run()
    assert out["status"] == "partial" and out["reason"] == "meta_ads_misconfigured"
    assert _acct(out)["status"] == "success"
    assert errors == ["meta_ads_api/meta_campaign_spend: credential set 2 is "
                      "misconfigured (access_token_missing) and was not synced"]
    assert store.finished[0]["status"] == "partial"
    assert store.finished[0]["last_source_date"] is None
    assert {c["account_id"] for c in graph.calls} == {ACCOUNT_ID}
    # The other half: a token with no account list.
    monkeypatch.setenv("META_ADS_ACCESS_TOKEN_2", SENTINEL_TOKEN_2)
    monkeypatch.delenv("META_AD_ACCOUNT_ID_2")
    assert connector.config_status()["misconfigured_sets"][0]["reason"] == (
        "ad_account_id_missing")


def test_47h_an_account_listed_in_two_sets_is_misconfigured_and_named(monkeypatch):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    monkeypatch.setenv("META_AD_ACCOUNT_ID_2", f"{OLD_ACCOUNT_ID},{ACCOUNT_ID}")
    status = connector.config_status()
    assert status["configured"] is False and status["misconfigured"] is True
    assert status["reason"] == "ad_account_id_in_two_sets"
    assert status["duplicate_account_ids"] == [ACCOUNT_ID]
    assert status["account_ids"] == []
    with pytest.raises(connector.MetaAdsNotConfigured):
        connector.fetch_account(ACCOUNT_ID)
    assert graph.calls == []
    # Listing an id twice in ONE set is not a conflict.
    monkeypatch.setenv("META_AD_ACCOUNT_ID_2", f"{OLD_ACCOUNT_ID},{OLD_ACCOUNT_ID}")
    assert connector.config_status()["account_ids"] == [ACCOUNT_ID, OLD_ACCOUNT_ID]


def test_47i_there_are_at_most_five_sets_and_set_one_is_not_required(monkeypatch):
    fakes.unconfigure(monkeypatch)
    monkeypatch.setenv("META_API_VERSION", fakes.VERSION)
    monkeypatch.setenv("META_ADS_ACCESS_TOKEN_6", SENTINEL_TOKEN)
    monkeypatch.setenv("META_AD_ACCOUNT_ID_6", ACCOUNT_ID)
    status = connector.config_status()
    assert status["configured"] is False and status["misconfigured"] is False
    assert connector.MAX_CREDENTIAL_SETS == 5
    monkeypatch.setenv("META_ADS_ACCESS_TOKEN_3", SENTINEL_TOKEN_2)
    monkeypatch.setenv("META_AD_ACCOUNT_ID_3", OLD_ACCOUNT_ID)
    status = connector.config_status()
    assert status["configured"] is True and status["account_ids"] == [OLD_ACCOUNT_ID]
    assert [s["set"] for s in status["sets"]] == [3]


def test_47j_neither_token_appears_in_any_failure_of_either_set(
        monkeypatch, store, caplog):
    """Every failure path, on both credential sets, with a different token on
    each: neither token is in a log, an error, a result or a stored value —
    even when Meta's own error text and the transport error echo them."""
    caplog.set_level(logging.DEBUG)
    echo = (f"Invalid OAuth access token {SENTINEL_TOKEN} / {SENTINEL_TOKEN_2} "
            f"access_token={SENTINEL_TOKEN_2}")
    failures = [
        lambda: error_response(400, 190, subcode=463, message=echo),
        lambda: error_response(403, 200, message=echo),
        lambda: error_response(400, 2635, message=echo + " upgrade to v27.0"),
        lambda: error_response(400, 100, message=echo),
        lambda: fakes.FakeResponse(500, None, text=echo),
        lambda: fakes.transport_error(SENTINEL_TOKEN_2),
        lambda: fakes.FakeResponse(200, {"data": "x", "paging": {"next": echo}}),
    ]
    seen = []
    for make in failures:
        for target in (ACCOUNT_ID, OLD_ACCOUNT_ID):
            graph = _pair()
            fakes.configure(monkeypatch, graph)
            graph.accounts[target].refusal = None
            original = graph.get

            def get(url, params=None, headers=None, timeout=None, _t=target,
                    _make=make, _get=original, **kw):
                if f"act_{_t}" in url:
                    _get(url, params=params, headers=headers, timeout=timeout, **kw)
                    item = _make()
                    if isinstance(item, BaseException):
                        raise item
                    return item
                return _get(url, params=params, headers=headers, timeout=timeout, **kw)
            monkeypatch.setattr(requests, "get", get)
            out, errors = _run()
            assert out["status"] == "partial", out      # the other account synced
            seen.append([out, errors])
    everything = json.dumps([seen, store.finished, store.failures, store.synced,
                             store.replaced, store.accounts], default=str) + caplog.text
    for token in (SENTINEL_TOKEN, SENTINEL_TOKEN_2):
        assert token not in everything
    assert "access_token=" not in everything.replace("access_token=[redacted]", "")


# ── the run verdict and the registries ────────────────────────────────────

def test_48_skipped_does_not_vote_and_partial_is_not_success():
    assert sync._overall_status({
        "a": {"status": "success"},
        sync.LABEL_META_ADS_SPEND: {"status": "skipped"}}) == "success"
    assert sync._overall_status({
        sync.LABEL_META_ADS_SPEND: {"status": "skipped"}}) == "success"
    assert sync._overall_status({
        "a": {"status": "success"},
        sync.LABEL_META_ADS_SPEND: {"status": "partial"}}) == "partial"
    assert sync._overall_status({
        "a": {"status": "success"},
        sync.LABEL_META_ADS_SPEND: {"status": "failed"}}) == "partial"
    assert "skipped" in sync.NON_VOTING_STATUSES


def test_49_the_step_runs_right_after_fx_in_the_daily_run():
    tree = ast.parse((ROOT / "scheduler/incremental_sync.py").read_text())
    run = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
               and n.name == "run_daily_incremental_sync")
    order = [n.func.id for n in ast.walk(run) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id.startswith("_sync_")]
    ordered = sorted(
        (n.lineno, n.func.id) for n in ast.walk(run) if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name) and n.func.id.startswith("_sync_"))
    names = [name for _, name in ordered]
    assert "_sync_meta_ads_spend" in order
    assert names[names.index("_sync_fx_rates") + 1] == "_sync_meta_ads_spend"


def test_50_no_run_type_and_no_cadence_was_added():
    assert monitoring.RUN_TYPE_CADENCE == {
        "daily": "daily", "daily_incremental_sync": "daily_incremental_sync",
        "monthly": "monthly"}
    assert sync.RUN_TYPE == "daily_incremental_sync"
    for path in ("scripts/backfill_meta_ads_spend.py", "scripts/meta_ads_smoke_test.py",
                 "services/meta_ads_spend_service.py", "connectors/meta_ads.py"):
        source = (ROOT / path).read_text()
        for writer in ("write_run", "update_run(", "INSERT INTO runs"):
            assert writer not in source, (path, writer)
    assert "start_sync_batch" not in (ROOT / "scripts/meta_ads_smoke_test.py").read_text()
    assert "db." not in (ROOT / "scripts/meta_ads_smoke_test.py").read_text()


def test_51_the_pair_is_registered_once_and_every_side_imports_it():
    pair = (dataset_keys.META_ADS_SOURCE, dataset_keys.META_ADS_SPEND_DATASET)
    assert pair == ("meta_ads_api", "meta_campaign_spend")
    assert dataset_keys.is_registered_pair(*pair)
    assert pair in sync.ACTIVE_SYNC_PAIRS
    assert sync.LABEL_META_ADS_SPEND == "meta_ads_api/meta_campaign_spend"
    config = DATASET_FRESHNESS_CONFIG["meta_ads_spend"]
    assert (config["source"], config["dataset"]) == pair
    assert config["page"] is None and config["depends_on"] == []
    from services import system_status_service as status
    assert status.SOURCE_DEFINITIONS["meta_ads_api"]["datasets"] == ["meta_ads_spend"]
    assert status.PIPELINE_DEPENDENCIES["meta_ads_spend"]["page"] is None
    # Not a core dataset: an unconnected platform is not a critical failure.
    assert "meta_ads_spend" not in status.CORE_DATASETS
    for impacted in status.PAGE_PIPELINE_IMPACT.values():
        assert "meta_ads_spend" not in impacted


def test_52_the_freshness_vocabulary_is_unchanged():
    for word in ("not_connected", "misconfigured", "token_rejected"):
        assert word not in CanonicalFreshnessStatus.ALL
    assert len(CanonicalFreshnessStatus.ALL) == len(set(CanonicalFreshnessStatus.ALL))
    # Unconfigured Meta has no batch: the dataset reads `not_run`, which is
    # neutral — it raises no warning and no blocker.
    from services.freshness_service import SEVERITY_MAP
    assert SEVERITY_MAP[CanonicalFreshnessStatus.NOT_RUN] == "neutral"


def test_53_reader_contract_modules_may_not_import_the_meta_connector():
    from scripts import audit_customer_revenue_reader_cutover as audit
    assert "connectors.meta_ads" in audit.FORBIDDEN_IMPORTS


# ── chunking ──────────────────────────────────────────────────────────────

def test_54_a_backfill_range_is_cut_at_month_ends():
    assert meta.month_chunks(date(2025, 12, 15), date(2026, 3, 2)) == [
        (date(2025, 12, 15), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 2, 28)),
        (date(2026, 3, 1), date(2026, 3, 2))]
    assert meta.month_chunks(date(2024, 2, 29), date(2024, 2, 29)) == [
        (date(2024, 2, 29), date(2024, 2, 29))]
    assert meta.month_chunks(date(2026, 3, 2), date(2026, 3, 1)) == []


def test_55_a_dry_run_backfill_plans_every_account_and_writes_nothing(
        monkeypatch, store):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    out = meta.run_backfill(date(2026, 9, 10), None, dry_run=True, now=NOW)
    assert out["status"] == "success" and out["dry_run"] is True
    plans = {a["account_id"]: [(c["chunk_start"], c["chunk_end"], c["status"])
                               for c in a["chunks"]] for a in out["accounts"]}
    # Each account is capped at ITS OWN yesterday.
    assert plans == {
        ACCOUNT_ID: [("2026-09-10", "2026-09-30", "planned"),
                     ("2026-10-01", "2026-10-31", "planned"),
                     ("2026-11-01", "2026-11-16", "planned")],
        OLD_ACCOUNT_ID: [("2026-09-10", "2026-09-30", "planned"),
                         ("2026-10-01", "2026-10-31", "planned"),
                         ("2026-11-01", "2026-11-17", "planned")]}
    assert store.accounts == {} and store.batches == [] and store.replaced == []
    assert store.synced == [] and graph.insight_calls() == []
    # One account by name; an account that is not configured is refused.
    one = meta.run_backfill(date(2026, 9, 10), None, dry_run=True, now=NOW,
                            account_ids=[OLD_ACCOUNT_ID])
    assert [a["account_id"] for a in one["accounts"]] == [OLD_ACCOUNT_ID]
    other = meta.run_backfill(date(2026, 9, 10), None, dry_run=True, now=NOW,
                              account_ids=["1111111111"])
    assert other["status"] == "failed" and "1111111111" in other["error"]


def test_56_a_backfill_stamps_backfill_batches_and_an_account_stops_at_a_failure(
        monkeypatch, store):
    graph = _pair()
    fakes.configure(monkeypatch, graph)
    old = graph.accounts[OLD_ACCOUNT_ID]
    old.spend(CAMPAIGN_C, "2026-10-05", "40.00")
    old.hidden.add((CAMPAIGN_C, "2026-10-05"))         # its October will not reconcile
    out = meta.run_backfill(date(2026, 9, 1), date(2026, 12, 31), now=NOW)
    assert out["status"] == "partial" and out["reason"] == "meta_reconciliation_mismatch"
    by_account = {a["account_id"]: a for a in out["accounts"]}
    assert by_account[ACCOUNT_ID]["status"] == "success"
    assert [c["status"] for c in by_account[ACCOUNT_ID]["chunks"]] == ["success"] * 3
    assert by_account[OLD_ACCOUNT_ID]["status"] == "failed"
    assert [c["status"] for c in by_account[OLD_ACCOUNT_ID]["chunks"]] == [
        "success", "failed"]                           # its November was not attempted
    assert OLD_ACCOUNT_ID in out["error"]
    assert {b["sync_type"] for b in store.batches} == {"backfill"}
    assert all(b["run_id"] is None for b in store.batches) and len(store.batches) == 5
    assert [f["status"] for f in store.finished] == ["success"] * 4 + ["failed"]


# ── days: each Meta account's own zone decides ────────────────────────────

def test_57_yesterday_is_the_meta_accounts_yesterday():
    la, amman = "America/Los_Angeles", "Asia/Amman"
    # 06:00 UTC, winter (PST, UTC-8): 22:00 the previous day in Los Angeles.
    assert meta.account_local_yesterday(la, NOW) == date(2026, 11, 16)
    # In Amman (UTC+3, no daylight saving) it is 09:00 on the 18th.
    assert meta.account_local_yesterday(amman, NOW) == date(2026, 11, 17)
    # The Los Angeles day ends at 08:00 UTC in winter...
    assert meta.account_local_yesterday(
        la, datetime(2026, 11, 18, 7, 59, tzinfo=timezone.utc)) == date(2026, 11, 16)
    assert meta.account_local_yesterday(
        la, datetime(2026, 11, 18, 8, 0, tzinfo=timezone.utc)) == date(2026, 11, 17)
    # ...and at 07:00 UTC in summer (PDT, UTC-7).
    assert meta.account_local_yesterday(
        la, datetime(2026, 7, 15, 6, 0, tzinfo=timezone.utc)) == date(2026, 7, 13)
    assert meta.account_local_yesterday(
        la, datetime(2026, 7, 15, 7, 0, tzinfo=timezone.utc)) == date(2026, 7, 14)
    # The Amman day ends at 21:00 UTC.
    assert meta.account_local_yesterday(
        amman, datetime(2026, 11, 18, 20, 59, tzinfo=timezone.utc)) == date(2026, 11, 17)
    assert meta.account_local_yesterday(
        amman, datetime(2026, 11, 18, 21, 0, tzinfo=timezone.utc)) == date(2026, 11, 18)
    assert meta.account_local_yesterday("Europe/London", NOW) == date(2026, 11, 17)
    for unknown in (None, "", "Mars/Olympus", "GMT+3 (Amman)"):
        assert meta.account_local_yesterday(unknown, NOW) is None
        assert meta.recent_window(unknown, NOW) is None
    assert meta.recent_window(la, NOW) == (WINDOW_START, YESTERDAY)
    assert meta.recent_window(la, NOW, days=1) == (YESTERDAY, YESTERDAY)
    assert meta.recent_window(amman, NOW) == (date(2026, 10, 21), date(2026, 11, 17))


def test_58_the_window_follows_metas_zone_not_google_adss(monkeypatch, store):
    """Google Ads is configured for Europe/London. Neither Meta account uses
    it: Los Angeles ends on the 16th, Amman on the 17th."""
    from services import canonical_contract
    monkeypatch.setattr(canonical_contract, "configured_account_time_zone",
                        lambda: "Europe/London")
    graph = _pair()
    graph.spend(CAMPAIGN_A, "2026-11-16", "5.00").spend(CAMPAIGN_A, "2026-11-17", "7.00")
    fakes.configure(monkeypatch, graph)
    out, _ = _run()
    assert [(a["account_time_zone"], a["chunk_end"]) for a in out["accounts"]] == [
        ("America/Los_Angeles", "2026-11-16"), ("Asia/Amman", "2026-11-17")]
    new_rows = next(r for r in store.replaced if r["account_id"] == ACCOUNT_ID)["rows"]
    assert [r["spend_date"] for r in new_rows] == ["2026-11-16"]   # not the 17th


def _chunk(start, end, fetched_at, status="verified"):
    return {"chunk_start": start, "chunk_end": end, "status": status,
            "rows_written": 3, "cost_micros_total": 100_000_000,
            "fetched_at": fetched_at}


def _account(account_id=ACCOUNT_ID, *, chunks, zone="America/Los_Angeles",
             currency="GBP", native=80.0, usd=100.0, name="Logistaas Limited",
             fx_complete=True):
    return {"account_id": account_id, "chunks": chunks, "zone": zone,
            "currency": currency, "native": native, "usd": usd, "name": name,
            "fx_complete": fx_complete}


OLD = dict(account_id=OLD_ACCOUNT_ID, zone="Asia/Amman", currency="USD",
           name="Logistaas")


def _truths(monkeypatch, accounts, *, start, end, configured=None, now=NOW,
            settled=True, states=None):
    """The real truth function over stored accounts described in ``accounts``.
    ``configured`` is the env list (default: every account given)."""
    from db import meta_ads_repository as repo
    fakes.unconfigure(monkeypatch)
    ids = [a["account_id"] for a in accounts] if configured is None else configured
    if ids:
        monkeypatch.setenv("META_ADS_ACCESS_TOKEN", SENTINEL_TOKEN)
        monkeypatch.setenv("META_AD_ACCOUNT_ID", ",".join(ids))
        monkeypatch.setenv("META_API_VERSION", fakes.VERSION)
    by_id = {a["account_id"]: a for a in accounts}

    def stored():
        rows = []
        for a in accounts:
            if a.get("never_read"):
                continue
            verified = [c for c in a["chunks"] if c["status"] == "verified"]
            rows.append({
                "account_id": a["account_id"], "account_name": a["name"],
                "currency_code": a["currency"], "time_zone_name": a["zone"],
                "covered_from": min((c["chunk_start"] for c in verified), default=None),
                "covered_through": max((c["chunk_end"] for c in verified), default=None),
                "has_spend_rows": bool(a["native"])})
        return {"available": True, "accounts": rows}
    monkeypatch.setattr(repo, "fetch_meta_accounts", stored)
    monkeypatch.setattr(repo, "fetch_meta_account_sync_states", lambda: {
        "available": True, "states": states or {}})
    asked = []

    def coverage(s, e, account_id):
        asked.append((account_id, s, e))
        return {"available": True, "chunks": by_id[account_id]["chunks"]}

    def spend(s, e, account_id):
        a = by_id[account_id]
        return {"available": True, "total_spend": a["native"],
                "total_spend_usd": a["usd"] if a["fx_complete"] else None,
                "fx_complete": a["fx_complete"], "currency_code": a["currency"]}
    monkeypatch.setattr(repo, "fetch_meta_spend_coverage", coverage)
    monkeypatch.setattr(repo, "fetch_meta_campaign_spend", spend)

    def no_network(*a, **k):
        raise AssertionError("the truth read must never call Meta")
    monkeypatch.setattr(requests, "get", no_network)
    out = truth_service.build_meta_ads_spend_truth_for_days(
        start, end, settled=settled, now=now)
    out["_asked"] = asked
    return out


def _truth(monkeypatch, *, chunks, start, end, zone="America/Los_Angeles", **kw):
    """One account (the new one, unless ``zone`` says otherwise)."""
    account_kw = {k: kw.pop(k) for k in ("native", "usd", "currency") if k in kw}
    return _truths(monkeypatch, [_account(chunks=chunks, zone=zone, **account_kw)],
                   start=start, end=end, **kw)


def test_59_the_0600_utc_sync_settles_the_los_angeles_window_it_wrote(monkeypatch):
    """The chunk the daily sync writes — ending on Los Angeles' yesterday,
    fetched at 06:00 UTC — holds every one of its days completely."""
    truth = _truth(monkeypatch,
                   chunks=[_chunk("2026-10-20", "2026-11-16", NOW)],
                   start=date(2026, 11, 10), end=YESTERDAY)
    assert truth["state"] == "verified"
    assert (truth["native_currency"], truth["native_spend"], truth["usd_spend"]) == (
        "GBP", 80.0, 100.0)
    assert truth["unsettled_reason"] is None and truth["settled_rule_applied"] is True
    assert truth["time_zones"] == ["America/Los_Angeles"]
    assert truth["mixed_time_zones"] is False


def test_60_the_utc_dates_yesterday_is_not_settled_in_los_angeles(monkeypatch):
    # At 06:00 UTC on the 18th it is 22:00 on the 17th in Los Angeles: the
    # 17th is still in progress there, whatever a chunk's end date says.
    chunks = [_chunk("2026-10-20", "2026-11-17", NOW)]
    truth = _truth(monkeypatch, chunks=chunks,
                   start=date(2026, 11, 10), end=IN_PROGRESS)
    assert truth["state"] == "coverage_incomplete"
    assert truth["usd_spend"] is None and truth["native_spend"] is None
    assert truth["unsettled_reason"] == "window_includes_day_in_progress"
    # Without the settle rule, chunk status alone would have published it.
    loose = _truth(monkeypatch, chunks=chunks, start=date(2026, 11, 10),
                   end=IN_PROGRESS, settled=False)
    assert loose["state"] == "verified" and loose["settled_rule_applied"] is False
    # In Amman the same fetch DOES settle the 17th: the zone decides.
    amman = _truth(monkeypatch, zone="Asia/Amman", chunks=chunks,
                   start=date(2026, 11, 10), end=IN_PROGRESS)
    assert amman["state"] == "verified"
    # Three hours later the Los Angeles day has ended, but nothing has re-read
    # it since: still not settled, for a different reason.
    later = _truth(monkeypatch, chunks=chunks, start=date(2026, 11, 10),
                   end=IN_PROGRESS, now=NOW + timedelta(hours=3))
    assert later["state"] == "coverage_incomplete"
    assert later["unsettled_reason"] == "spend_not_reread_since_day_ended"


@pytest.mark.parametrize("zone", [None, "", "Not/AZone"])
def test_61_an_unknown_zone_settles_nothing(monkeypatch, zone):
    truth = _truth(monkeypatch, zone=zone,
                   chunks=[_chunk("2026-10-20", "2026-11-16", NOW)],
                   start=date(2026, 11, 11), end=date(2026, 11, 12))
    assert truth["state"] == "coverage_incomplete"
    assert truth["unsettled_reason"] == "account_time_zone_unknown"
    assert truth["usd_spend"] is None


@pytest.mark.parametrize("zone, day, fetched, settled", [
    # Los Angeles. Clocks go forward on 2026-03-08 (a 23-hour day, PDT from
    # 10:00 UTC): the 8th ends at 07:00 UTC on the 9th.
    ("America/Los_Angeles", date(2026, 3, 8),
     datetime(2026, 3, 9, 6, 59, tzinfo=timezone.utc), False),
    ("America/Los_Angeles", date(2026, 3, 8),
     datetime(2026, 3, 9, 7, 0, tzinfo=timezone.utc), True),
    # The day before is still PST: it ends at 08:00 UTC on the 8th.
    ("America/Los_Angeles", date(2026, 3, 7),
     datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc), False),
    ("America/Los_Angeles", date(2026, 3, 7),
     datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc), True),
    # Clocks go back on 2026-11-01 (a 25-hour day): it ends at 08:00 UTC on
    # the 2nd, an hour later than the summer days before it.
    ("America/Los_Angeles", date(2026, 11, 1),
     datetime(2026, 11, 2, 7, 30, tzinfo=timezone.utc), False),
    ("America/Los_Angeles", date(2026, 11, 1),
     datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc), True),
    ("America/Los_Angeles", date(2026, 10, 31),
     datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc), True),
    # London — the zone the owner intends to move the new account to. Clocks
    # go forward on 2026-03-29 (BST = UTC+1): 23:30 UTC that day is already
    # 00:30 on the 30th there.
    ("Europe/London", date(2026, 3, 29),
     datetime(2026, 3, 29, 23, 30, tzinfo=timezone.utc), True),
    ("Europe/London", date(2026, 3, 29),
     datetime(2026, 3, 29, 22, 30, tzinfo=timezone.utc), False),
    ("Europe/London", date(2026, 10, 25),
     datetime(2026, 10, 25, 23, 30, tzinfo=timezone.utc), False),
    ("Europe/London", date(2026, 10, 25),
     datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc), True),
    # Amman has no daylight saving: every day ends at 21:00 UTC.
    ("Asia/Amman", date(2026, 3, 29),
     datetime(2026, 3, 29, 20, 59, tzinfo=timezone.utc), False),
    ("Asia/Amman", date(2026, 3, 29),
     datetime(2026, 3, 29, 21, 0, tzinfo=timezone.utc), True),
])
def test_62_daylight_saving_days_end_when_the_local_day_ends(
        monkeypatch, zone, day, fetched, settled):
    truth = _truth(monkeypatch, zone=zone,
                   chunks=[_chunk(day.isoformat(), day.isoformat(), fetched)],
                   start=day, end=day, now=fetched + timedelta(days=3))
    assert (truth["state"] == "verified") is settled, truth
    if not settled:
        assert truth["unsettled_reason"] == "spend_not_reread_since_day_ended"


def test_63_the_settle_rule_ignores_the_google_ads_zone(monkeypatch):
    """Google Ads says Asia/Amman, where the 17th ended at 21:00 UTC. Were
    that zone used for the Los Angeles account, the 17th would read settled."""
    from services import canonical_contract
    monkeypatch.setattr(canonical_contract, "configured_account_time_zone",
                        lambda: "Asia/Amman")
    truth = _truth(monkeypatch,
                   chunks=[_chunk("2026-10-20", "2026-11-17", NOW)],
                   start=date(2026, 11, 10), end=IN_PROGRESS)
    assert truth["state"] == "coverage_incomplete"
    assert truth["time_zones"] == ["America/Los_Angeles"]


# ── several accounts: one total, each account on its own terms ────────────

def _both(new_chunks=None, old_chunks=None, **old_kw):
    return [
        _account(chunks=new_chunks if new_chunks is not None
                 else [_chunk("2026-10-20", "2026-11-16", NOW)]),
        _account(chunks=old_chunks if old_chunks is not None
                 else [_chunk("2026-10-21", "2026-11-17", NOW)],
                 **{**OLD, "native": 130.34, "usd": 130.34, **old_kw}),
    ]


def test_63a_two_accounts_in_two_zones_and_currencies_sum_to_one_usd_total(
        monkeypatch):
    truth = _truths(monkeypatch, _both(), start=date(2026, 11, 10), end=YESTERDAY)
    assert truth["state"] == "verified"
    assert truth["usd_spend"] == 230.34              # 100.00 (GBP priced) + 130.34
    # GBP and USD have no common native total.
    assert truth["native_spend"] is None and truth["native_currency"] is None
    assert truth["mixed_currencies"] is True and truth["mixed_time_zones"] is True
    assert truth["time_zones"] == ["America/Los_Angeles", "Asia/Amman"]
    assert truth["account_ids"] == [ACCOUNT_ID, OLD_ACCOUNT_ID]
    assert [(a["account_id"], a["account_name"], a["currency"], a["time_zone"],
             a["state"], a["native_spend"], a["usd_spend"], a["configured"])
            for a in truth["accounts"]] == [
        (ACCOUNT_ID, "Logistaas Limited", "GBP", "America/Los_Angeles",
         "verified", 80.0, 100.0, True),
        (OLD_ACCOUNT_ID, "Logistaas", "USD", "Asia/Amman",
         "verified", 130.34, 130.34, True)]
    assert truth["accounts"][1]["usd_identity"] is True
    assert truth["accounts"][0]["usd_identity"] is False


def test_63b_the_total_is_published_only_when_every_account_is_settled(monkeypatch):
    """Through the 17th: Amman's 17th has ended and is settled; Los Angeles'
    has not, and that account's ledger stops at the 16th. The old account's
    own figure is proven; the total is not."""
    truth = _truths(monkeypatch, _both(), start=date(2026, 11, 10), end=IN_PROGRESS)
    assert truth["state"] == "coverage_incomplete"
    assert truth["usd_spend"] is None and truth["native_spend"] is None
    new, old = truth["accounts"]
    assert (new["state"], new["usd_spend"]) == ("coverage_incomplete", None)
    assert (old["state"], old["usd_spend"]) == ("verified", 130.34)
    # Each account was judged on the same requested days, in its own zone.
    assert sorted(truth["_asked"]) == [
        (ACCOUNT_ID, date(2026, 11, 10), IN_PROGRESS),
        (OLD_ACCOUNT_ID, date(2026, 11, 10), IN_PROGRESS)]


def test_63c_one_account_without_rates_withholds_the_usd_total(monkeypatch):
    accounts = _both()
    accounts[0]["fx_complete"] = False
    truth = _truths(monkeypatch, accounts, start=date(2026, 11, 10), end=YESTERDAY)
    assert truth["state"] == "fx_incomplete" and truth["usd_spend"] is None
    assert truth["fx_reason"] == "fx_rates_missing"
    assert truth["native_spend"] is None             # two currencies: no native total
    assert truth["accounts"][0]["native_spend"] == 80.0      # its own, in GBP
    assert truth["accounts"][1]["usd_spend"] == 130.34


def test_63d_accounts_in_one_currency_have_a_native_total(monkeypatch):
    truth = _truths(monkeypatch, _both(currency="GBP", usd=162.93),
                    start=date(2026, 11, 10), end=YESTERDAY)
    assert truth["state"] == "verified"
    assert (truth["native_currency"], truth["native_spend"]) == ("GBP", 210.34)
    assert truth["usd_spend"] == 262.93 and truth["mixed_currencies"] is False


def test_63e_a_suspended_account_still_configured_withholds_later_days(monkeypatch):
    """The old account was last verified through 2026-12-20, then suspended.
    While it is still in the configuration, a range that reaches past that
    day is withheld — its spend after the 20th is unknown, not zero."""
    now = datetime(2027, 1, 5, 6, 0, tzinfo=timezone.utc)
    fetched = datetime(2026, 12, 21, 6, 0, tzinfo=timezone.utc)
    accounts = [
        _account(chunks=[_chunk("2026-10-20", "2027-01-03", now)]),
        _account(chunks=[_chunk("2026-10-21", "2026-12-20", fetched),
                         _chunk("2026-12-08", "2027-01-04", now, "failed")],
                 native=500.0, usd=500.0, **OLD)]
    states = {OLD_ACCOUNT_ID: {
        "last_status": "failed", "last_reason": "meta_account_not_readable",
        "not_readable_since": datetime(2026, 12, 22, 6, 0, tzinfo=timezone.utc)}}
    december = (date(2026, 12, 1), date(2026, 12, 31))
    truth = _truths(monkeypatch, accounts, start=december[0], end=december[1],
                    now=now, states=states)
    assert truth["state"] == "coverage_incomplete" and truth["usd_spend"] is None
    old = truth["accounts"][1]
    assert old["state"] == "coverage_incomplete" and old["configured"] is True
    assert old["covered_through"] == "2026-12-20"
    assert old["last_sync_reason"] == "meta_account_not_readable"
    assert old["not_readable_since"].startswith("2026-12-22")
    # Days it WAS verified for still publish.
    early = _truths(monkeypatch, accounts, start=date(2026, 12, 1),
                    end=date(2026, 12, 20), now=now, states=states)
    assert early["state"] == "verified" and early["usd_spend"] == 600.0

    # The owner removes it from the configuration: its history keeps
    # counting, up to its last verified day, and nothing after.
    removed = _truths(monkeypatch, accounts, start=december[0], end=december[1],
                      configured=[ACCOUNT_ID], now=now, states=states)
    assert removed["state"] == "verified" and removed["usd_spend"] == 600.0
    old = removed["accounts"][1]
    assert (old["configured"], old["required_through"], old["state"]) == (
        False, "2026-12-20", "verified")
    assert (OLD_ACCOUNT_ID, december[0], date(2026, 12, 20)) in removed["_asked"]
    # A range wholly after its last day: it is not part of it at all.
    january = _truths(monkeypatch, accounts, start=date(2027, 1, 1),
                      end=date(2027, 1, 3), configured=[ACCOUNT_ID], now=now)
    assert january["account_ids"] == [ACCOUNT_ID] and january["usd_spend"] == 100.0


def test_63f_a_removed_account_with_a_gap_in_its_history_withholds(monkeypatch):
    now = datetime(2027, 1, 5, 6, 0, tzinfo=timezone.utc)
    fetched = datetime(2026, 12, 21, 6, 0, tzinfo=timezone.utc)
    accounts = [
        _account(chunks=[_chunk("2026-10-20", "2027-01-03", now)]),
        _account(chunks=[_chunk("2026-10-21", "2026-11-30", fetched),
                         _chunk("2026-12-10", "2026-12-20", fetched)],
                 native=500.0, usd=500.0, **OLD)]     # 1–9 December never fetched
    truth = _truths(monkeypatch, accounts, start=date(2026, 12, 1),
                    end=date(2026, 12, 31), configured=[ACCOUNT_ID], now=now)
    assert truth["state"] == "coverage_incomplete" and truth["usd_spend"] is None


def test_63g_a_configured_account_never_read_withholds_the_total(monkeypatch):
    accounts = _both()
    accounts[1]["never_read"] = True
    truth = _truths(monkeypatch, accounts, start=date(2026, 11, 10), end=YESTERDAY)
    assert truth["state"] == "coverage_incomplete" and truth["usd_spend"] is None
    old = truth["accounts"][1]
    assert old["never_read"] is True and old["state"] == "coverage_incomplete"
    assert old["currency"] is None and old["native_spend"] is None
    # With nothing on record at all, Meta is simply not connected yet.
    for account in accounts:
        account["never_read"] = True
    nothing = _truths(monkeypatch, accounts, start=date(2026, 11, 10), end=YESTERDAY)
    assert nothing["state"] == "not_connected" and nothing["accounts"] == []


def test_63h_with_nothing_configured_stored_history_still_counts(monkeypatch):
    accounts = _both()
    truth = _truths(monkeypatch, accounts, start=date(2026, 11, 10), end=YESTERDAY,
                    configured=[])
    assert truth["state"] == "verified" and truth["usd_spend"] == 230.34
    assert [a["configured"] for a in truth["accounts"]] == [False, False]
    # A range after every account's last day: nothing is relevant to it.
    later = _truths(monkeypatch, accounts, start=date(2026, 12, 1),
                    end=date(2026, 12, 7), configured=[],
                    now=NOW + timedelta(days=30))
    assert later["state"] == "not_connected" and later["usd_spend"] is None


def test_63i_combining_account_truths_is_pure_and_fails_closed():
    def entry(state, native, usd, currency="USD"):
        return {"state": state, "native_spend": native, "usd_spend": usd,
                "currency": currency, "fx_reason": (
                    "fx_currency_not_supported" if state == "fx_incomplete" else None)}
    combine = truth_service.combine_account_truths
    assert combine([])["state"] == "not_connected"
    both = combine([entry("verified", 10.0, 10.0), entry("verified_zero_spend", 0.0, 0.0)])
    assert (both["state"], both["usd_spend"], both["native_spend"]) == ("verified", 10.0, 10.0)
    zero = combine([entry("verified_zero_spend", 0.0, 0.0, "GBP"),
                    entry("verified_zero_spend", 0.0, 0.0, "USD")])
    assert zero["state"] == "verified_zero_spend" and zero["usd_spend"] == 0.0
    assert zero["native_currency"] is None           # GBP and USD: none in common
    fx = combine([entry("verified", 10.0, 10.0), entry("fx_incomplete", 5.0, None, "JOD")])
    assert (fx["state"], fx["usd_spend"], fx["fx_reason"]) == (
        "fx_incomplete", None, "fx_currency_not_supported")
    for bad in ("coverage_incomplete", "source_unavailable"):
        out = combine([entry("verified", 10.0, 10.0), entry(bad, None, None)])
        assert out["state"] == bad and out["usd_spend"] is None
        assert out["native_spend"] is None
    # An account in a state this code does not know cannot be summed.
    odd = combine([entry("verified", 10.0, 10.0), entry("verified", 5.0, None)])
    assert odd["state"] == "source_unavailable" and odd["usd_spend"] is None


# ── the truth state machine ───────────────────────────────────────────────

BASE = {"connected": True, "available": True, "coverage_complete": True,
        "native_currency": "GBP", "native_total": 80.0, "usd_total": 100.0,
        "fx_status": "verified", "fx_reason": None}


@pytest.mark.parametrize("change, state, native, usd", [
    ({}, "verified", 80.0, 100.0),
    ({"native_total": 0.0, "usd_total": 0.0}, "verified_zero_spend", 0.0, 0.0),
    ({"fx_status": "incomplete", "usd_total": None,
      "fx_reason": "fx_rates_missing"}, "fx_incomplete", 80.0, None),
    ({"fx_status": None}, "fx_incomplete", 80.0, None),
    ({"usd_total": None}, "fx_incomplete", 80.0, None),
    ({"coverage_complete": False}, "coverage_incomplete", None, None),
    ({"coverage_complete": None}, "coverage_incomplete", None, None),
    ({"available": False}, "source_unavailable", None, None),
    ({"native_total": None}, "source_unavailable", None, None),
    ({"connected": False}, "not_connected", None, None),
    ({"connected": None}, "not_connected", None, None),
])
def test_64_each_state_publishes_only_what_it_has_proven(change, state, native, usd):
    truth = truth_service.derive_meta_ads_spend_truth({**BASE, **change})
    assert truth["state"] == state
    assert truth["native_spend"] == native and truth["usd_spend"] == usd
    if state == "not_connected":
        assert truth["native_currency"] is None
    if state == "fx_incomplete" and change.get("fx_reason"):
        assert truth["fx_reason"] == "fx_rates_missing"


def test_65_an_unknown_currency_stays_unknown():
    truth = truth_service.derive_meta_ads_spend_truth(
        {**BASE, "native_currency": None})
    assert truth["native_currency"] is None
    assert truth_service.derive_meta_ads_spend_truth(None)["state"] == "not_connected"
    source = (ROOT / "services/meta_ads_spend_truth_service.py").read_text()
    assert '"GBP"' not in source and "'GBP'" not in source


def test_66_the_truth_has_the_shape_of_the_google_ads_one(monkeypatch):
    from services import revenue_spend_truth_service as google
    shared = {"state", "native_currency", "native_spend", "usd_spend", "fx_status",
              "spend_coverage_status"}
    assert shared <= set(google.derive_google_ads_spend_truth({}))
    assert shared <= set(truth_service.derive_meta_ads_spend_truth({}))
    truth = _truth(monkeypatch,
                   chunks=[_chunk("2026-10-20", "2026-11-16", NOW)],
                   start=date(2026, 11, 11), end=YESTERDAY)
    assert {"start_date", "end_date", "unsettled_reason", "settled_rule_applied",
            "accounts", "account_ids", "time_zones", "mixed_time_zones",
            "mixed_currencies"} <= set(truth)
    for name in (google.STATE_VERIFIED, google.STATE_VERIFIED_ZERO,
                 google.STATE_FX_INCOMPLETE, google.STATE_COVERAGE_INCOMPLETE,
                 google.STATE_SOURCE_UNAVAILABLE):
        assert name in (truth_service.STATE_VERIFIED, truth_service.STATE_VERIFIED_ZERO,
                        truth_service.STATE_FX_INCOMPLETE,
                        truth_service.STATE_COVERAGE_INCOMPLETE,
                        truth_service.STATE_SOURCE_UNAVAILABLE)


# ── nothing here writes to Meta, and nothing names a forbidden metric ─────

NEW_FILES = ("connectors/meta_ads.py", "services/meta_ads_spend_service.py",
             "services/meta_ads_spend_truth_service.py", "db/meta_ads_repository.py",
             "scripts/backfill_meta_ads_spend.py", "scripts/meta_ads_smoke_test.py")


def test_67_only_the_connector_talks_to_meta_and_only_with_get():
    for path in NEW_FILES:
        source = (ROOT / path).read_text()
        for verb in ("requests.post", "requests.put", "requests.delete",
                     "requests.patch", "requests.request", "requests.Session",
                     "urllib.request", "httpx"):
            assert verb not in source, (path, verb)
        if path != "connectors/meta_ads.py":
            assert "import requests" not in source, path
    for path in ("db/meta_ads_repository.py",
                 "services/meta_ads_spend_truth_service.py"):
        source = (ROOT / path).read_text()
        for write in ("INSERT", "UPDATE ", "DELETE", "connectors"):
            assert write not in source, (path, write)


def test_68_no_period_ratio_word_appears_in_the_new_code():
    for path in NEW_FILES + ("tests/meta_ads_fakes.py",):
        words = set(re.findall(r"[a-z]+", (ROOT / path).read_text().lower()))
        assert not (words & {"roas", "cac"}), path


# ── the System page line, rendered by the real code ───────────────────────

def _line(source: dict) -> str:
    src = (ROOT / "static/app.js").read_text()
    start = src.index("const SYSTEM_CHECK_LABELS = {")
    end = src.index("let _systemReqSeq = 0;")
    esc_start = src.index("function escapeHtml(str) {")
    esc_end = src.index("\n}\n", esc_start) + 3
    script = (src[esc_start:esc_end] + src[start:end]
              + "\nprocess.stdout.write(systemSourceLine("
              + json.dumps(source) + "));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


META = {"key": "meta_ads_spend", "label": "Meta Ads spend"}


def test_69_not_connected_is_neutral_neither_healthy_nor_a_warning():
    line = _line({**META, "status": "not_connected"})
    assert "Not connected" in line and "system-source--neutral" in line
    assert "system-source--ok" not in line and "system-source--warn" not in line
    assert "No successful update on record" not in line


@pytest.mark.parametrize("source, words", [
    ({"status": "misconfigured", "reason": "access_token_missing"},
     "Set up incorrectly"),
    ({"status": "token_rejected"}, "Meta rejected the access token"),
    ({"status": "not_proven"}, "no successful update yet"),
    ({"status": "not_proven", "reason": "meta_api_version_deprecated",
      "reason_detail": "Meta API version v22.0 is deprecated; Meta says upgrade "
                       "to v27.0 (HTTP 400, code 2635)"},
     "Meta no longer accepts the API version in use — Meta API version v22.0 is "
     "deprecated; Meta says upgrade to v27.0"),
    ({"status": "late", "reason": "meta_api_version_deprecated"},
     "Meta no longer accepts the API version in use"),
    ({"status": "late", "missing_ranges": [{"start": "2026-11-16",
                                            "end": "2026-11-17"}]},
     "Late — no spend recorded for 2026-11-16 to 2026-11-17"),
    ({"status": "late", "missing_ranges": []}, "a recent spend update failed"),
    ({"status": "unavailable"}, "Could not be read"),
    ({"status": "current"}, "Could not be read"),          # no day proven
    ({"status": "something_new"}, "Could not be read"),
    ({"status": None}, "Could not be read"),
    ({"status": "NOT_CONNECTED"}, "Could not be read"),
])
def test_70_every_other_meta_status_is_a_warning(source, words):
    line = _line({**META, **source})
    assert words in line and "system-source--warn" in line
    assert "system-source--ok" not in line and "system-source--neutral" not in line


def test_71_current_with_a_proven_day_is_the_only_healthy_meta_line():
    line = _line({**META, "status": "current", "complete_through": "2026-11-16",
                  "last_success_at": "2026-11-18T06:00:00+00:00", "age_hours": 3})
    assert "Complete through 2026-11-16" in line and "system-source--ok" in line


def test_72_not_connected_is_neutral_for_the_meta_key_only():
    for key in ("google_ads_spend", "hubspot_deals", "hubspot_contacts", "new"):
        line = _line({"key": key, "label": "X", "status": "not_connected"})
        assert "system-source--warn" in line and "neutral" not in line, key


NEW_LINE = {"status": "current", "summary": "Logistaas Limited (4730305973961806): "
            "complete through 2026-11-16 (America/Los_Angeles)"}
OLD_GONE = {"status": "not_readable", "summary": "Logistaas (944370805110952): account "
            "not readable since 2026-12-22 (HTTP 403, code 200); history through "
            "2026-12-20 is kept"}


def test_72a_each_account_is_listed_in_its_own_words():
    line = _line({**META, "status": "not_readable", "accounts": [NEW_LINE, OLD_GONE]})
    assert "An ad account is not readable — its history is kept" in line
    assert "system-source--warn" in line and "system-source--ok" not in line
    assert ("account not readable since 2026-12-22 (HTTP 403, code 200); history "
            "through 2026-12-20 is kept") in line
    assert "complete through 2026-11-16 (America/Los_Angeles)" in line
    assert line.count("system-source__detail") == 2
    # Two current accounts: healthy, and both dates are shown (they differ).
    both = _line({**META, "status": "current", "complete_through": "2026-11-16",
                  "accounts": [NEW_LINE, {"status": "current", "summary":
                               "Logistaas (944370805110952): complete through "
                               "2026-11-17 (Asia/Amman)"}]})
    assert "system-source--ok" in both and "Complete through 2026-11-16" in both
    assert "complete through 2026-11-17 (Asia/Amman)" in both
    # One current account needs no second line.
    one = _line({**META, "status": "current", "complete_through": "2026-11-16",
                 "accounts": [NEW_LINE]})
    assert "system-source__detail" not in one
    # Not connected, with history kept from a removed account: still neutral.
    kept = _line({**META, "status": "not_connected", "accounts": [{
        "status": "history_kept", "summary": "Logistaas (944370805110952): no longer "
        "connected; history through 2026-12-20 is kept"}]})
    assert "system-source--neutral" in kept and "Not connected" in kept
    assert "history through 2026-12-20 is kept" in kept
    # An account name is text, not markup.
    unsafe = _line({**META, "status": "late", "accounts": [{
        "status": "late", "summary": "<img src=x onerror=alert(1)> (1): late"}]})
    assert "<img" not in unsafe and "&lt;img" in unsafe


def test_73_the_neutral_style_and_the_dataset_labels_exist():
    css = (ROOT / "static/styles.css").read_text()
    assert ".system-source--neutral" in css
    js = (ROOT / "static/app.js").read_text()
    assert '"meta_ads_api/meta_campaign_spend": "Meta Ads spend data"' in js
    assert 'meta_ads_api: "Meta Ads API"' in js


def test_74_the_credentials_are_declared_for_deployment_without_values():
    env = (ROOT / ".env.example").read_text()
    render = (ROOT / "render.yaml").read_text()
    for name in ("META_ADS_ACCESS_TOKEN", "META_AD_ACCOUNT_ID", "META_API_VERSION",
                 "META_APP_SECRET", "META_ADS_ACCESS_TOKEN_2",
                 "META_AD_ACCOUNT_ID_2", "META_APP_SECRET_2"):
        assert re.search(rf"^{name}=$", env, re.M), name
        assert re.search(rf"- key: {name}\n\s+sync: false", render), name
