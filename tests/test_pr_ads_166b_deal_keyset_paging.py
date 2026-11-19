"""PR-ADS-166B (review round 1) — the deal ledger read pages by keyset.

``pull_deals_for_ledger`` paged HubSpot's deal search by its ``after`` offset
over ``hs_lastmodifieddate >= since`` sorted ascending, stopped on the first
response without ``paging.next``, never read ``total`` and never
de-duplicated ids. The reviewer executed three failures against the real
connector:

* a deal already read on page 1 and edited before page 2 moved to the end of
  the result set, every later row shifted up one slot, and the next offset
  skipped an unread deal — ``complete: True``, 250 records, 249 unique ids, a
  closed-won deal never read, and the watermark moved past it;
* 150 deals tied on one timestamp, which HubSpot orders differently per
  query: 33 never read, ``complete: True``;
* a 200 with no ``results`` key: ``complete: True``, no deals.

The fake HubSpot here honours every filter the connector sends
(``GTE``/``GT``/``EQ`` on the timestamp, ``GT`` on ``hs_object_id``, ``IN`` on
the stage), both sort orders, the ``after`` offset (so the pre-fix code can be
run against it), HubSpot's 10,000-result offset ceiling, and reports
``total``. Among tied timestamps its order changes on every query, and a
deal can be edited between queries. It sits under the REAL SDK client: only
``urllib3.PoolManager.request`` is replaced. The sync cases run the real
``sync_deals`` and the real ``record_sync_state`` over a recording
connection, and every assertion is over unique deal ids.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

import hubspot
import pytest
import urllib3

import connectors.hubspot_pull as hubspot_pull

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
WON_STAGE = "326093516"
OPEN_STAGE = "appointmentscheduled"
SEARCH_PATH = "/crm/v3/objects/deals/search"


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _ms(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


class FakeDealSearch:
    """HubSpot's deal search, faithful in the ways that matter here."""

    OFFSET_CEILING = 10000

    def __init__(self, deals: dict, *, seed: int = 7, total_bonus: int = 0,
                 ignore_id_sort: bool = False):
        self.deals = deals                  # id -> properties
        self.rng = random.Random(seed)
        # Honour `hs_object_id GT` but not the id SORT: the premise PR-ADS-161I
        # verified live for contacts and nobody has verified for deals.
        self.ignore_id_sort = ignore_id_sort
        self.total_bonus = total_bonus      # report more than exists
        self.queries: list = []             # request bodies, in order
        self.after_query: dict = {}         # n -> callable(fake), run after query n
        self.fail_from_query = None         # (n, HTTPResponse factory)
        self.raw_override: dict = {}        # n -> raw JSON payload

    # ── filtering ───────────────────────────────────────────────────────────
    def _matches(self, deal_id, props, filters) -> bool:
        for f in filters:
            prop, op = f["propertyName"], f["operator"]
            if prop == "hs_lastmodifieddate":
                have, want = _ms(props["hs_lastmodifieddate"]), int(f["value"])
            elif prop == "hs_object_id":
                have, want = int(deal_id), int(f["value"])
            elif prop == "dealstage" and op == "IN":
                if props.get("dealstage") not in f["values"]:
                    return False
                continue
            else:
                raise AssertionError(f"unexpected filter {f}")
            if op == "GTE" and not have >= want:
                return False
            if op == "GT" and not have > want:
                return False
            if op == "EQ" and not have == want:
                return False
            if op not in ("GTE", "GT", "EQ"):
                raise AssertionError(f"unexpected operator {f}")
        return True

    def answer(self, body: dict):
        self.queries.append(body)
        n = len(self.queries)
        if self.fail_from_query and n >= self.fail_from_query[0]:
            return self.fail_from_query[1]()
        if n in self.raw_override:
            payload = self.raw_override[n]
        else:
            payload = self._search(body)
        hook = self.after_query.get(n)
        if hook:
            hook(self)
        return urllib3.HTTPResponse(
            body=json.dumps(payload).encode(), status=200, reason="OK",
            headers={"Content-Type": "application/json"}, preload_content=True)

    def _search(self, body: dict):
        offset = int(body.get("after") or 0)
        limit = int(body["limit"])
        if offset + limit > self.OFFSET_CEILING:
            return self._refuse_offset()
        filters = body["filterGroups"][0]["filters"]
        rows = [(d, p) for d, p in self.deals.items()
                if self._matches(d, p, filters)]
        sort = (body.get("sorts") or [{}])[0].get("propertyName")
        if sort == "hs_object_id" and self.ignore_id_sort:
            self.rng.shuffle(rows)
        elif sort == "hs_object_id":
            rows.sort(key=lambda r: int(r[0]))
        else:
            # HubSpot keeps no order among tied timestamps between queries.
            tiebreak = {d: self.rng.random() for d, _ in rows}
            rows.sort(key=lambda r: (_ms(r[1]["hs_lastmodifieddate"]),
                                     tiebreak[r[0]]))
        page = rows[offset:offset + limit]
        out = {"total": len(rows) + self.total_bonus, "results": [
            {"id": d, "properties": dict(p), "createdAt": _iso(T0),
             "updatedAt": p["hs_lastmodifieddate"], "archived": False}
            for d, p in page]}
        if offset + limit < len(rows):
            out["paging"] = {"next": {"after": str(offset + limit)}}
        return out

    def _refuse_offset(self):
        raise _OffsetRefused()

    def edit(self, deal_id: str, when: datetime):
        self.deals[deal_id]["hs_lastmodifieddate"] = _iso(when)


class _OffsetRefused(Exception):
    pass


def _http_error(status: int, reason: str):
    return lambda: urllib3.HTTPResponse(
        body=json.dumps({"status": "error", "message": reason}).encode(),
        status=status, reason=reason,
        headers={"Content-Type": "application/json"}, preload_content=True)


@pytest.fixture
def hubspot_fake(monkeypatch):
    monkeypatch.setattr(hubspot_pull.time, "sleep", lambda *_: None)

    def _install(fake: FakeDealSearch):
        def _request(_pool, method, url, *a, **kw):
            assert url.endswith(SEARCH_PATH), url
            body = json.loads(kw.get("body") or "{}")
            try:
                return fake.answer(body)
            except _OffsetRefused:
                # HubSpot answers 400 past its 10,000-result paging ceiling.
                return _http_error(400, "Bad Request")()
        monkeypatch.setattr(urllib3.PoolManager, "request", _request)
        client = hubspot.Client.create(access_token="test-token")
        monkeypatch.setattr(hubspot_pull, "get_client", lambda: client)
        return fake

    return _install


def _ladder(n: int, *, won: str | None = None, start: int = 1000) -> dict:
    """``n`` deals, one second apart, ids ``start..``; ``won`` is closed-won."""
    deals = {}
    for i in range(n):
        did = str(start + i)
        is_won = did == won
        deals[did] = {
            "dealname": f"Deal {did}",
            "dealstage": WON_STAGE if is_won else OPEN_STAGE,
            "hs_is_closed": "true" if is_won else "false",
            "hs_is_closed_won": "true" if is_won else "false",
            "amount": "5000", "deal_currency_code": "USD",
            "hs_lastmodifieddate": _iso(T0 + timedelta(seconds=i))}
    return deals


def _ties(n: int, *, start: int = 5000) -> dict:
    ts = _iso(T0)
    return {str(start + i): {"dealname": f"T{i}", "dealstage": WON_STAGE,
                             "hs_is_closed": "true", "hs_is_closed_won": "true",
                             "amount": "100", "deal_currency_code": "USD",
                             "hs_lastmodifieddate": ts}
            for i in range(n)}


def _ids(pull) -> list:
    return [d["id"] for d in pull["deals"]]


# ─────────────────────────────────────────────────────────────────────────────
# The real sync, the real record_sync_state
# ─────────────────────────────────────────────────────────────────────────────
class _Conn:
    def __init__(self, sink, stored=None):
        self.sink = sink
        self.stored = stored

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.sink.append(params)
        # What the real SQL does with the watermark: replace it only when
        # `advance` (`last_modified_watermark = CASE WHEN advance …`).
        if self.stored is not None and params and params.get("advance"):
            self.stored["watermark"] = params["watermark"]

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def ledger(monkeypatch):
    import db.deal_ledger_repository as repo
    import services.hubspot_deal_sync_service as svc

    state: list = []
    written: list = []
    stored = {"watermark": "2026-10-01T00:00:00+00:00"}
    monkeypatch.setattr(repo, "get_conn", lambda: _Conn(state, stored))
    monkeypatch.setattr(repo, "fetch_sync_state", lambda: {
        "available": True,
        "row": {"last_modified_watermark": stored["watermark"]}})
    monkeypatch.setattr(repo, "upsert_deal", lambda row, **kw: (
        written.append(row) or {"available": True, "written": 1,
                                "skipped_stale": 0, "error": None}))
    monkeypatch.setattr(svc, "_fx_rates_for", lambda *a, **k: {})
    monkeypatch.setattr(hubspot_pull, "fetch_portal_home_currency", lambda: {
        "available": True, "home_currency_code": "USD", "verified": True})
    monkeypatch.setattr(hubspot_pull, "fetch_deal_associations", lambda d: {
        "deal_id": d, "contacts": [], "complete": True})
    return {"svc": svc, "state": state, "written": written, "stored": stored}


# ─────────────────────────────────────────────────────────────────────────────
# 1. A deal edited mid-scan (review probe 1)
# ─────────────────────────────────────────────────────────────────────────────
def _probe_one(hubspot_fake) -> FakeDealSearch:
    fake = hubspot_fake(FakeDealSearch(_ladder(250, won="1100")))
    # A deal ALREADY READ on page 1 is edited in HubSpot before page 2.
    fake.after_query[1] = lambda f: f.edit("1005", T0 + timedelta(hours=2))
    return fake


def test_1_a_a_deal_edited_after_page_one_skips_no_unread_deal(hubspot_fake):
    fake = _probe_one(hubspot_fake)

    pull = hubspot_pull.pull_deals_for_ledger()

    ids = _ids(pull)
    assert len(set(ids)) == len(ids), "a deal was returned twice"
    assert set(ids) == set(fake.deals), (
        f"never read: {sorted(set(fake.deals) - set(ids))}")
    assert "1100" in ids, "the closed-won deal was skipped"
    assert pull["complete"] is True and pull["error"] is None


def test_1_b_the_edited_deal_is_kept_as_its_newest_version_in_ascending_order(hubspot_fake):
    _probe_one(hubspot_fake)

    pull = hubspot_pull.pull_deals_for_ledger()

    ids = _ids(pull)
    assert len(ids) == len(set(ids)) == 250, "one record per deal id"
    by_id = {d["id"]: d for d in pull["deals"]}
    assert by_id["1005"]["properties"]["hs_lastmodifieddate"] == _iso(
        T0 + timedelta(hours=2))
    stamps = [_ms(d["properties"]["hs_lastmodifieddate"]) for d in pull["deals"]]
    assert stamps == sorted(stamps)
    assert _ids(pull)[-1] == "1005"


def test_1_c_through_the_real_sync_every_deal_and_the_won_deal_are_written(
        hubspot_fake, ledger):
    fake = _probe_one(hubspot_fake)

    result = ledger["svc"].sync_deals(batch_id=1)

    written = {r["deal_id"] for r in ledger["written"]}
    assert written == set(fake.deals)
    assert "1100" in written
    assert result["status"] == "success"
    (params,) = ledger["state"]
    assert params["status"] == "success" and params["advance"] is True
    # The success watermark is the newest deal read, and no deal is unread.
    assert params["watermark"] == _iso(T0 + timedelta(hours=2))


def test_1_d_a_run_cut_short_mid_scan_never_checkpoints_past_an_unread_deal(
        hubspot_fake, ledger):
    """Page 1 read, a page-1 deal edited, page 2 read, then a 429 that never
    clears. Under offset paging page 2 skipped deal 1100 and the clean-prefix
    checkpoint moved past it."""
    fake = _probe_one(hubspot_fake)
    fake.fail_from_query = (3, _http_error(429, "Too Many Requests"))

    result = ledger["svc"].sync_deals(batch_id=1)

    assert result["status"] == "partial"
    (params,) = ledger["state"]
    assert params["advance"] is True and result["watermark_is_checkpoint"] is True
    checkpoint = _ms(params["watermark"])
    written = {r["deal_id"] for r in ledger["written"]}
    unread = set(fake.deals) - written
    assert unread, "the run was meant to stop early"
    behind = sorted(d for d in unread
                    if _ms(fake.deals[d]["hs_lastmodifieddate"]) < checkpoint)
    assert behind == [], f"checkpoint {params['watermark']} passed unread {behind}"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Tied timestamps (review probe 3)
# ─────────────────────────────────────────────────────────────────────────────
def test_2_a_150_deals_on_one_timestamp_in_a_shifting_order_are_all_read(hubspot_fake):
    fake = hubspot_fake(FakeDealSearch(_ties(150)))

    pull = hubspot_pull.pull_deals_for_ledger()

    ids = _ids(pull)
    assert len(ids) == len(set(ids)) == 150
    assert set(ids) == set(fake.deals)
    assert pull["complete"] is True
    # The tie group was walked by id, not by offset.
    walked = [q for q in fake.queries
              if any(f["propertyName"] == "hs_object_id"
                     for f in q["filterGroups"][0]["filters"])]
    assert walked and all(q["sorts"][0]["propertyName"] == "hs_object_id"
                          for q in walked)


def test_2_b_ties_plus_a_ladder_with_an_edit_mid_walk_through_the_real_sync(
        hubspot_fake, ledger):
    deals = {**_ties(130), **_ladder(60, start=9000)}
    fake = hubspot_fake(FakeDealSearch(deals, seed=11))
    # A tied deal already read is edited while the tie group is being walked.
    fake.after_query[2] = lambda f: f.edit("5003", T0 + timedelta(days=1))

    result = ledger["svc"].sync_deals(batch_id=1)

    written = [r["deal_id"] for r in ledger["written"]]
    assert len(written) == len(set(written))
    assert set(written) == set(deals)
    assert result["status"] == "success"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Completeness is proven against HubSpot's total
# ─────────────────────────────────────────────────────────────────────────────
def test_3_a_fewer_unique_deals_than_hubspot_counted_is_partial_not_complete(
        hubspot_fake, ledger):
    hubspot_fake(FakeDealSearch(_ladder(120), total_bonus=1))

    pull = hubspot_pull.pull_deals_for_ledger()
    assert pull["available"] is True and pull["complete"] is False
    assert "INCOMPLETE" in pull["error"]
    assert len(set(_ids(pull))) == 120

    result = ledger["svc"].sync_deals(batch_id=1)
    assert result["status"] == "partial"
    params = ledger["state"][-1]
    assert params["status"] == "partial"
    # Review round 2: a shortfall proves a gap exists, not where. No prefix
    # is provably clean, so nothing advances and the stored watermark stays.
    assert params["advance"] is False and params["watermark"] is None
    assert result["watermark"] is None
    assert ledger["stored"]["watermark"] == "2026-10-01T00:00:00+00:00"


def test_3_b_a_page_with_no_results_list_is_an_error_not_an_empty_answer(
        hubspot_fake, ledger):
    fake = hubspot_fake(FakeDealSearch(_ladder(5)))
    fake.raw_override[1] = {"total": 42}

    pull = hubspot_pull.pull_deals_for_ledger()
    assert pull["complete"] is False
    assert "results" in pull["error"]
    assert pull["pages"] == 0

    fake.queries.clear()
    result = ledger["svc"].sync_deals(batch_id=1)
    # Nothing was read: a failed run, and no watermark moves.
    assert result["status"] == "failed"
    assert ledger["state"][-1]["advance"] is False


def test_3_c_a_missing_total_is_never_complete_and_the_run_is_failed(
        hubspot_fake, ledger):
    fake = hubspot_fake(FakeDealSearch(_ladder(5)))
    fake.raw_override[1] = {"results": []}

    pull = hubspot_pull.pull_deals_for_ledger()

    assert pull["complete"] is False
    assert "total" in pull["error"]

    fake.queries.clear()
    result = ledger["svc"].sync_deals(batch_id=1)
    # Nothing was read (review round 2, R2-MINOR-1): failed, not partial.
    assert result["status"] == "failed"
    assert ledger["state"][-1]["status"] == "failed"
    assert ledger["state"][-1]["advance"] is False


def test_3_d_an_unreadable_page_mid_read_keeps_the_keyset_safe_checkpoint(
        hubspot_fake, ledger):
    """Not a proven gap: every deal not reached sorts at or after the newest
    one read, so the clean prefix may be checkpointed (review R2-OBS-1)."""
    fake = hubspot_fake(FakeDealSearch(_ladder(250)))
    fake.raw_override[2] = {"total": 250}

    result = ledger["svc"].sync_deals(batch_id=1)

    assert result["status"] == "partial"
    assert "results" in result["error"]
    params = ledger["state"][-1]
    assert params["advance"] is True and result["watermark_is_checkpoint"]
    checkpoint = _ms(params["watermark"])
    written = {r["deal_id"] for r in ledger["written"]}
    assert all(_ms(fake.deals[d]["hs_lastmodifieddate"]) >= checkpoint
               for d in set(fake.deals) - written)


def test_3_e_a_tie_walk_whose_id_sort_is_ignored_moves_no_watermark_and_the_next_run_reads_the_gap(
        hubspot_fake, ledger):
    """Review S4. HubSpot honours `hs_object_id GT` but not the id sort on
    deals (unverified either way). The walk skips tied deals; the scan
    continues to the end and comes up short of `total`. Under the round-1
    code the partial run checkpointed past the skipped deals, and the next
    incremental never read them: a won deal was lost for good."""
    deals = _ladder(578, won="1150")
    tie_ts = deals["1100"]["hs_lastmodifieddate"]
    for i in range(100, 350):
        deals[str(1000 + i)]["hs_lastmodifieddate"] = tie_ts
    hubspot_fake(FakeDealSearch(deals, seed=11, ignore_id_sort=True))

    run1 = ledger["svc"].sync_deals(batch_id=1)

    written1 = {r["deal_id"] for r in ledger["written"]}
    missed = set(deals) - written1
    assert missed, "the premise failure was meant to skip deals"
    assert run1["status"] == "partial" and "INCOMPLETE" in run1["error"]
    params = ledger["state"][-1]
    assert params["advance"] is False, (
        "a run that proved a gap moved the watermark past it")
    assert ledger["stored"]["watermark"] == "2026-10-01T00:00:00+00:00"

    # Run 2: HubSpot answers properly. It must reach every deal run 1 missed.
    ledger["written"].clear()
    hubspot_fake(FakeDealSearch(deals, seed=12))
    run2 = ledger["svc"].sync_deals(batch_id=2)

    written2 = {r["deal_id"] for r in ledger["written"]}
    assert run2["status"] == "success"
    assert missed <= written2, f"never recovered: {sorted(missed - written2)}"
    assert "1150" in written1 | written2


# ─────────────────────────────────────────────────────────────────────────────
# 4. No offset is ever sent
# ─────────────────────────────────────────────────────────────────────────────
def test_4_a_more_than_10000_deals_are_read_past_hubspots_offset_ceiling(hubspot_fake):
    fake = hubspot_fake(FakeDealSearch(_ladder(10050, won="10049", start=1)))

    pull = hubspot_pull.pull_deals_for_ledger()

    assert pull["complete"] is True, pull["error"]
    ids = _ids(pull)
    assert len(ids) == len(set(ids)) == 10050
    assert all(not q.get("after") for q in fake.queries)


def test_4_b_a_stage_filter_rides_on_every_keyset_query_and_an_edit_skips_nothing(
        hubspot_fake):
    deals = _ladder(300)
    won = {d for i, d in enumerate(sorted(deals, key=int)) if i % 2 == 0}
    for d in won:
        deals[d]["dealstage"] = WON_STAGE
    fake = hubspot_fake(FakeDealSearch(deals))
    fake.after_query[1] = lambda f: f.edit("1004", T0 + timedelta(hours=3))

    pull = hubspot_pull.pull_deals_for_ledger(stages=[WON_STAGE])

    ids = _ids(pull)
    assert len(ids) == len(set(ids))
    assert set(ids) == won and pull["complete"] is True
    assert len(fake.queries) > 1
    for q in fake.queries:
        assert {"propertyName": "dealstage", "operator": "IN",
                "values": [WON_STAGE]} in q["filterGroups"][0]["filters"]


# ─────────────────────────────────────────────────────────────────────────────
# 5. When the ledger withholds after a run that is not a success
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("status", ["partial", "failed"])
def test_5_a_one_unsuccessful_incremental_withholds_at_once_not_after_36_hours(status):
    """docs/09 said "once 36 hours pass". Executed: the coverage gate fails on
    the first unsuccessful run (`last_sync_not_successful`), so coverage is
    unproven and every figure withholds for that reason immediately; the 36 h
    `canonical_ledger_stale` limit never gets to judge (`stale` is None)."""
    from services import canonical_customer_revenue_service as ccr
    from services.revenue_reconciliation_service import check_sync_coverage

    now = datetime(2026, 11, 19, 10, tzinfo=timezone.utc)
    row = {"bootstrap_status": "complete",
           "bootstrap_started_at": "2026-10-01T00:00:00+00:00",
           "bootstrap_completed_at": "2026-10-02T00:00:00+00:00",
           "last_incremental_at": "2026-11-19T09:00:00+00:00",
           "last_sync_mode": "incremental", "last_status": status,
           "last_modified_watermark": "2026-11-19T08:00:00+00:00"}

    findings = check_sync_coverage({"available": True, "row": row})
    block = ccr.freshness_block(row, findings, now)
    staleness = ccr.ledger_staleness(block)

    assert [f["code"] for f in findings] == ["last_sync_not_successful"]
    assert block["coverage_proven"] is False
    assert block["latest_successful_incremental_at"] is None
    assert staleness["stale"] is None
    # Control: the same row one hour after a SUCCESS publishes.
    ok = {**row, "last_status": "success"}
    assert check_sync_coverage({"available": True, "row": ok}) == []
    assert ccr.ledger_staleness(ccr.freshness_block(ok, [], now))["stale"] is False


# ─────────────────────────────────────────────────────────────────────────────
# 6. The contact scan: an unreadable page is not a proven gap (R2-OBS-1)
# ─────────────────────────────────────────────────────────────────────────────
def test_6_a_an_unreadable_contact_page_keeps_the_bootstrap_checkpoint_not_a_rewind(
        monkeypatch):
    """A page with no `results` in the middle of a contact bootstrap used to
    raise the same error as a shortfall, and PR-ADS-161I rewinds a shortfall
    to the start — about 22,000 contacts. The prefix before an unreadable
    page is keyset-safe, so the run fails and keeps its checkpoint."""
    from tests.test_pr_ads_161i_contact_scan_paging import (
        BASE_MS, _client, _HubSpotLikeSearch, _Resp, _real_iterator, _writers)

    class _UnreadableSecondPage(_HubSpotLikeSearch):
        def do_search(self, public_object_search_request=None):
            page = super().do_search(public_object_search_request)
            if len(self.calls) == 2:
                return _Resp(None, None, page.total)
            return page

    sync, writers = _writers(monkeypatch)
    search = _UnreadableSecondPage([BASE_MS + 7 * i for i in range(300)])

    with pytest.raises(hubspot_pull.HubSpotSearchPageUnreadableError):
        list(hubspot_pull.iter_contacts_modified_since(0, client=_client(
            _UnreadableSecondPage([BASE_MS + 7 * i for i in range(300)]))))
    assert not issubclass(hubspot_pull.HubSpotSearchPageUnreadableError,
                          hubspot_pull.HubSpotSearchIncompleteError)

    result = sync.run_contact_funnel_sync(
        mode=sync.MODE_BOOTSTRAP, page_iterator=_real_iterator(search),
        restart_from_epoch=True, now=datetime(2026, 11, 16, tzinfo=timezone.utc))

    assert result["status"] == "failed"
    assert "results" in (writers.state.get("last_error") or "")
    kept = writers.state["last_modified_watermark"]
    assert kept is not None, "the bootstrap was rewound to the epoch"
    first_page_max = BASE_MS + 7 * 99
    assert int(kept.timestamp() * 1000) == first_page_max
