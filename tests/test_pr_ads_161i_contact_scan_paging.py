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

    def __init__(self, modified_ms, *, report_total=True):
        self.rows = sorted(((ms, f"c{i}") for i, ms in enumerate(modified_ms)))
        self.report_total = report_total
        self.calls = []

    def do_search(self, public_object_search_request=None):
        req = public_object_search_request or {}
        f = req["filterGroups"][0]["filters"][0]
        assert f["propertyName"] == "lastmodifieddate" and f["operator"] == "GTE"
        floor = int(f["value"])
        matching = [(ms, cid) for ms, cid in self.rows if ms >= floor]
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
            len(matching) if self.report_total else None)


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


def test_02_one_paged_query_keeps_one_filter_value():
    search = _HubSpotLikeSearch([BASE_MS + i for i in range(950)])
    _scan(search)
    floors = {floor for floor, _offset in search.calls}
    assert floors == {0}, "the filter moved while paging one result set"


def test_03_a_scan_past_10000_re_anchors_and_still_reads_everything():
    n = hp.HUBSPOT_SEARCH_RESULT_CAP + 2_345
    search = _HubSpotLikeSearch([BASE_MS + 3 * i for i in range(n)])
    ids, metas = _scan(search)
    assert len(set(ids)) == n
    assert metas[-1]["complete"] is True
    assert metas[-1]["reanchored"] >= 1


def test_04_an_incremental_scan_from_a_watermark_reads_all_newer_contacts():
    search = _HubSpotLikeSearch([BASE_MS + 10 * i for i in range(1200)])
    since_ms = BASE_MS + 10 * 400            # contacts 400..1199 qualify
    ids, metas = _scan(search, since=since_ms)
    assert len(set(ids)) == 800
    assert metas[-1]["source_total"] == 800


def test_05_a_scan_that_ends_short_of_hubspots_total_raises_not_completes():
    """The guard, independent of the paging fix: if pages go missing for ANY
    reason, the scan must not end with a completion sentinel."""
    class _Skipping(_HubSpotLikeSearch):
        def do_search(self, public_object_search_request=None):
            req = dict(public_object_search_request)
            if req.get("after"):                    # silently skip a page
                req["after"] = str(int(req["after"]) + 100)
            return super().do_search(req)

    search = _Skipping([BASE_MS + 7 * i for i in range(1000)])
    with pytest.raises(hp.HubSpotSearchIncompleteError):
        _scan(search)


def test_06_a_response_without_hubspots_total_cannot_prove_completeness():
    search = _HubSpotLikeSearch([BASE_MS + i for i in range(50)],
                                report_total=False)
    with pytest.raises(hp.HubSpotSearchIncompleteError):
        _scan(search)


def test_07_a_small_shortfall_from_deletions_during_the_scan_is_tolerated():
    """Contacts merged or deleted mid-scan drop out; 1% / 5 contacts slack."""
    assert hp._assert_scan_complete(1000, {f"c{i}" for i in range(990)}) is None
    with pytest.raises(hp.HubSpotSearchIncompleteError):
        hp._assert_scan_complete(1000, {f"c{i}" for i in range(989)})
    assert hp._assert_scan_complete(100, {f"c{i}" for i in range(95)}) is None
    with pytest.raises(hp.HubSpotSearchIncompleteError):
        hp._assert_scan_complete(100, {f"c{i}" for i in range(94)})


def test_08_an_incomplete_scan_leaves_a_bootstrap_partial_not_complete(
        monkeypatch):
    """End to end through the sync service: the guard's exception is a failed
    run, so the bootstrap stays partial and the next run resumes it."""
    from services import hubspot_contact_funnel_sync_service as sync
    from tests.test_pr_ads_153b_canonical_funnel import _FakeWriters

    writers = _FakeWriters()
    monkeypatch.setattr(sync, "db_writers", writers)

    def iterator(since, max_pages=None):
        yield [{"id": "1", "properties": {
            "lastmodifieddate": "2026-07-01T00:00:00Z"}}], {
            "watermark_ms": 0, "complete": False}
        hp._assert_scan_complete(21751, {"1"})

    result = sync.run_contact_funnel_sync(
        mode=sync.MODE_BOOTSTRAP, page_iterator=iterator,
        restart_from_epoch=True,
        now=datetime(2026, 11, 16, tzinfo=timezone.utc))
    assert result["status"] == "failed"
    assert writers.state["bootstrap_status"] == sync.BOOTSTRAP_PARTIAL
    assert "INCOMPLETE" in (writers.state.get("last_error") or "")
