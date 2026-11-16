"""PR-ADS-161I — the contact scan pages ONE result set, and proves it is whole.

Production, 2026-11-16: HubSpot held 21,751 contacts; the canonical contact
store held 6,389. A full rebuild from the epoch "completed" after 2,000. The
connector re-sent its advancing watermark as the search filter on every page
while keeping the paging offset, so each request was the Nth page of a
different, shrinking result set: it skipped ahead further on every page and hit
"no next page" early, reporting success. The existing fakes ignored the filter,
so no test could see it.

The fake below behaves like HubSpot's search API: it honours the
``lastmodifieddate GTE`` filter, sorts ascending, pages by offset, reports
``total`` and refuses to page past 10,000 results.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from connectors import hubspot_pull as hp

BASE_MS = 1_737_635_582_000          # 2025-01-23T12:33:02Z, as in production


class _Obj:
    def __init__(self, payload):
        self._payload = payload

    def to_dict(self):
        return self._payload


class _Next:
    def __init__(self, after):
        self.after = after


class _Paging:
    def __init__(self, after):
        self.next = _Next(after)


class _Resp:
    def __init__(self, results, after, total):
        self.results = results
        self.paging = _Paging(after) if after is not None else None
        self.total = total


class _HubSpotLikeSearch:
    """Behaves like HubSpot's contact search for the queries the scan sends.

    Honours ``lastmodifieddate`` GTE / GT / EQ and ``hs_object_id`` GT filters,
    sorts by the requested property, pages by ``after`` offset (and refuses to
    page past 10,000), reports ``total``. Optionally changes its data before
    every request (``mutate``) and returns tied rows in a different order on
    every request (``shuffle_ties``), as HubSpot's order among ties is
    arbitrary. ``hidden`` ids are counted in ``total`` but never returned.
    """

    def __init__(self, modified_ms, *, report_total=True, hidden=(),
                 mutate=None, shuffle_ties=False):
        self.rows = sorted(((ms, f"c{i}") for i, ms in enumerate(modified_ms)))
        self.report_total = report_total
        self.hidden = set(hidden)
        self.mutate = mutate
        self.shuffle_ties = shuffle_ties
        self._rng = __import__("random").Random(161)
        self.calls = []

    @staticmethod
    def _num(cid):
        return int("".join(ch for ch in cid if ch.isdigit()))

    def do_search(self, public_object_search_request=None):
        req = public_object_search_request or {}
        if self.mutate:
            self.mutate(self)
        rows = list(self.rows)
        for f in req["filterGroups"][0]["filters"]:
            prop, op, val = f["propertyName"], f["operator"], int(f["value"])
            if prop == "lastmodifieddate":
                keep = {"GTE": lambda ms: ms >= val, "GT": lambda ms: ms > val,
                        "EQ": lambda ms: ms == val}[op]
                rows = [r for r in rows if keep(r[0])]
            elif prop == "hs_object_id":
                assert op == "GT"
                rows = [r for r in rows if self._num(r[1]) > val]
            else:
                raise AssertionError(f"unexpected filter {prop}")
        sort = req["sorts"][0]["propertyName"]
        if sort == "hs_object_id":
            rows.sort(key=lambda r: self._num(r[1]))
        else:
            if self.shuffle_ties:
                self._rng.shuffle(rows)
            rows.sort(key=lambda r: r[0])          # stable: ties stay shuffled
        total = len(rows)
        rows = [r for r in rows if r[1] not in self.hidden]
        offset = int(req.get("after") or 0)
        self.calls.append((req["filterGroups"][0]["filters"], offset))
        if offset >= hp.HUBSPOT_SEARCH_RESULT_CAP:
            raise AssertionError("HubSpot refuses to page past 10,000 results")
        limit = int(req["limit"])
        page = rows[offset:offset + limit]
        nxt = offset + limit
        after = str(nxt) if nxt < len(rows) else None
        return _Resp([_Obj({"id": cid, "properties": {
            "lastmodifieddate": str(ms)}}) for ms, cid in page], after,
            total if self.report_total else None)


def _client(search):
    c = type("C", (), {})()
    c.crm = type("R", (), {})()
    c.crm.contacts = type("K", (), {})()
    c.crm.contacts.search_api = search
    return c


def _scan(search, since=0):
    ids, metas = [], []
    for rows, meta in hp.iter_contacts_modified_since(since, client=_client(search)):
        metas.append(meta)
        ids.extend(r["id"] for r in rows)
    return ids, metas


def test_01_a_full_scan_reads_every_contact_not_the_first_2000():
    """The production failure: 2,500 contacts, each a few ms apart. The old
    connector yielded a fraction and reported complete."""
    search = _HubSpotLikeSearch([BASE_MS + 7 * i for i in range(2500)])
    ids, metas = _scan(search)
    assert len(set(ids)) == 2500
    assert metas[-1]["complete"] is True
    assert metas[-1]["source_total"] == 2500


def test_02_no_request_ever_pages_by_offset():
    """Keyset only: every request has offset 0, so a result set that changes
    between requests cannot shift what the next page returns."""
    search = _HubSpotLikeSearch([BASE_MS + i for i in range(950)]
                                + [BASE_MS + 5000] * 260)
    _scan(search)
    assert {offset for _filters, offset in search.calls} == {0}


def test_03_a_scan_past_10000_reads_everything_without_hitting_the_cap():
    n = hp.HUBSPOT_SEARCH_RESULT_CAP + 2_345
    search = _HubSpotLikeSearch([BASE_MS + 3 * i for i in range(n)])
    ids, metas = _scan(search)
    assert len(set(ids)) == n
    assert metas[-1]["complete"] is True
    assert metas[-1]["unique_read"] == n


def test_04_an_incremental_scan_from_a_watermark_reads_all_newer_contacts():
    search = _HubSpotLikeSearch([BASE_MS + 10 * i for i in range(1200)])
    since_ms = BASE_MS + 10 * 400            # contacts 400..1199 qualify
    ids, metas = _scan(search, since=since_ms)
    assert len(set(ids)) == 800
    assert metas[-1]["source_total"] == 800


def test_05_a_scan_that_ends_short_of_hubspots_total_raises_not_completes():
    """The guard, independent of the paging logic: if contacts go missing for
    ANY reason, the scan must not end with a completion sentinel."""
    search = _HubSpotLikeSearch([BASE_MS + 7 * i for i in range(1000)],
                                hidden={f"c{i}" for i in range(0, 1000, 10)})
    with pytest.raises(hp.HubSpotSearchIncompleteError):
        _scan(search)


def test_06_a_response_without_hubspots_total_cannot_prove_completeness():
    search = _HubSpotLikeSearch([BASE_MS + i for i in range(50)],
                                report_total=False)
    with pytest.raises(hp.HubSpotSearchIncompleteError):
        _scan(search)


def test_07_tolerance_is_1pct_or_2_and_zero_read_never_passes():
    assert hp._assert_scan_complete(1000, {f"c{i}" for i in range(990)}) is None
    with pytest.raises(hp.HubSpotSearchIncompleteError):
        hp._assert_scan_complete(1000, {f"c{i}" for i in range(989)})
    assert hp._assert_scan_complete(100, {f"c{i}" for i in range(98)}) is None
    with pytest.raises(hp.HubSpotSearchIncompleteError):
        hp._assert_scan_complete(100, {f"c{i}" for i in range(97)})
    with pytest.raises(hp.HubSpotSearchIncompleteError):
        hp._assert_scan_complete(3, set())          # tiny result set, none read
    assert hp._assert_scan_complete(0, set()) is None


def test_08_contacts_changed_during_the_scan_never_cause_a_skip():
    """Production data moves while the scan runs. Before every request this
    fake (a) re-modifies two contacts the scan has ALREADY read, (b) deletes
    one it has already read and (c) creates a new one. With offset paging each
    of these shifts the live result set and skips an unread contact; the
    count check cannot see it. Every contact never touched must be read."""
    n = 2500
    state = {"touch": 0, "new": 0}
    read = set()

    def mutate(fake):
        if not read:
            return
        newest = max(ms for ms, _ in fake.rows)
        done = [r for r in fake.rows if r[1] in read]
        for k in range(min(2, len(done))):
            ms, cid = done[(state["touch"] + k) % len(done)]
            fake.rows.remove((ms, cid))
            fake.rows.append((newest + 1 + k, cid))
        state["touch"] += 2
        victim = next((r for r in fake.rows if r[1] in read), None)
        if victim:
            fake.rows.remove(victim)
        fake.rows.append((newest + 10, f"new{state['new']}"))
        state["new"] += 1

    search = _HubSpotLikeSearch([BASE_MS + 7 * i for i in range(n)],
                                mutate=mutate)
    for rows, meta in hp.iter_contacts_modified_since(
            0, client=_client(search)):
        read.update(r["id"] for r in rows)
    missing = {f"c{i}" for i in range(n)} - read
    assert not missing, f"{len(missing)} contacts never read"


def test_09_a_tie_group_larger_than_a_page_is_walked_by_id():
    """Production has contacts sharing one bulk-update millisecond."""
    ms = [BASE_MS] * 350 + [BASE_MS + 1000 + i for i in range(120)]
    search = _HubSpotLikeSearch(ms)
    ids, metas = _scan(search)
    assert len(set(ids)) == 470
    assert metas[-1]["complete"] is True
    walked = [f for f, _o in search.calls
              if any(x["propertyName"] == "hs_object_id" for x in f)]
    assert walked, "the tie group was not walked by id"


def test_10_more_than_10000_contacts_on_one_timestamp_are_all_read():
    """No offset means no 10,000 ceiling inside a tie group either."""
    n = hp.HUBSPOT_SEARCH_RESULT_CAP + 50
    search = _HubSpotLikeSearch([BASE_MS] * n + [BASE_MS + 1])
    ids, metas = _scan(search)
    assert len(set(ids)) == n + 1
    assert metas[-1]["complete"] is True


def test_10a_tie_order_differing_between_requests_skips_nothing():
    """HubSpot's order among tied rows is arbitrary. Audit case [A]: a
    350-row tie group reshuffled on every request lost 99 contacts under
    offset walking and still reported complete."""
    ms = ([BASE_MS + 7 * i for i in range(400)] + [BASE_MS + 9000] * 350
          + [BASE_MS + 10_000 + i for i in range(300)])
    search = _HubSpotLikeSearch(ms, shuffle_ties=True)
    ids, metas = _scan(search)
    assert len(set(ids)) == 1050
    assert metas[-1]["complete"] is True


def test_10b_a_tie_member_modified_mid_walk_skips_no_other_member():
    """Audit case [B]: a 3,000-row tie group in which one already-read member
    is modified before every request lost 29 contacts under offset walking,
    inside the slack, reporting complete."""
    n = 3000
    read = set()

    def mutate(fake):
        done = [r for r in fake.rows if r[1] in read and r[0] == BASE_MS]
        if done:
            fake.rows.remove(done[0])
            fake.rows.append((BASE_MS + 50_000 + len(read), done[0][1]))

    search = _HubSpotLikeSearch([BASE_MS] * n + [BASE_MS + 1 + i
                                                 for i in range(200)],
                                mutate=mutate)
    for rows, meta in hp.iter_contacts_modified_since(
            0, client=_client(search)):
        read.update(r["id"] for r in rows)
    missing = {f"c{i}" for i in range(n + 200)} - read
    assert not missing, f"{len(missing)} contacts never read"


def test_10c_a_search_that_ignores_the_id_keyset_stalls_not_completes():
    class _IgnoresId(_HubSpotLikeSearch):
        def do_search(self, public_object_search_request=None):
            req = dict(public_object_search_request)
            fg = [{"filters": [f for f in req["filterGroups"][0]["filters"]
                               if f["propertyName"] != "hs_object_id"]}]
            req["filterGroups"] = fg
            return super().do_search(req)

    search = _IgnoresId([BASE_MS] * 250)
    with pytest.raises(hp.HubSpotSearchStalledError):
        _scan(search)


def _writers(monkeypatch):
    from services import hubspot_contact_funnel_sync_service as sync
    from tests.test_pr_ads_153b_canonical_funnel import _FakeWriters
    writers = _FakeWriters()
    monkeypatch.setattr(sync, "db_writers", writers)
    return sync, writers


def _real_iterator(search):
    def iterator(since, max_pages=None):
        return hp.iter_contacts_modified_since(
            since, max_pages=max_pages, client=_client(search))
    return iterator


def test_11_an_incomplete_scan_is_never_completed_by_the_next_run(monkeypatch):
    """The audit's blocker: run 1 raises, but its per-page checkpoints had
    moved the watermark past the gap; run 2 resumed from there, checked only
    the tail and marked the bootstrap COMPLETE. The rewind makes run 2 start
    from the epoch again, so it meets the same gap and fails too."""
    sync, writers = _writers(monkeypatch)
    search = _HubSpotLikeSearch([BASE_MS + 7 * i for i in range(1000)],
                                hidden={f"c{i}" for i in range(100, 200)})
    now = datetime(2026, 11, 16, tzinfo=timezone.utc)

    run1 = sync.run_contact_funnel_sync(
        mode=sync.MODE_BOOTSTRAP, page_iterator=_real_iterator(search),
        restart_from_epoch=True, now=now)
    assert run1["status"] == "failed"
    assert writers.state["bootstrap_status"] == sync.BOOTSTRAP_PARTIAL
    assert writers.state["last_modified_watermark"] is None
    assert "INCOMPLETE" in (writers.state.get("last_error") or "")

    run2 = sync.run_contact_funnel_sync(
        mode=sync.get_bootstrap_mode(), page_iterator=_real_iterator(search),
        now=now)
    assert run2["status"] == "failed"
    assert writers.state["bootstrap_status"] != sync.BOOTSTRAP_COMPLETE

    search.hidden.clear()                      # the gap is gone
    run3 = sync.run_contact_funnel_sync(
        mode=sync.get_bootstrap_mode(), page_iterator=_real_iterator(search),
        now=now)
    assert run3["status"] == "success"
    assert writers.state["bootstrap_status"] == sync.BOOTSTRAP_COMPLETE
    assert run3["source_total"] == 1000 and run3["unique_contacts_read"] == 1000
    written = {r["contact_id"] for page in writers.upserts for r in page}
    assert written >= {f"c{i}" for i in range(1000)}


def test_12_an_incomplete_incremental_rewinds_to_where_it_began(monkeypatch):
    sync, writers = _writers(monkeypatch)
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    writers.state.update(bootstrap_status=sync.BOOTSTRAP_COMPLETE,
                         last_modified_watermark=start)
    start_ms = int(start.timestamp() * 1000)
    search = _HubSpotLikeSearch([start_ms + 60_000 + 7 * i for i in range(500)],
                                hidden={"c10", "c11", "c12", "c13", "c14",
                                        "c15", "c16", "c17"})
    result = sync.run_contact_funnel_sync(
        mode=sync.MODE_INCREMENTAL, page_iterator=_real_iterator(search),
        overlap_minutes=0, now=datetime(2026, 11, 16, tzinfo=timezone.utc))
    assert result["status"] == "failed"
    assert writers.state["last_modified_watermark"] == start
    assert writers.state["last_incremental_status"] == "failed"


def test_13_the_migration_withdraws_the_false_complete_once(monkeypatch):
    """On a live database: a stored 'complete' over the old scan becomes
    'partial' with no watermark, exactly once."""
    from tests import test_pr_ads_153b_pg_integration as pgmod
    if not pgmod._have_postgres():
        pytest.skip("PostgreSQL not available")
    cluster = pgmod._PgCluster().start()
    try:
        monkeypatch.setenv("DATABASE_URL", cluster.url)
        import db.connection as connection
        monkeypatch.setattr(connection, "_pool", None, raising=False)
        connection.init_pool()
        from db.schema import init_db
        init_db()
        mig = "PR-ADS-161I-contact-funnel-rebuild-from-epoch"
        with cluster.connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO hubspot_contact_funnel_sync_state
                (scope, bootstrap_status, bootstrap_completed_at,
                 last_modified_watermark)
                VALUES ('contacts', 'complete', now(), now())""")
            cur.execute("DELETE FROM migrations WHERE migration_id = %s", (mig,))
        init_db()
        with cluster.connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT bootstrap_status, last_modified_watermark,
                bootstrap_completed_at FROM hubspot_contact_funnel_sync_state
                WHERE scope = 'contacts'""")
            assert cur.fetchone() == ("partial", None, None)
            cur.execute("""UPDATE hubspot_contact_funnel_sync_state
                SET bootstrap_status = 'complete' WHERE scope = 'contacts'""")
        init_db()                                   # runs once only
        with cluster.connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT bootstrap_status FROM
                hubspot_contact_funnel_sync_state WHERE scope = 'contacts'""")
            assert cur.fetchone()[0] == "complete"
    finally:
        if getattr(connection, "_pool", None) is not None:
            connection._pool.closeall()
            connection._pool = None
        cluster.stop()


def test_14_an_incremental_never_restores_a_withdrawn_complete(monkeypatch):
    """Audit minor 1: an incremental wrote back the bootstrap_status it read
    at its start, so one running across the migration restored 'complete'."""
    sync, writers = _writers(monkeypatch)
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    writers.state.update(bootstrap_status=sync.BOOTSTRAP_COMPLETE,
                         last_modified_watermark=start)
    start_ms = int(start.timestamp() * 1000)
    search = _HubSpotLikeSearch([start_ms + 60_000 + i for i in range(150)])

    def withdrawing_iterator(since, max_pages=None):
        for rows, meta in hp.iter_contacts_modified_since(
                since, max_pages=max_pages, client=_client(search)):
            # The deploy's migration lands while this run is mid-scan.
            writers.state["bootstrap_status"] = sync.BOOTSTRAP_PARTIAL
            yield rows, meta

    result = sync.run_contact_funnel_sync(
        mode=sync.MODE_INCREMENTAL, page_iterator=withdrawing_iterator,
        overlap_minutes=0, now=datetime(2026, 11, 16, tzinfo=timezone.utc))
    assert result["status"] == "success"
    assert writers.state["bootstrap_status"] == sync.BOOTSTRAP_PARTIAL
    assert all("bootstrap_status" not in u for u in writers.state_updates)


def test_15_a_contact_with_an_unparseable_timestamp_is_still_read():
    search = _HubSpotLikeSearch([BASE_MS + i for i in range(30)])
    search.rows.append((BASE_MS + 5, "c999"))
    real = search.do_search

    def garbling(public_object_search_request=None):
        resp = real(public_object_search_request)
        for obj in resp.results:
            if obj.to_dict()["id"] == "c999":
                obj.to_dict()["properties"]["lastmodifieddate"] = "not-a-date"
        return resp
    search.do_search = garbling
    ids, metas = _scan(search)
    assert "c999" in ids and metas[-1]["complete"] is True


def test_16_a_failed_rewind_is_reported_not_silent(monkeypatch):
    sync, writers = _writers(monkeypatch)
    search = _HubSpotLikeSearch([BASE_MS + 7 * i for i in range(300)],
                                hidden={f"c{i}" for i in range(10, 40)})
    real_update = writers.update_contact_funnel_sync_state

    def failing_final_write(scope="contacts", **fields):
        if fields.get("last_status") == "failed":
            writers.state_updates.append(dict(fields))
            return False
        return real_update(scope, **fields)

    writers.update_contact_funnel_sync_state = failing_final_write
    result = sync.run_contact_funnel_sync(
        mode=sync.MODE_BOOTSTRAP, page_iterator=_real_iterator(search),
        restart_from_epoch=True,
        now=datetime(2026, 11, 16, tzinfo=timezone.utc))
    assert result["status"] == "failed"
    assert result.get("watermark_rewind_failed") is True


def test_17_a_block_of_unparseable_timestamps_stalls_rather_than_loops():
    """Third review: 100+ contiguous rows whose lastmodifieddate cannot be
    parsed cycled GTE → EQ walk → GT forever. The scan must stop instead."""
    search = _HubSpotLikeSearch([BASE_MS + 1] * 150 + [BASE_MS + 10 + i
                                                       for i in range(20)])
    real = search.do_search
    garbled = {f"c{i}" for i in range(150)}

    def garbling(public_object_search_request=None):
        resp = real(public_object_search_request)
        for obj in resp.results:
            d = obj.to_dict()
            if d["id"] in garbled:
                d["properties"]["lastmodifieddate"] = "not-a-date"
        return resp
    search.do_search = garbling
    with pytest.raises(hp.HubSpotSearchStalledError):
        for page_no, (_rows, _meta) in enumerate(
                hp.iter_contacts_modified_since(0, client=_client(search))):
            assert page_no < 200, "the scan looped instead of stalling"


def test_18_an_empty_first_tie_walk_page_stalls_rather_than_skipping():
    """Third review: an EQ query that matches nothing right after a full page
    at that timestamp proves HubSpot did not honour it; resuming with GT would
    skip the rest of the group."""
    class _EqBlind(_HubSpotLikeSearch):
        def do_search(self, public_object_search_request=None):
            f = public_object_search_request["filterGroups"][0]["filters"]
            if any(x["operator"] == "EQ" for x in f):
                return _Resp([], None, 0)
            return super().do_search(public_object_search_request)

    search = _EqBlind([BASE_MS] * 250 + [BASE_MS + 1 + i for i in range(10)])
    with pytest.raises(hp.HubSpotSearchStalledError):
        _scan(search)
