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
    """Filter-honouring, offset-paged, sorted, 10k-capped — like HubSpot."""

    def __init__(self, modified_ms, *, report_total=True, hidden=(),
                 mutate=None):
        self.rows = sorted(((ms, f"c{i}") for i, ms in enumerate(modified_ms)))
        self.report_total = report_total
        # Counted in HubSpot's total but never returned: pages lost "for any
        # reason", to prove the completeness guard independently of paging.
        self.hidden = set(hidden)
        # Called before every request with this fake: lets a test modify,
        # delete or create contacts WHILE the scan runs, as production does.
        self.mutate = mutate
        self.calls = []

    def do_search(self, public_object_search_request=None):
        req = public_object_search_request or {}
        f = req["filterGroups"][0]["filters"][0]
        assert f["propertyName"] == "lastmodifieddate" and f["operator"] == "GTE"
        floor = int(f["value"])
        if self.mutate:
            self.mutate(self)
        self.rows.sort()
        matching = [(ms, cid) for ms, cid in self.rows if ms >= floor]
        total = len(matching)
        matching = [(ms, cid) for ms, cid in matching if cid not in self.hidden]
        offset = int(req.get("after") or 0)
        limit = int(req["limit"])
        self.calls.append((floor, offset))
        if offset >= hp.HUBSPOT_SEARCH_RESULT_CAP:
            raise AssertionError("HubSpot refuses to page past 10,000 results")
        page = matching[offset:offset + limit]
        nxt = offset + limit
        # Like HubSpot: a next cursor is offered while results remain, even at
        # the 10,000 boundary — requesting it is what fails (above).
        after = str(nxt) if nxt < len(matching) else None
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


def test_02_every_page_is_a_keyset_query_from_the_watermark():
    """No offset over a live result set: with distinct timestamps every
    request starts at offset 0 and the filter advances with the watermark."""
    search = _HubSpotLikeSearch([BASE_MS + i for i in range(950)])
    _scan(search)
    assert {offset for _floor, offset in search.calls} == {0}
    floors = [floor for floor, _offset in search.calls]
    assert floors == sorted(floors) and len(set(floors)) == len(floors)


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


def test_09_a_tie_group_larger_than_a_page_is_walked_not_skipped():
    """Production has hundreds of contacts sharing one bulk-update second."""
    ms = [BASE_MS] * 350 + [BASE_MS + 1000 + i for i in range(120)]
    search = _HubSpotLikeSearch(ms)
    ids, metas = _scan(search)
    assert len(set(ids)) == 470
    assert metas[-1]["complete"] is True


def test_10_more_than_10000_contacts_on_one_timestamp_stall_never_complete():
    search = _HubSpotLikeSearch([BASE_MS] * (hp.HUBSPOT_SEARCH_RESULT_CAP + 50))
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
