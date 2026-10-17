"""
tests/test_pr_ads_161d2_deal_company_associations.py

PR-ADS-161D2 — canonical deal→company association ingestion and customer
identity certification.

§1  connector: complete, read-only, fail-closed reads
§2  storage: atomic success, failure preservation, monotonic ordering (PG)
§3  customer identity through the canonical truth service (PG)
§4  sync service, backfill command and scheduler
§5  audits: coverage certification and the closed-won audit's derivation
§6  registry, CI wiring

No production totals are hardcoded: every number comes from the fixtures here.
"""

from __future__ import annotations

import ast
import io
import json
import sys
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import tests.conftest as conftest  # noqa: E402,F401  (import-order guard)
import analysis.closed_won_truth as cwt  # noqa: E402
import connectors.hubspot_pull as hubspot  # noqa: E402
import services.canonical_customer_revenue_service as svc  # noqa: E402
from tests.canonical_ledger_fixtures import ledger_row  # noqa: E402
from tests.test_pr_ads_153e_a_pg_integration import (  # noqa: E402,F401
    _have_postgres, pg,
)

_needs_pg = pytest.mark.skipif(
    not _have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
Q4 = "2026-10-02T09:00:00+00:00"
BUSINESS = svc.WINDOW_BUSINESS


# ═════════════════════════════════════════════════════════════════════════════
# §1 — the connector
# ═════════════════════════════════════════════════════════════════════════════

class _Resp:
    def __init__(self, status=200, payload=None, headers=None, raw=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self._raw = raw

    def json(self):
        if self._raw is not None:
            raise ValueError("not json")
        return self._payload


def _page(rows, after=None):
    out = {"results": rows}
    if after:
        out["paging"] = {"next": {"after": after}}
    return out


def _row(company_id, *types):
    return {"toObjectId": company_id,
            "associationTypes": [{"category": c, "typeId": t, "label": lbl}
                                 for c, t, lbl in types]}


_PRIMARY = ("HUBSPOT_DEFINED", 5, "Primary")
_UNLABELED = ("HUBSPOT_DEFINED", 341, None)
_CUSTOM = ("USER_DEFINED", 17, "Billing entity")


@pytest.fixture()
def http(monkeypatch):
    """Queue HTTP responses for the connector; record every call."""
    calls, queue, sleeps = [], [], []
    monkeypatch.setattr(hubspot, "HUBSPOT_API_KEY", "test-token")

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append({"url": url, "params": dict(params or {})})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(hubspot.requests, "get", fake_get)
    return {"calls": calls, "queue": queue, "sleeps": sleeps,
            "sleep": sleeps.append}


def test_01_one_deal_one_company_is_one_path_with_a_stable_key(http):
    http["queue"].append(_Resp(payload=_page([_row(901, _PRIMARY)])))
    out = hubspot.fetch_deal_company_associations("D1", sleep=http["sleep"])
    assert out["complete"] is True and out["deal_id"] == "D1"
    assert out["companies"] == [{
        "company_id": "901", "association_category": "HUBSPOT_DEFINED",
        "association_type_id": "5", "association_label": "Primary",
        "association_path_key": "HUBSPOT_DEFINED:5"}]
    assert "/crm/v4/objects/deals/D1/associations/companies" in \
        http["calls"][0]["url"]


def test_02_every_association_type_is_kept_as_its_own_path(http):
    """Two types to one company, plus a custom label: three paths, never the
    first type only."""
    http["queue"].append(_Resp(payload=_page(
        [_row(901, _PRIMARY, _UNLABELED), _row(902, _CUSTOM)])))
    out = hubspot.fetch_deal_company_associations("D1", sleep=http["sleep"])
    keys = [(c["company_id"], c["association_path_key"])
            for c in out["companies"]]
    assert keys == [("901", "HUBSPOT_DEFINED:5"), ("901", "HUBSPOT_DEFINED:341"),
                    ("902", "USER_DEFINED:17")]


def test_03_every_page_is_read(http):
    http["queue"].extend([
        _Resp(payload=_page([_row(901, _PRIMARY)], after="c1")),
        _Resp(payload=_page([_row(902, _PRIMARY)], after="c2")),
        _Resp(payload=_page([_row(903, _PRIMARY)])),
    ])
    out = hubspot.fetch_deal_company_associations("D1", sleep=http["sleep"])
    assert [c["company_id"] for c in out["companies"]] == ["901", "902", "903"]
    assert out["pages"] == 3
    assert [c["params"].get("after") for c in http["calls"]] == [None, "c1", "c2"]


def test_04_rate_limits_and_server_errors_are_retried_with_backoff(http):
    http["queue"].extend([
        _Resp(status=429, headers={"Retry-After": "7"}),
        _Resp(status=503),
        _Resp(payload=_page([_row(901, _PRIMARY)])),
    ])
    out = hubspot.fetch_deal_company_associations("D1", sleep=http["sleep"])
    assert [c["company_id"] for c in out["companies"]] == ["901"]
    assert http["sleeps"][0] == 7.0          # the server's Retry-After, honoured
    assert len(http["sleeps"]) == 2


def test_05_a_failed_lookup_raises_and_is_never_an_empty_result(http):
    http["queue"].extend([_Resp(status=503)] * hubspot.MAX_RETRIES)
    with pytest.raises(hubspot.DealCompanyAssociationLookupError) as exc:
        hubspot.fetch_deal_company_associations("D1", sleep=http["sleep"])
    assert exc.value.reason.startswith("http_503")
    # A 4xx will not change on retry: one call, then a failure — never [].
    http["queue"].append(_Resp(status=404))
    http["calls"].clear()
    with pytest.raises(hubspot.DealCompanyAssociationLookupError) as exc:
        hubspot.fetch_deal_company_associations("D2", sleep=http["sleep"])
    assert exc.value.reason == "http_404" and len(http["calls"]) == 1
    # A timeout that never clears is a failure too.
    import requests
    http["queue"].extend([requests.exceptions.Timeout()] * hubspot.MAX_RETRIES)
    with pytest.raises(hubspot.DealCompanyAssociationLookupError):
        hubspot.fetch_deal_company_associations("D3", sleep=http["sleep"])


@pytest.mark.parametrize("responses,reason", [
    ([_Resp(payload={"results": [{"associationTypes": [
        {"category": "HUBSPOT_DEFINED", "typeId": 5}]}]})], "malformed_payload"),
    ([_Resp(payload={"results": [{"toObjectId": 1, "associationTypes": []}]})],
     "malformed_payload"),
    ([_Resp(payload={"results": [{"toObjectId": 1, "associationTypes": [
        {"category": None, "typeId": 5}]}]})], "malformed_payload"),
    ([_Resp(payload={"results": "nope"})], "malformed_payload"),
    ([_Resp(payload=_page([_row(1, _PRIMARY)], after="same")),
      _Resp(payload=_page([_row(2, _PRIMARY)], after="same"))],
     "pagination_cursor_repeated"),
    ([_Resp(raw="<html>")] * 3, "malformed_payload_after_3_attempts"),
])
def test_06_an_incomplete_or_malformed_response_fails_closed(
        http, responses, reason):
    http["queue"].extend(responses)
    with pytest.raises(hubspot.DealCompanyAssociationLookupError) as exc:
        hubspot.fetch_deal_company_associations("D1", sleep=http["sleep"])
    assert exc.value.reason == reason


def test_07_the_connector_only_reads():
    src = (_ROOT / "connectors/hubspot_pull.py").read_text(encoding="utf-8")
    for fn in ("fetch_deal_company_associations", "_company_association_page"):
        body = src.split(f"def {fn}(")[1].split("\ndef ")[0].lower()
        for banned in ("requests.post", "requests.put", "requests.patch",
                       "requests.delete", "_api.create", "_api.update",
                       "_api.archive"):
            assert banned not in body, (fn, banned)
    assert "requests.get(" in src.split("def _company_association_page(")[1]


def test_08_a_successful_empty_read_is_zero_companies(http):
    http["queue"].append(_Resp(payload={"results": []}))
    out = hubspot.fetch_deal_company_associations("D1", sleep=http["sleep"])
    assert out == {"deal_id": "D1", "companies": [], "complete": True,
                   "pages": 1}


# ═════════════════════════════════════════════════════════════════════════════
# §2 — storage (PostgreSQL)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def ledger(pg, monkeypatch):  # noqa: F811
    from db import deal_ledger_repository as repo

    assert repo.record_sync_state(status="success", sync_mode="bootstrap",
                                  proved_complete=True)["available"]
    assert repo.record_sync_state(status="success",
                                  sync_mode="incremental")["available"]
    return repo


def _deal(ledger_repo, deal_id, **kw):
    kw.setdefault("deal_close_date", Q4)
    res = ledger_repo.upsert_deal(ledger_row(deal_id, **kw), associations=[])
    assert res.get("available") is not False, res


def _path(company, category="HUBSPOT_DEFINED", type_id="5", label=None):
    return {"company_id": str(company), "association_category": category,
            "association_type_id": str(type_id), "association_label": label,
            "association_path_key": f"{category}:{type_id}"}


def _t(minutes):
    return datetime(2026, 10, 7, 11, 0, tzinfo=timezone.utc) + \
        timedelta(minutes=minutes)


def _q(sql, params=()):
    from db.connection import get_conn
    with get_conn() as c, c.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _obs(deal_id):
    rows = _q("SELECT * FROM hubspot_deal_company_observation WHERE deal_id=%s",
              (deal_id,))
    return rows[0] if rows else None


def _bridge(deal_id):
    return sorted((r["company_id"], r["association_path_key"]) for r in _q(
        "SELECT company_id, association_path_key FROM "
        "hubspot_deal_company_association WHERE deal_id=%s", (deal_id,)))


@_needs_pg
def test_10_a_success_writes_paths_and_counts_atomically(ledger):
    from db import deal_company_association_repository as repo
    _deal(ledger, "1")
    res = repo.record_observation_success(
        "1", [_path(901), _path(901, type_id="341")],
        observation_started_at=_t(0))
    assert res == {"available": True, "applied": True, "skipped_stale": 0,
                   "paths_written": 2, "error": None}
    o = _obs("1")
    assert (o["last_attempt_status"], o["success_company_count"],
            o["success_path_count"], o["success_zero_companies"]) == \
        ("success", 1, 2, False)
    assert _bridge("1") == [("901", "HUBSPOT_DEFINED:341"),
                            ("901", "HUBSPOT_DEFINED:5")]


@_needs_pg
def test_11_a_successful_zero_is_recorded_not_inferred(ledger):
    from db import deal_company_association_repository as repo
    _deal(ledger, "1")
    assert repo.record_observation_success(
        "1", [], observation_started_at=_t(0))["applied"]
    o = _obs("1")
    assert o["success_zero_companies"] is True
    assert o["success_company_count"] == 0 and _bridge("1") == []


@_needs_pg
def test_12_a_failure_is_not_zero_and_preserves_the_last_known_set(ledger):
    from db import deal_company_association_repository as repo
    _deal(ledger, "1")
    _deal(ledger, "2")
    # Deal 2 has never succeeded: a failure must not become "no company".
    assert repo.record_observation_failure(
        "2", observation_started_at=_t(0), reason="timeout")["applied"]
    o2 = _obs("2")
    assert o2["last_attempt_status"] == "failed"
    assert o2["success_zero_companies"] is None
    assert o2["last_success_started_at"] is None
    # Deal 1 succeeds, then a later refresh fails: the set stands.
    repo.record_observation_success("1", [_path(901)],
                                    observation_started_at=_t(1))
    assert repo.record_observation_failure(
        "1", observation_started_at=_t(2), reason="http_503")["applied"]
    o1 = _obs("1")
    assert o1["last_attempt_status"] == "failed"
    assert o1["last_failure_reason"] == "http_503"
    assert o1["success_company_count"] == 1
    assert _bridge("1") == [("901", "HUBSPOT_DEFINED:5")]


@_needs_pg
def test_13_a_successful_refresh_removes_paths_hubspot_no_longer_reports(ledger):
    from db import deal_company_association_repository as repo
    _deal(ledger, "1")
    repo.record_observation_success(
        "1", [_path(901), _path(902)], observation_started_at=_t(0))
    repo.record_observation_success("1", [_path(902)],
                                    observation_started_at=_t(1))
    assert _bridge("1") == [("902", "HUBSPOT_DEFINED:5")]
    repo.record_observation_success("1", [], observation_started_at=_t(2))
    assert _bridge("1") == [] and _obs("1")["success_zero_companies"] is True


@_needs_pg
def test_14_a_rerun_is_idempotent(ledger):
    from db import deal_company_association_repository as repo
    _deal(ledger, "1")
    for _ in range(3):
        assert repo.record_observation_success(
            "1", [_path(901), _path(901), _path(902, "USER_DEFINED", 17)],
            observation_started_at=_t(0))["available"]
    assert _bridge("1") == [("901", "HUBSPOT_DEFINED:5"),
                            ("902", "USER_DEFINED:17")]
    assert _q("SELECT count(*) AS n FROM hubspot_deal_company_observation"
              )[0]["n"] == 1


@_needs_pg
def test_15_an_older_observation_cannot_overwrite_a_newer_one(ledger):
    from db import deal_company_association_repository as repo
    _deal(ledger, "1")
    repo.record_observation_success("1", [_path(902)],
                                    observation_started_at=_t(10))
    # A slow pass that STARTED earlier commits later: rejected as stale.
    old = repo.record_observation_success("1", [_path(901)],
                                          observation_started_at=_t(5))
    assert (old["applied"], old["skipped_stale"]) == (False, 1)
    assert _bridge("1") == [("902", "HUBSPOT_DEFINED:5")]
    # An older failure does not displace the newer successful attempt.
    assert not repo.record_observation_failure(
        "1", observation_started_at=_t(6), reason="timeout")["applied"]
    assert _obs("1")["last_attempt_status"] == "success"
    # A newer failure, then an older-than-it but newer-than-success success:
    # the success columns move, the latest attempt stays the failure.
    repo.record_observation_failure("1", observation_started_at=_t(20),
                                    reason="timeout")
    repo.record_observation_success("1", [_path(903)],
                                    observation_started_at=_t(15))
    o = _obs("1")
    assert o["last_attempt_status"] == "failed"
    assert _bridge("1") == [("903", "HUBSPOT_DEFINED:5")]


@_needs_pg
def test_16_a_persistence_failure_rolls_back_the_whole_observation(
        ledger, monkeypatch):
    from db import deal_company_association_repository as repo
    _deal(ledger, "1")
    repo.record_observation_success("1", [_path(901)],
                                    observation_started_at=_t(0))
    real = repo.get_conn
    inserts = {"n": 0}

    class _Cur:
        def __init__(self, cur):
            self._cur = cur

        def execute(self, sql, *a, **k):
            if "INSERT INTO hubspot_deal_company_association" in str(sql):
                inserts["n"] += 1
                if inserts["n"] == 2:
                    raise RuntimeError("disk full")
            return self._cur.execute(sql, *a, **k)

        def __getattr__(self, name):
            return getattr(self._cur, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._cur.__exit__(*exc)

    class _Conn:
        def __init__(self, conn):
            self._conn = conn

        def cursor(self, *a, **k):
            return _Cur(self._conn.cursor(*a, **k))

        def __getattr__(self, name):
            return getattr(self._conn, name)

    @contextmanager
    def failing():
        with real() as conn:
            yield _Conn(conn)

    monkeypatch.setattr(repo, "get_conn", failing)
    res = repo.record_observation_success(
        "1", [_path(905), _path(906)], observation_started_at=_t(5))
    monkeypatch.setattr(repo, "get_conn", real)
    assert res["available"] is False and res["applied"] is False
    # Nothing of the failed observation landed: not the bridge, not the counts.
    assert _bridge("1") == [("901", "HUBSPOT_DEFINED:5")]
    o = _obs("1")
    assert o["success_company_count"] == 1
    assert o["last_success_started_at"] == _t(0)


@_needs_pg
def test_17_a_malformed_path_writes_nothing(ledger):
    from db import deal_company_association_repository as repo
    _deal(ledger, "1")
    res = repo.record_observation_success(
        "1", [_path(901), {"company_id": "", "association_path_key": "x"}],
        observation_started_at=_t(0))
    assert res["available"] is False
    assert _obs("1") is None and _bridge("1") == []


# ═════════════════════════════════════════════════════════════════════════════
# §3 — customer identity through the canonical truth service (PG)
# ═════════════════════════════════════════════════════════════════════════════

def _window(window="current_quarter"):
    return svc.get_closed_won_truth([(BUSINESS, window)], now=NOW,
                                    resolver=None,
                                    resolver_detail="test")["windows"][0]


def _observe(deal_id, companies, minutes=0):
    from db import deal_company_association_repository as repo
    paths = []
    for c in companies:
        paths.append(_path(c) if not isinstance(c, dict) else c)
    assert repo.record_observation_success(
        deal_id, paths, observation_started_at=_t(minutes))["applied"]


def _won_ids():
    return [r["deal_id"] for r in _q(
        "SELECT deal_id FROM hubspot_deal_ledger "
        "WHERE hs_is_closed_won IS TRUE ORDER BY deal_id")]


def _record_complete_scan():
    """A successful, complete full-population run, as the scheduled scan would
    record it — naming the won deals it certified. A published total needs one
    (the scan-current gate)."""
    from db import deal_company_association_repository as repo
    won = _won_ids()
    assert repo.record_sync_run(
        mode="incremental", status="success", started_at=_t(-60),
        population=len(won), attempted=len(won), succeeded=len(won),
        complete=True, full_population=True,
        scanned_deal_ids=won)["available"]


@_needs_pg
def test_20_one_company_per_deal_publishes_exact_distinct_customers(ledger):
    for d in ("1", "2", "3"):
        _deal(ledger, d)
    _observe("1", [901])
    _observe("2", [901])          # same company, second won deal
    _observe("3", [902, _path(902, type_id="341")])   # two paths, one company
    _record_complete_scan()
    w = _window()
    assert w["outcomes"]["closed_won_deals"] == 3
    assert w["publication"]["customers"] == {"status": cwt.PUBLISHED,
                                             "reason": None}
    assert w["outcomes"]["customers"] == 2
    assert w["coverage"]["customer_identity"] == {
        cwt.CUSTOMER_RESOLVED_SINGLE: 2, cwt.CUSTOMER_RESOLVED_MULTI_PATH: 1}


@_needs_pg
def test_21_unresolved_or_ambiguous_identity_publishes_only_a_lower_bound(
        ledger):
    for d in ("1", "2", "3"):
        _deal(ledger, d)
    _observe("1", [901])
    _observe("2", [901, 902])     # two distinct companies — ambiguous
    _observe("3", [])             # successful lookup, no company
    _record_complete_scan()
    w = _window()
    assert w["outcomes"]["customers"] is None
    assert w["publication"]["customers"] == {
        "status": cwt.WITHHELD, "reason": cwt.R_CUSTOMER_IDENTITY}
    assert w["outcomes"]["confirmed_customer_lower_bound"] == 1
    assert w["outcomes"]["unresolved_customer_identity"] == 2
    assert w["coverage"]["customer_identity"] == {
        cwt.CUSTOMER_AMBIGUOUS: 1, cwt.CUSTOMER_RESOLVED_SINGLE: 1,
        cwt.CUSTOMER_NO_COMPANY: 1}
    # Never the won-deal count standing in for customers.
    assert w["outcomes"]["closed_won_deals"] == 3


@_needs_pg
def test_22_a_never_observed_or_failed_deal_is_unavailable_not_no_company(
        ledger):
    from db import deal_company_association_repository as repo
    for d in ("1", "2", "3"):
        _deal(ledger, d)
    _observe("1", [901])
    repo.record_observation_failure("2", observation_started_at=_t(0),
                                    reason="timeout")
    # Deal 3: never looked at.
    _record_complete_scan()
    w = _window()
    assert w["publication"]["customers"] == {
        "status": cwt.UNAVAILABLE, "reason": cwt.R_CUSTOMER_IDENTITY_UNAVAILABLE}
    assert w["coverage"]["customer_identity"] == {
        cwt.CUSTOMER_ASSOCIATION_UNAVAILABLE: 2, cwt.CUSTOMER_RESOLVED_SINGLE: 1}
    assert w["outcomes"]["confirmed_customer_lower_bound"] == 1


@_needs_pg
def test_23_stale_evidence_is_used_disclosed_and_never_a_published_total(
        ledger):
    from db import deal_company_association_repository as repo
    _deal(ledger, "1")
    _deal(ledger, "2", hubspot_lastmodified_at="2026-10-07T11:30:00+00:00")
    _observe("1", [901], minutes=0)
    _observe("2", [902], minutes=10)    # observed 11:10, deal changed 11:30
    _record_complete_scan()
    w = _window()
    assert w["publication"]["customers"] == {
        "status": cwt.WITHHELD, "reason": cwt.R_CUSTOMER_IDENTITY_STALE}
    assert w["outcomes"]["stale_customer_identity"] == 1
    assert w["outcomes"]["confirmed_customer_lower_bound"] == 1
    # A failed refresh after a success is stale too — the set stands, unused
    # for a total.
    _deal(ledger, "3")
    _observe("3", [903], minutes=0)
    repo.record_observation_failure("3", observation_started_at=_t(30),
                                    reason="http_503")
    assert _window()["outcomes"]["stale_customer_identity"] == 2


@_needs_pg
def test_24_the_evidence_is_read_inside_the_one_read_only_snapshot(
        ledger, monkeypatch):
    _deal(ledger, "1")
    _observe("1", [901])
    real = ledger.get_conn
    executed = []

    class _Cur:
        def __init__(self, cur):
            self._cur = cur

        def execute(self, sql, *a, **k):
            executed.append(" ".join(str(sql).split()))
            return self._cur.execute(sql, *a, **k)

        def __getattr__(self, name):
            return getattr(self._cur, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._cur.__exit__(*exc)

    class _Conn:
        def __init__(self, conn):
            self._conn = conn

        def cursor(self, *a, **k):
            return _Cur(self._conn.cursor(*a, **k))

        def __getattr__(self, name):
            return getattr(self._conn, name)

    @contextmanager
    def recording():
        with real() as conn:
            yield _Conn(conn)

    monkeypatch.setattr(ledger, "get_conn", recording)
    universe = ledger.fetch_closed_won_universe("326093516")
    assert universe["company_associations"]["available"] is True
    assert executed[0] == ledger.CLOSED_WON_UNIVERSE_TRANSACTION
    company_reads = [i for i, sql in enumerate(executed)
                     if "hubspot_deal_company_association" in sql]
    assert company_reads, "the company evidence was not read"
    # One connection, one transaction: no COMMIT between the snapshot's start
    # and its company reads.
    assert not any(sql.upper().startswith("COMMIT") for sql in executed)
    paths = universe["company_associations"]["paths"]
    assert [(p["deal_id"], p["company_id"]) for p in paths] == [("1", "901")]


@_needs_pg
def test_25_unreadable_company_evidence_withholds_customers_only(
        ledger, monkeypatch):
    from db import deal_company_association_repository as company_repo
    _deal(ledger, "1")

    def boom(cur):
        cur.execute("SELECT * FROM no_such_table_161d2")

    monkeypatch.setattr(company_repo, "read_won_deal_company_evidence", boom)
    w = _window()
    assert w["publication"]["closed_won_deals"]["status"] == cwt.PUBLISHED
    assert w["outcomes"]["closed_won_deals"] == 1
    assert w["publication"]["revenue_usd"]["status"] == cwt.PUBLISHED
    assert w["publication"]["customers"] == {
        "status": cwt.UNAVAILABLE,
        "reason": cwt.R_COMPANY_ASSOCIATIONS_UNREADABLE}


def test_26_no_identity_is_ever_inferred_from_names_contacts_or_campaigns():
    """The company map is built from stored company ids and nothing else."""
    src = (_ROOT / "services/canonical_customer_revenue_service.py").read_text(
        encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef)
              and n.name == "company_identity_inputs")
    body = ast.get_source_segment(src, fn)
    for forbidden in ("deal_name", "company_name", "email", "campaign",
                      "contact_id", "gclid", "domain", "label"):
        assert forbidden not in body, forbidden
    # Behaviour: a deal with contacts, a GCLID and a campaign but no observed
    # company is UNAVAILABLE, never resolved from that evidence.
    inputs = svc.company_identity_inputs(
        {"available": True, "paths": [], "observations": []})
    out = cwt.evaluate_window(
        won_rows=[ledger_row("1", deal_close_date=Q4, gclid="g",
                             campaign_name_raw="Brand", primary_contact_id="c")],
        definition_rows=[], contacts_by_deal={}, start=None,
        end=NOW + timedelta(days=1), is_all_time=True, now=NOW,
        coverage_findings=[], company_ids_by_deal=inputs["company_ids_by_deal"],
        stale_company_evidence=inputs["stale"])
    assert out["coverage"]["customer_identity"] == {
        cwt.CUSTOMER_ASSOCIATION_UNAVAILABLE: 1}
    assert out["outcomes"]["customers"] is None


@_needs_pg
def test_27_a_stopped_pipeline_withholds_customers_whatever_each_deal_says(
        ledger, monkeypatch):
    """The truth auditor's probe 1: a complete scan, then runs that fail before
    reading any deal. No per-deal record moves, so per-deal staleness cannot
    see it — the dataset's own run record must."""
    from db import deal_company_association_repository as repo
    from services.deal_company_association_sync_service import (
        sync_deal_company_associations,
    )
    _deal(ledger, "1")
    _deal(ledger, "2")
    _fake_reads(monkeypatch, {"1": [901], "2": [902]})
    assert sync_deal_company_associations(mode="incremental")["complete"]
    assert _window()["outcomes"]["customers"] == 2
    monkeypatch.setattr(repo, "fetch_won_deal_population",
                        lambda: {"available": False, "deals": [],
                                 "error": "boom"})
    assert sync_deal_company_associations(mode="incremental")["status"] == \
        "failed"
    w = _window()
    assert w["outcomes"]["customers"] is None
    assert w["publication"]["customers"] == {
        "status": cwt.WITHHELD,
        "reason": cwt.R_CUSTOMER_IDENTITY_SCAN_NOT_CURRENT}
    assert w["outcomes"]["confirmed_customer_lower_bound"] is None


@_needs_pg
def test_28_an_old_complete_scan_is_not_current(ledger, monkeypatch):
    from services.deal_company_association_sync_service import (
        sync_deal_company_associations,
    )
    _deal(ledger, "1")
    _fake_reads(monkeypatch, {"1": [901]})
    assert sync_deal_company_associations(mode="incremental")["complete"]
    # Aged from the scan's START (PR-ADS-161D2-F2).
    started = svc._parse(_q(
        "SELECT last_complete_scan_started_at AS f FROM "
        "hubspot_deal_company_sync_state")[0]["f"])
    max_age = svc.association_scan_max_age_hours()
    fresh = svc.get_closed_won_truth(
        [(BUSINESS, "all_time")], now=started + timedelta(hours=max_age - 1),
        resolver=None, resolver_detail="t")["windows"][0]
    old = svc.get_closed_won_truth(
        [(BUSINESS, "all_time")], now=started + timedelta(hours=max_age + 1),
        resolver=None, resolver_detail="t")["windows"][0]
    assert fresh["outcomes"]["customers"] == 1
    assert old["outcomes"]["customers"] is None
    assert old["publication"]["customers"]["reason"] == \
        cwt.R_CUSTOMER_IDENTITY_SCAN_NOT_CURRENT


@_needs_pg
def test_29_nothing_observed_is_no_lower_bound_not_zero(ledger):
    _deal(ledger, "1")
    _record_complete_scan()
    w = _window()
    assert w["outcomes"]["closed_won_deals"] == 1
    assert w["outcomes"]["confirmed_customer_lower_bound"] is None


@_needs_pg
def test_29_b_observations_are_ordered_by_the_database_clock(
        ledger, monkeypatch):
    """A host clock running a year ahead must not stamp observations, or every
    genuinely newer read afterwards would be rejected as stale."""
    from services.deal_company_association_sync_service import (
        sync_deal_company_associations,
    )
    _deal(ledger, "1")
    _fake_reads(monkeypatch, {"1": [901]})
    ahead = datetime.now(tz=timezone.utc) + timedelta(days=365)
    sync_deal_company_associations(mode="incremental", now_fn=lambda: ahead)
    started = _obs("1")["last_success_started_at"]
    assert started < ahead - timedelta(days=300)


@_needs_pg
def test_29_c_an_older_run_committing_late_cannot_overwrite_a_newer_verdict(
        ledger):
    """Copilot #190: run state ordered by RUN START, not commit order. A newer
    scan failed; an older successful one commits after it."""
    from db import deal_company_association_repository as repo
    assert repo.record_sync_run(mode="incremental", status="failed",
                                started_at=_t(10), full_population=True,
                                error="boom")["available"]
    assert repo.record_sync_run(mode="incremental", status="success",
                                started_at=_t(5), complete=True,
                                full_population=True)["available"]
    state = repo.fetch_sync_state()["row"]
    assert (state["last_run_status"], state["last_full_run_status"]) == \
        ("failed", "failed")
    assert state["last_error"] == "boom"


@_needs_pg
def test_29_d_the_run_start_comes_from_the_database_clock(ledger, monkeypatch):
    """Copilot #190: a host clock running ahead must not stamp the run start —
    a future start could never be superseded and would freeze the scan gate."""
    from db import deal_company_association_repository as repo
    from services.deal_company_association_sync_service import (
        sync_deal_company_associations,
    )
    _deal(ledger, "1")
    _fake_reads(monkeypatch, {"1": [901]})
    ahead = datetime.now(tz=timezone.utc) + timedelta(days=365)
    sync_deal_company_associations(mode="incremental", now_fn=lambda: ahead)
    state = repo.fetch_sync_state()["row"]
    for key in ("last_run_started_at", "last_full_run_started_at",
                "last_complete_scan_started_at"):
        assert svc._parse(state[key]) < ahead - timedelta(days=300), key


# ═════════════════════════════════════════════════════════════════════════════
# §4 — sync service, backfill command, scheduler
# ═════════════════════════════════════════════════════════════════════════════

def _fake_reads(monkeypatch, mapping, *, fail=(), calls=None):
    """HubSpot as a dict: deal → companies; deals in ``fail`` raise."""
    def fake(deal_id, **_):
        if calls is not None:
            calls.append(str(deal_id))
        if str(deal_id) in fail:
            raise hubspot.DealCompanyAssociationLookupError("timeout")
        return {"deal_id": str(deal_id), "complete": True, "pages": 1,
                "companies": [_path(c) for c in mapping.get(str(deal_id), [])]}
    monkeypatch.setattr(hubspot, "fetch_deal_company_associations", fake)


_TABLES = ("hubspot_deal_company_association",
           "hubspot_deal_company_observation",
           "hubspot_deal_company_sync_state", "sync_batches",
           "hubspot_deal_ledger")


def _snapshot():
    return {t: _q(f"SELECT count(*) AS n, md5(coalesce(string_agg(x::text, '|' "
                  f"ORDER BY x::text), '')) AS h FROM {t} x")[0]
            for t in _TABLES}


@_needs_pg
def test_30_the_full_scan_covers_every_won_deal_and_proves_it(
        ledger, monkeypatch):
    from db import deal_company_association_repository as repo
    from services.deal_company_association_sync_service import (
        sync_deal_company_associations,
    )
    _deal(ledger, "1")
    _deal(ledger, "2")
    _deal(ledger, "9", hs_is_closed_won=False, deal_stage_id="x")  # not won
    calls = []
    _fake_reads(monkeypatch, {"1": [901], "2": []}, calls=calls)
    out = sync_deal_company_associations(mode="incremental")
    assert calls == ["1", "2"], "the population is the won deals only"
    assert (out["status"], out["complete"], out["population"],
            out["succeeded"], out["applied"]) == ("success", True, 2, 2, 2)
    state = repo.fetch_sync_state()["row"]
    assert state["last_run_status"] == "success"
    assert state["last_complete_scan_population"] == 2
    assert state["last_complete_scan_finished_at"] is not None


@_needs_pg
def test_31_lookup_failures_make_the_run_partial_or_failed_never_success(
        ledger, monkeypatch):
    from db import deal_company_association_repository as repo
    from services.deal_company_association_sync_service import (
        sync_deal_company_associations,
    )
    _deal(ledger, "1")
    _deal(ledger, "2")
    _fake_reads(monkeypatch, {"1": [901]}, fail={"2"})
    out = sync_deal_company_associations(mode="incremental")
    assert (out["status"], out["complete"], out["failed"]) == \
        ("partial", False, 1)
    assert out["failure_reasons"] == {"timeout": 1}
    state = repo.fetch_sync_state()["row"]
    assert state["last_run_status"] == "partial"
    assert state["last_complete_scan_finished_at"] is None, \
        "a partial run never proves a complete scan"
    _fake_reads(monkeypatch, {}, fail={"1", "2"})
    assert sync_deal_company_associations(mode="incremental")["status"] == \
        "failed"
    # The cap ends a run as partial, disclosed.
    _fake_reads(monkeypatch, {"1": [901], "2": [902]})
    capped = sync_deal_company_associations(mode="incremental", max_lookups=1)
    assert (capped["status"], capped["truncated"]) == ("partial", True)


def _backfill(argv):
    from scripts import backfill_deal_company_associations as bf
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = bf.main(argv + ["--json"])
    return code, json.loads(buf.getvalue())


@_needs_pg
def test_32_the_backfill_checkpoints_resumes_and_proves_completion(
        ledger, monkeypatch):
    from db import deal_company_association_repository as repo
    for d in ("1", "2", "3"):
        _deal(ledger, d)
    calls = []
    _fake_reads(monkeypatch, {"1": [901], "2": [902], "3": [903]}, calls=calls)

    code, out = _backfill(["--apply", "--max-lookups", "2"])
    assert code == 1 and out["reason"].startswith("checkpoint")
    assert out["after"] == {"population": 3, "done": 2, "remaining": 1}
    assert repo.fetch_sync_state()["row"]["backfill_status"] == "in_progress"

    # A plain --apply refuses to restart over the in-progress generation.
    code, out = _backfill(["--apply"])
    assert code == 1 and out["reason"].startswith("backfill_in_progress")

    calls.clear()
    code, out = _backfill(["--resume", "--apply"])
    assert code == 0 and out["reason"] == "backfill_generation_complete"
    assert calls == ["3"], "resume reads only what the generation still lacks"
    state = repo.fetch_sync_state()["row"]
    assert state["backfill_status"] == "complete"
    assert state["last_complete_scan_population"] == 3
    # Only the completed generation counts as a full run; its resume passes
    # (subsets) never did.
    assert (state["last_full_run_status"], state["last_full_run_complete"]) == \
        ("success", True)


@_needs_pg
def test_32_b_the_generation_anchor_is_on_the_database_clock(
        ledger, monkeypatch):
    """Copilot #190: the anchor is compared with DB-clock observation starts,
    so it must be on the same clock — or a host running ahead leaves every
    deal pending forever."""
    from db import deal_company_association_repository as repo
    _deal(ledger, "1")
    _fake_reads(monkeypatch, {"1": [901]})
    code, out = _backfill(["--apply"])
    assert code == 0, out
    anchor = svc._parse(out["generation_started_at"])
    assert anchor <= _obs("1")["last_success_started_at"]
    src = (_ROOT / "scripts/backfill_deal_company_associations.py").read_text(
        encoding="utf-8")
    assert "datetime.now" not in src and "repo.db_now()" in src
    assert repo.fetch_sync_state()["row"]["backfill_status"] == "complete"


@_needs_pg
def test_33_the_backfill_cannot_report_complete_while_work_remains(
        ledger, monkeypatch):
    from db import deal_company_association_repository as repo
    for d in ("1", "2", "3"):
        _deal(ledger, d)
    calls = []
    _fake_reads(monkeypatch, {"1": [901], "3": [903]}, fail={"2"}, calls=calls)
    code, out = _backfill(["--apply"])
    assert code == 1 and out["reason"].startswith("stopped_on_failure")
    assert calls == ["1", "2"], "it stops at the failure, not past it"
    assert out["after"]["remaining"] == 2
    state = repo.fetch_sync_state()["row"]
    assert state["backfill_status"] == "in_progress"
    assert state["last_complete_scan_finished_at"] is None
    # The failed deal kept its outcome as a failure — not zero companies.
    assert _obs("2")["last_attempt_status"] == "failed"
    assert _obs("2")["success_zero_companies"] is None


@_needs_pg
def test_34_the_dry_run_writes_nothing(ledger, monkeypatch):
    for d in ("1", "2"):
        _deal(ledger, d)
    _fake_reads(monkeypatch, {"1": [901], "2": [901, 902]})
    before = _snapshot()
    code, out = _backfill([])
    assert _snapshot() == before, "the dry run changed the database"
    assert code == 0 and out["mode"] == "dry_run"
    assert out["run"]["shapes"] == {"one_company": 1, "multiple_companies": 1}
    assert out["database_writes_performed"] is False


@_needs_pg
def test_35_the_scheduler_step_reports_exactly_what_happened(
        ledger, monkeypatch):
    import scheduler.incremental_sync as sync
    _deal(ledger, "1")
    _deal(ledger, "2")
    _fake_reads(monkeypatch, {"1": [901]}, fail={"2"})
    errors = []
    out = sync._sync_deal_company_associations(run_id=None, errors=errors)
    assert (out["status"], out["succeeded"], out["failed"]) == ("partial", 1, 1)
    assert errors and "1 lookup failure" in errors[0]
    batch = _q("SELECT status, row_count FROM sync_batches "
               "WHERE dataset = 'deal_company_associations'")
    assert batch == [{"status": "partial", "row_count": 1}]


def test_36_no_durable_batch_aborts_before_any_hubspot_call(monkeypatch):
    import scheduler.incremental_sync as sync
    calls = []
    _fake_reads(monkeypatch, {"1": [901]}, calls=calls)
    monkeypatch.setattr(sync.db_writers, "start_sync_batch", lambda **k: 0)
    monkeypatch.setattr(sync.db_writers, "finish_sync_batch", lambda **k: True)
    errors = []
    out = sync._sync_deal_company_associations(run_id=1, errors=errors)
    assert out["status"] == "failed" and calls == []
    assert errors


@_needs_pg
def test_36_b_a_step_that_dies_is_recorded_as_a_failed_full_run(
        ledger, monkeypatch):
    import scheduler.incremental_sync as sync
    import services.deal_company_association_sync_service as service
    from db import deal_company_association_repository as repo
    _deal(ledger, "1")
    _fake_reads(monkeypatch, {"1": [901]})
    assert service.sync_deal_company_associations(mode="incremental")["complete"]

    def boom(**_):
        raise RuntimeError("worker killed")

    monkeypatch.setattr(service, "sync_deal_company_associations", boom)
    out = sync._sync_deal_company_associations(run_id=None, errors=[])
    assert out["status"] == "failed"
    state = repo.fetch_sync_state()["row"]
    assert state["last_full_run_status"] == "failed"
    assert _window()["publication"]["customers"]["reason"] == \
        cwt.R_CUSTOMER_IDENTITY_SCAN_NOT_CURRENT


def test_37_the_scheduler_runs_associations_after_the_ledger():
    src = (_ROOT / "scheduler/incremental_sync.py").read_text(encoding="utf-8")
    body = src.split("def run_daily_incremental_sync(")[1]
    assert body.index("_sync_deal_ledger(") < \
        body.index("_sync_deal_company_associations(")
    fn = src.split("def _sync_deal_company_associations(")[1].split("\ndef ")[0]
    for banned in ("customer", "company_id", "hs_is_closed_won"):
        assert banned not in fn.split('"""')[2].lower(), banned


# ═════════════════════════════════════════════════════════════════════════════
# §5 — audits
# ═════════════════════════════════════════════════════════════════════════════

def _coverage_audit():
    from scripts import audit_deal_company_association_coverage as audit
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = audit.main(["--json"])
    return code, json.loads(buf.getvalue())


@_needs_pg
def test_40_the_coverage_audit_certifies_a_complete_scan_and_writes_nothing(
        ledger, monkeypatch):
    from services.deal_company_association_sync_service import (
        sync_deal_company_associations,
    )
    for d in ("1", "2", "3"):
        _deal(ledger, d)
    # Before any observation: coverage is not proven.
    code, out = _coverage_audit()
    assert code == 1
    assert out["checks"]["every_won_deal_has_an_observation_outcome"] is False
    assert out["checks"]["coverage_proven_by_a_complete_scan"] is False

    _fake_reads(monkeypatch, {"1": [901], "2": [901, 902], "3": []})
    assert sync_deal_company_associations(mode="incremental")["complete"]
    before = _snapshot()
    code, out = _coverage_audit()
    assert _snapshot() == before, "the audit changed the database"
    assert code == 0, out["violations"]
    assert out["identity_states"] == {
        "ambiguous_multiple_companies": 1, "resolved_single_customer": 1,
        "unresolved_no_company": 1}
    assert out["crm_findings"]["multiple_company_count"] == 1
    assert out["crm_findings"]["multiple_company_deal_ids_sample"] == ["2"]
    assert out["crm_findings"]["no_company_count"] == 1
    assert out["crm_findings"]["no_company_deal_ids_sample"] == ["3"]
    assert out["coverage"]["complete_scan_certified_deals"] == 3
    assert out["checks"]["complete_scan_covers_current_won_population"] is True
    assert out["storage"]["successful_zero_company_lookups"] == 1
    assert out["hubspot_calls_performed"] is False
    # Every window those deals touch withholds the exact total.
    for w in out["windows"]:
        if w["deals_without_supported_identity"]:
            assert w["customers"] is None and \
                w["customers_status"] != "published"


@_needs_pg
def test_41_the_coverage_audit_catches_broken_storage(ledger, monkeypatch):
    from db.connection import get_conn
    from services.deal_company_association_sync_service import (
        sync_deal_company_associations,
    )
    _deal(ledger, "1")
    _deal(ledger, "2")
    _fake_reads(monkeypatch, {"1": [901]}, fail={"2"})
    sync_deal_company_associations(mode="incremental")
    _fake_reads(monkeypatch, {"1": [901], "2": [902]})
    sync_deal_company_associations(mode="incremental")
    assert _coverage_audit()[0] == 0

    with get_conn() as c, c.cursor() as cur:
        # A path for a company the observation never counted.
        cur.execute("INSERT INTO hubspot_deal_company_association (deal_id, "
                    "company_id, association_category, association_type_id, "
                    "association_path_key, last_observed_at) VALUES "
                    "('1', '999', 'HUBSPOT_DEFINED', '5', 'HUBSPOT_DEFINED:5', "
                    "NOW())")
    code, out = _coverage_audit()
    assert code == 1
    assert out["checks"]["paths_reconcile_with_observation"] is False


@_needs_pg
def test_41_b_a_complete_scan_that_did_not_cover_a_deal_fails_the_audit(
        ledger, monkeypatch):
    """Copilot #190: a recorded complete scan whose start post-dates a won
    deal's latest success did not observe that deal — its claim is false."""
    from db import deal_company_association_repository as repo
    from services.deal_company_association_sync_service import (
        sync_deal_company_associations,
    )
    _deal(ledger, "1")
    _fake_reads(monkeypatch, {"1": [901]})
    assert sync_deal_company_associations(mode="incremental")["complete"]
    assert _coverage_audit()[0] == 0
    later = repo.db_now()["now"] + timedelta(minutes=5)
    assert repo.record_sync_run(mode="incremental", status="success",
                                started_at=later, complete=True,
                                full_population=True,
                                scanned_deal_ids=["1"])["available"]
    code, out = _coverage_audit()
    assert code == 1
    assert out["checks"]["complete_scan_covers_current_won_population"] is False
    assert out["coverage"]["won_deals_behind_complete_scan"] == 1


def test_42_the_coverage_audit_has_no_external_or_write_path():
    from scripts import audit_deal_company_association_coverage as audit
    a = audit.Audit()
    audit.structural_checks(a)
    assert a.violations == []


def _universe(rows, observations, paths):
    return {"available": True, "won_rows": rows,
            "won_definition_rows": [
                {"deal_id": r["deal_id"], "deal_stage_id": r["deal_stage_id"],
                 "hs_is_closed_won": True, "deal_close_date": r["deal_close_date"]}
                for r in rows],
            "unknown_won_rows": [], "acquisition_contacts": [],
            "sync_state": {"bootstrap_status": "complete"},
            "coverage_findings": [],
            "company_associations": {"available": True, "paths": paths,
                                     "observations": observations,
                                     "sync_state": _CURRENT_SCAN}}


#: A full scan that succeeded, covered every deal, and ran just before NOW.
_CURRENT_SCAN = {"last_full_run_status": "success",
                 "last_full_run_complete": True,
                 "last_complete_scan_started_at": "2026-10-07T05:55:00+00:00",
                 "last_complete_scan_finished_at": "2026-10-07T06:00:00+00:00"}


def _ok_obs(deal_id, companies, paths):
    return {"deal_id": deal_id, "last_attempt_status": "success",
            "last_success_started_at": "2026-10-07T11:00:00+00:00",
            "success_company_count": companies, "success_path_count": paths,
            "success_zero_companies": companies == 0,
            "deal_lastmodified_at": None}


def test_43_the_closed_won_audit_catches_customers_published_over_ambiguity(
        monkeypatch):
    """Implementation mutation: the identity resolver calls everything one
    customer. The closed-won audit re-derives from the raw rows and refuses."""
    from scripts import audit_customer_closed_won_truth as audit
    rows = [ledger_row("1", deal_close_date=Q4),
            ledger_row("2", deal_close_date=Q4)]
    universe = _universe(
        rows, [_ok_obs("1", 1, 1), _ok_obs("2", 2, 2)],
        [{"deal_id": "1", "company_id": "901"},
         {"deal_id": "2", "company_id": "901"},
         {"deal_id": "2", "company_id": "902"}])
    honest = svc.get_closed_won_truth([(BUSINESS, "current_quarter")], now=NOW,
                                      universe=universe, resolver=None,
                                      resolver_detail="t")["windows"][0]
    a = audit.Audit()
    audit.window_checks(a, honest, universe, now=NOW)
    assert a.checks.get("customer_count_is_distinct_identities", True)
    assert honest["outcomes"]["customers"] is None

    monkeypatch.setattr(cwt, "customer_identity_state",
                        lambda ids, source_available:
                        cwt.CUSTOMER_RESOLVED_SINGLE)
    broken = svc.get_closed_won_truth([(BUSINESS, "current_quarter")], now=NOW,
                                      universe=universe, resolver=None,
                                      resolver_detail="t")["windows"][0]
    assert broken["publication"]["customers"]["status"] == cwt.PUBLISHED
    a = audit.Audit()
    audit.window_checks(a, broken, universe, now=NOW)
    assert a.checks["customer_count_is_distinct_identities"] is False


def test_44_the_closed_won_audit_catches_a_wrong_lower_bound(monkeypatch):
    """An OUTPUT counterfactual (the dict is edited), not an implementation
    mutation: it proves the lower-bound check can fire. test_43 and test_45
    break the implementation itself."""
    from scripts import audit_customer_closed_won_truth as audit
    rows = [ledger_row("1", deal_close_date=Q4),
            ledger_row("2", deal_close_date=Q4)]
    universe = _universe(rows, [_ok_obs("1", 1, 1), _ok_obs("2", 0, 0)],
                         [{"deal_id": "1", "company_id": "901"}])
    w = svc.get_closed_won_truth([(BUSINESS, "current_quarter")], now=NOW,
                                 universe=universe, resolver=None,
                                 resolver_detail="t")["windows"][0]
    assert w["outcomes"]["confirmed_customer_lower_bound"] == 1
    w["outcomes"]["confirmed_customer_lower_bound"] = 2
    a = audit.Audit()
    audit.window_checks(a, w, universe, now=NOW)
    assert a.checks["customer_lower_bound_is_confirmed_companies"] is False


def test_45_the_audits_catch_a_failed_lookup_read_as_no_company(monkeypatch):
    """Implementation mutation (the auditor's MINOR 1): a deal whose only
    lookup FAILED is put in the map as ``[]``. The customer total is still
    withheld — so "not published" alone proves nothing. Both audits must see
    that the published status and distribution contradict the evidence."""
    from scripts import audit_customer_closed_won_truth as audit
    from scripts import audit_deal_company_association_coverage as cov
    rows = [ledger_row("1", deal_close_date=Q4),
            ledger_row("2", deal_close_date=Q4)]
    failed_only = {"deal_id": "2", "last_attempt_status": "failed",
                   "last_success_started_at": None,
                   "success_company_count": None, "success_path_count": None,
                   "success_zero_companies": None, "deal_lastmodified_at": None}
    universe = _universe(rows, [_ok_obs("1", 1, 1), failed_only],
                         [{"deal_id": "1", "company_id": "901"}])
    real = svc.company_identity_inputs

    def misreads(evidence):
        out = real(evidence)
        out["company_ids_by_deal"]["2"] = []      # failure read as "no company"
        return out

    monkeypatch.setattr(svc, "company_identity_inputs", misreads)
    w = svc.get_closed_won_truth([(BUSINESS, "current_quarter")], now=NOW,
                                 universe=universe, resolver=None,
                                 resolver_detail="t")["windows"][0]
    assert w["outcomes"]["customers"] is None     # withheld either way
    a = audit.Audit()
    audit.window_checks(a, w, universe, now=NOW)
    assert a.checks["customer_verdict_matches_the_evidence"] is False
    identity = cov.independent_identity(universe)
    a = cov.Audit()
    monkeypatch.setattr(svc, "get_closed_won_truth",
                        lambda **k: {"windows": [w]})
    cov.publication_checks(a, universe, identity, now=NOW, scan_current=True)
    assert a.checks["customer_verdict_matches_the_evidence"] is False
    assert a.checks["identity_distribution_matches_the_evidence"] is False


@_needs_pg
def test_46_the_coverage_audit_fails_while_the_scan_is_not_current(
        ledger, monkeypatch):
    """The auditor's probe 6: a once-complete scan, then a failed pipeline."""
    from db import deal_company_association_repository as repo
    from services.deal_company_association_sync_service import (
        sync_deal_company_associations,
    )
    _deal(ledger, "1")
    _fake_reads(monkeypatch, {"1": [901]})
    assert sync_deal_company_associations(mode="incremental")["complete"]
    assert _coverage_audit()[0] == 0
    monkeypatch.setattr(repo, "fetch_won_deal_population",
                        lambda: {"available": False, "deals": [],
                                 "error": "boom"})
    sync_deal_company_associations(mode="incremental")
    code, out = _coverage_audit()
    assert code == 1
    assert out["checks"]["association_scan_is_current"] is False


def test_47_a_zero_lower_bound_over_nothing_observed_is_caught():
    """Copilot #190: the lower bound is checked unconditionally. With every
    member unobserved it must be NULL; a 0 there is a finding nobody made."""
    from scripts import audit_customer_closed_won_truth as audit
    rows = [ledger_row("1", deal_close_date=Q4)]
    universe = _universe(rows, [], [])
    w = svc.get_closed_won_truth([(BUSINESS, "current_quarter")], now=NOW,
                                 universe=universe, resolver=None,
                                 resolver_detail="t")["windows"][0]
    assert w["outcomes"]["confirmed_customer_lower_bound"] is None
    a = audit.Audit()
    audit.window_checks(a, w, universe, now=NOW)
    assert a.checks["customer_lower_bound_is_confirmed_companies"] is True
    w["outcomes"]["confirmed_customer_lower_bound"] = 0
    a = audit.Audit()
    audit.window_checks(a, w, universe, now=NOW)
    assert a.checks["customer_lower_bound_is_confirmed_companies"] is False


# ═════════════════════════════════════════════════════════════════════════════
# §6 — registry and CI
# ═════════════════════════════════════════════════════════════════════════════

def test_50_the_dataset_key_is_registered_and_scheduled():
    import scheduler.incremental_sync as sync
    from services import dataset_keys
    pair = (dataset_keys.DEAL_COMPANY_ASSOCIATION_SOURCE,
            dataset_keys.DEAL_COMPANY_ASSOCIATION_DATASET)
    assert pair == ("hubspot", "deal_company_associations")
    assert dataset_keys.is_registered_pair(*pair)
    assert pair in sync.ACTIVE_SYNC_PAIRS
    assert sync.LABEL_DEAL_COMPANY_ASSOCIATIONS == \
        "hubspot/deal_company_associations"


def test_51_ci_runs_this_suite_in_the_postgresql_step_and_asserts_it_ran():
    wf = (_ROOT / ".github/workflows/pr-ads-153d-checks.yml").read_text(
        encoding="utf-8")
    assert "tests/test_pr_ads_161d2_deal_company_associations.py" in wf
    assert '"tests.test_pr_ads_161d2_deal_company_associations"' in wf
