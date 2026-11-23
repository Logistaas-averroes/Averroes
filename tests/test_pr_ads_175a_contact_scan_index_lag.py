"""PR-ADS-175A — a row HubSpot returns out of its sort position moves nothing.

Production, 2026-11-23 06:00 UTC (``runs`` 342, ``sync_batches`` 2744): the
incremental contact scan from ``lastmodifieddate >= 2026-11-22T05:41:08.617Z``
failed after 8 s: "contact scan ended after 813 unique contacts but HubSpot
counted 965 at its start". The guard was right; the paging was wrong.

HubSpot's search filters and sorts on its SEARCH INDEX, then returns each
record's CURRENT properties. Contact 935470478561 was modified at
05:57:02.287 (an email event), 2 min 58 s before the scan. At 06:00:07 the
index still held its previous value, 2026-11-22T21:42:49.482Z, so the ninth
query (``>= 21:17:12.511``) returned it as row 16 of 100 — carrying
``lastmodifieddate = 2026-11-23T05:57:02.287``. ``_keyset_scan`` moved the
watermark to that value on that one row, discarded rows 17-100 of the page as
"already read" (their timestamps were now below the watermark), and sent the
next query ``>= 05:57:02.287``: four rows, a short page, the end. 152 contacts
modified between 22 Nov 21:42 and 23 Nov 05:57 were never read. Stored rows
of batch 2744 show it: page 9 holds 15 contacts from 21:18-21:28 and then
935470478561; page 10 holds four contacts from 05:58-05:59.

The fake below separates the two values the way HubSpot does: filters, sort
and ``total`` read ``indexed``; results carry ``current``. A modification can
reach the index several requests late (or never, within one scan).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from connectors import hubspot_pull as hp

S = 1_795_326_068_617          # 2026-11-22T05:41:08.617Z, the scan's `since`
NOW = 1_795_413_600_601        # 2026-11-23T06:00:00.601Z, the scan's start


class _Obj:
    def __init__(self, payload):
        self._payload = payload

    def to_dict(self):
        return self._payload


class _Resp:
    def __init__(self, results, total):
        self.results = results
        self.paging = None
        self.total = total


class _LaggingIndexSearch:
    """HubSpot's contact search, with a search index that can lag the record.

    Honours ``lastmodifieddate`` GTE / GT / EQ and ``hs_object_id`` GT,
    sorts by the requested property (ties in a different order on every
    request), reports ``total`` — all over ``indexed``. Every result carries
    ``current``. ``modify(cid, ms, lag)`` changes ``current`` now and
    ``indexed`` only after ``lag`` more requests (``None``: not within this
    scan). ``mutate(fake)`` runs before every request.
    """

    def __init__(self, stamps, *, mutate=None):
        self.current = {f"c{i}": ms for i, ms in enumerate(stamps)}
        self.indexed = dict(self.current)
        self.mutate = mutate
        self.pending = []            # [requests_left, cid, ms]
        self.calls = []
        self._rng = __import__("random").Random(175)

    def modify(self, cid, ms, lag=None):
        self.current[cid] = ms
        if lag == 0:
            self.indexed[cid] = ms
        elif lag is not None:
            self.pending.append([lag, cid, ms])

    @staticmethod
    def _num(cid):
        return int("".join(ch for ch in cid if ch.isdigit()))

    def do_search(self, public_object_search_request=None):
        req = public_object_search_request
        if self.mutate:
            self.mutate(self)
        for p in self.pending:
            p[0] -= 1
        for p in [p for p in self.pending if p[0] <= 0]:
            self.indexed[p[1]] = p[2]
            self.pending.remove(p)
        assert not req.get("after"), "keyset paging never sends an offset"
        rows = list(self.indexed.items())
        for f in req["filterGroups"][0]["filters"]:
            prop, op, val = f["propertyName"], f["operator"], int(f["value"])
            if prop == "lastmodifieddate":
                keep = {"GTE": lambda ms: ms >= val, "GT": lambda ms: ms > val,
                        "EQ": lambda ms: ms == val}[op]
                rows = [r for r in rows if keep(r[1])]
            elif prop == "hs_object_id":
                assert op == "GT"
                rows = [r for r in rows if self._num(r[0]) > val]
            else:
                raise AssertionError(f"unexpected filter {prop}")
        if req["sorts"][0]["propertyName"] == "hs_object_id":
            rows.sort(key=lambda r: self._num(r[0]))
        else:
            self._rng.shuffle(rows)
            rows.sort(key=lambda r: r[1])        # stable: ties stay shuffled
        self.calls.append(req["filterGroups"][0]["filters"])
        page = rows[:int(req["limit"])]
        return _Resp([_Obj({"id": cid, "properties": {
            "lastmodifieddate": str(self.current[cid])}})
            for cid, _ms in page], len(rows))


def _client(search):
    c = type("C", (), {})()
    c.crm = type("R", (), {})()
    c.crm.contacts = type("K", (), {})()
    c.crm.contacts.search_api = search
    return c


def _scan(search, since=S):
    ids, metas = [], []
    for rows, meta in hp.iter_contacts_modified_since(since,
                                                      client=_client(search)):
        metas.append(meta)
        ids.extend(r["id"] for r in rows)
    return ids, metas


def _production_day():
    """The 22-23 Nov shape: a few contacts after `since`, a bulk update of
    ~600 contacts over four minutes (pairs sharing a millisecond), a 250-row
    group on one millisecond (larger than a page), a day and a night of
    scattered edits, and four contacts in the minutes before 06:00."""
    stamps = [S + 600_000 + 37_000 * i for i in range(15)]
    bulk0 = S + 2 * 3600_000 + 25 * 60_000            # ~08:06
    stamps += [bulk0 + 400 * (i // 2) for i in range(600)]
    stamps += [bulk0 + 3600_000] * 250                # one-millisecond group
    stamps += [bulk0 + 4 * 3600_000 + 211_000 * i for i in range(150)]
    stamps += [NOW - 104_000, NOW - 73_000, NOW - 69_000, NOW - 3_000]
    return stamps


def test_01_a_contact_modified_minutes_before_the_scan_skips_no_one():
    """The production failure, replayed. One contact in the night block was
    modified three minutes before the scan; the index still sorts it at its
    old time, the response carries the new one. Every contact HubSpot counted
    must be read, and the scan must complete."""
    stamps = _production_day()
    search = _LaggingIndexSearch(stamps)
    lagged = f"c{15 + 600 + 250 + 120}"           # deep in the night block
    search.modify(lagged, NOW - 178_000, lag=None)
    ids, metas = _scan(search)
    assert set(ids) == set(search.current), (
        f"{len(set(search.current) - set(ids))} contacts never read")
    assert metas[-1]["complete"] is True
    assert metas[-1]["source_total"] == len(stamps)


def test_02_a_lagged_row_at_the_end_of_a_page_moves_nothing():
    """The last row of a page is the one the next query starts from. One
    lagged row there must not carry the watermark past the unread rows."""
    stamps = [S + 1_000 * i for i in range(450)]
    search = _LaggingIndexSearch(stamps)
    search.modify("c99", NOW, lag=None)            # row 100 of page 1
    search.modify("c297", NOW - 5, lag=None)       # the end of a later page
    ids, metas = _scan(search)
    assert set(ids) == set(search.current)
    assert metas[-1]["complete"] is True


def test_03_a_lagged_member_of_a_tie_group_does_not_end_the_walk_early():
    """Inside the id walk of a one-millisecond group, a member whose current
    value is newer moved the watermark to it; the scan then resumed AFTER that
    value and skipped every contact in between."""
    t = S + 3600_000
    stamps = ([S + 7 * i for i in range(60)] + [t] * 260
              + [t + 1_000 + 900 * i for i in range(300)])
    search = _LaggingIndexSearch(stamps)
    search.modify("c130", NOW - 60_000, lag=None)  # in the tie group
    ids, metas = _scan(search)
    assert set(ids) == set(search.current), (
        f"{len(set(search.current) - set(ids))} contacts never read")
    assert metas[-1]["complete"] is True
    assert any(any(f["propertyName"] == "hs_object_id" for f in q)
               for q in search.calls), "the group was not walked by id"


def test_04_contacts_modified_during_the_scan_with_a_lagging_index():
    """Data moves while the scan runs, and the index learns of it late:
    before every request one contact the scan has NOT yet read is modified to
    the newest time, reaching the index only two requests later. Every
    contact is read — at its old index position or its new one — and the
    scan completes."""
    stamps = _production_day()
    state = {"k": 0}

    def mutate(fake):
        unread = sorted((ms, cid) for cid, ms in fake.indexed.items()
                        if cid not in read)
        if len(unread) > 20:
            _ms, cid = unread[len(unread) // 2]
            newest = max(fake.current.values())
            fake.modify(cid, newest + 1, lag=2)
            state["k"] += 1

    read = set()
    search = _LaggingIndexSearch(stamps, mutate=mutate)
    complete = False
    for rows, meta in hp.iter_contacts_modified_since(
            S, client=_client(search)):
        read.update(r["id"] for r in rows)
        complete = complete or bool(meta.get("complete"))
    assert state["k"] > 5
    assert read == set(search.current), (
        f"{len(set(search.current) - read)} contacts never read")
    assert complete


def test_05_two_lagged_rows_ending_one_page_fail_closed_never_complete():
    """The residual case: if BOTH rows the next query starts from are lagged,
    nothing in the response says where the page really ended. The scan may
    then miss contacts — and `_assert_scan_complete` must still refuse to call
    it complete. The run is `partial`, the watermark rewinds, the next run
    retries once the index has caught up."""
    stamps = [S + 1_000 * i for i in range(450)]
    search = _LaggingIndexSearch(stamps)
    search.modify("c98", NOW - 1, lag=None)
    search.modify("c99", NOW, lag=None)
    with pytest.raises(hp.HubSpotSearchIncompleteError):
        _scan(search)


def test_06_a_checkpoint_never_passes_a_contact_the_scan_has_not_read(
        monkeypatch):
    """A run that stops mid-scan (a capped run here; a killed worker in
    production) resumes from its last checkpoint. The checkpoint was the
    newest timestamp on the page — the lagged contact's — so the next run
    started past every unread contact behind it, and its own `total` (counted
    from that later start) could not see the gap."""
    from services import hubspot_contact_funnel_sync_service as sync
    from tests.test_pr_ads_153b_canonical_funnel import _FakeWriters
    writers = _FakeWriters()
    monkeypatch.setattr(sync, "db_writers", writers)
    start = datetime.fromtimestamp(S / 1000, tz=timezone.utc)
    writers.state.update(bootstrap_status=sync.BOOTSTRAP_COMPLETE,
                         last_modified_watermark=start)
    stamps = [S + 60_000 + 1_000 * i for i in range(400)]
    search = _LaggingIndexSearch(stamps)
    search.modify("c40", NOW - 178_000, lag=None)  # row 41 of page 1

    def iterator(since, max_pages=None):
        return hp.iter_contacts_modified_since(
            since, max_pages=max_pages, client=_client(search))

    first = sync.run_contact_funnel_sync(
        mode=sync.MODE_INCREMENTAL, page_iterator=iterator, overlap_minutes=0,
        max_pages=1, now=datetime(2026, 11, 23, 6, tzinfo=timezone.utc))
    assert first["status"] == "partial"
    checkpoint = writers.state["last_modified_watermark"]
    first_unread_ms = S + 60_000 + 1_000 * 100
    assert checkpoint.timestamp() * 1000 <= first_unread_ms, (
        f"checkpoint {checkpoint} is past unread contacts")

    second = sync.run_contact_funnel_sync(
        mode=sync.MODE_INCREMENTAL, page_iterator=iterator, overlap_minutes=0,
        now=datetime(2026, 11, 23, 6, 5, tzinfo=timezone.utc))
    assert second["status"] == "success"
    written = {r["contact_id"] for page in writers.upserts for r in page}
    assert written >= set(search.current), (
        f"{len(set(search.current) - written)} contacts never written")
