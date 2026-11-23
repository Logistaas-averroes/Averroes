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
    must be read, and the scan must complete. The lagged row sits mid-page
    with unread contacts on later pages, as in production: this pins "never
    move the watermark to the newest row" and "never discard a returned row".
    The older-of-the-last-two rule is pinned by `test_02`."""
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
    lagged row there must not carry the watermark past the unread rows. This
    is the test that pins the older-of-the-last-two rule: a "last row"
    rule passes `test_01` and fails here."""
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


def test_05_two_lagged_rows_ending_one_page_fail_closed_and_rewind(
        monkeypatch):
    """The residual case: if BOTH rows the next query starts from are lagged,
    nothing in the response says where the page really ended, and the scan
    skips contacts. When the shortfall exceeds the contact slack (1% or 2),
    `_assert_scan_complete` refuses it: the service run is `failed`, the
    watermark rewinds to where the run began, and the next run retries once
    the index has caught up. (Within the slack it can still complete — the
    documented remaining gap, docs/09 PR-ADS-175A.)"""
    stamps = [S + 1_000 * i for i in range(450)]
    search = _LaggingIndexSearch(stamps)
    search.modify("c98", NOW - 1, lag=None)
    search.modify("c99", NOW, lag=None)
    with pytest.raises(hp.HubSpotSearchIncompleteError):
        _scan(search)

    from services import hubspot_contact_funnel_sync_service as sync
    from tests.test_pr_ads_153b_canonical_funnel import _FakeWriters
    writers = _FakeWriters()
    monkeypatch.setattr(sync, "db_writers", writers)
    start = datetime.fromtimestamp((S - 1) / 1000, tz=timezone.utc)
    writers.state.update(bootstrap_status=sync.BOOTSTRAP_COMPLETE,
                         last_modified_watermark=start)

    def iterator(since, max_pages=None):
        return hp.iter_contacts_modified_since(
            since, max_pages=max_pages, client=_client(search))

    result = sync.run_contact_funnel_sync(
        mode=sync.MODE_INCREMENTAL, page_iterator=iterator, overlap_minutes=0,
        now=datetime(2026, 11, 23, 6, tzinfo=timezone.utc))
    assert result["status"] == "failed"
    assert "INCOMPLETE" in (writers.state.get("last_error") or "")
    assert writers.state["last_modified_watermark"] == start
    assert writers.state["last_incremental_status"] == "failed"


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


# ─────────────────────────────────────────────────────────────────────────────
# The deal ledger reads through the same `_keyset_scan`. With the scan fixed,
# a read containing an out-of-place deal COMPLETES (before, it ended short of
# `total` and was `gap_proven`), and a capped or cut-short run then reached a
# checkpoint that assumed the read was ascending: the newest timestamp of its
# clean prefix — the out-of-place deal's.
# ─────────────────────────────────────────────────────────────────────────────
from tests.test_pr_ads_166b_deal_keyset_paging import ledger  # noqa: E402,F401

DEAL_BASE = 1_795_300_000_000          # 2026-11-21T22:26:40Z
MIN = 60_000


def _deal_iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


class _LaggingDealSearch(_LaggingIndexSearch):
    """`_LaggingIndexSearch` for deals: filters on ``hs_lastmodifieddate``,
    returns ISO text and the ledger's properties. ``fail_from`` raises the
    given exception on that request and every later one."""

    def __init__(self, n, *, fail_from=None):
        self.current = {str(5000 + i): DEAL_BASE + i * MIN for i in range(n)}
        self.indexed = dict(self.current)
        self.mutate = None
        self.pending = []
        self.calls = []
        self.fail_from = fail_from
        self._rng = __import__("random").Random(175)

    def do_search(self, public_object_search_request=None, **_kw):
        req = public_object_search_request
        if self.fail_from and len(self.calls) + 1 >= self.fail_from[0]:
            self.calls.append(None)
            raise self.fail_from[1]
        assert not req.get("after"), "keyset paging never sends an offset"
        rows = list(self.indexed.items())
        for f in req["filterGroups"][0]["filters"]:
            prop, op = f["propertyName"], f["operator"]
            if prop == "hs_lastmodifieddate":
                val = int(f["value"])
                keep = {"GTE": lambda ms: ms >= val, "GT": lambda ms: ms > val,
                        "EQ": lambda ms: ms == val}[op]
                rows = [r for r in rows if keep(r[1])]
            elif prop == "hs_object_id":
                rows = [r for r in rows if int(r[0]) > int(f["value"])]
            else:
                raise AssertionError(f"unexpected filter {prop}")
        if req["sorts"][0]["propertyName"] == "hs_object_id":
            rows.sort(key=lambda r: int(r[0]))
        else:
            self._rng.shuffle(rows)
            rows.sort(key=lambda r: r[1])
        self.calls.append(req["filterGroups"][0]["filters"])
        page = rows[:int(req["limit"])]
        return _Resp([_Obj({"id": did, "properties": {
            "hs_lastmodifieddate": _deal_iso(self.current[did]),
            "dealname": f"deal {did}", "dealstage": "326093516",
            "hs_is_closed_won": "true", "hs_is_closed": "true",
            "amount": "100", "deal_currency_code": "USD"}})
            for did, _ms in page], len(rows))


def _install_deals(monkeypatch, fake):
    client = type("C", (), {})()
    client.crm = type("R", (), {})()
    client.crm.deals = type("K", (), {})()
    client.crm.deals.search_api = fake
    monkeypatch.setattr(hp, "get_client", lambda: client)
    monkeypatch.setattr(hp.time, "sleep", lambda *_: None)


def _ckpt_ms(text):
    return int(datetime.fromisoformat(
        str(text).replace("Z", "+00:00")).timestamp() * 1000)


def test_07_a_capped_deal_run_never_checkpoints_past_an_unprocessed_deal(
        monkeypatch, ledger):  # noqa: F811
    """Deal 5010 was modified minutes before the read; the index still sorts
    it at 22:36:40 and the response carries 04:26:40 next day. With the cap
    at 50 the clean prefix is deals 5000-5049 and its newest timestamp is
    5010's — 250 deals behind it. Before the fix the checkpoint landed there
    and run 2 (a `success`, `complete`) read none of them."""
    fake = _LaggingDealSearch(300)
    fake.current["5010"] = DEAL_BASE + 30 * 60 * MIN
    _install_deals(monkeypatch, fake)
    svc = ledger["svc"]

    first = svc.sync_deals(batch_id=1, max_association_lookups=50)
    assert first["status"] == "partial"
    assert first["watermark_is_checkpoint"] is True
    first_unprocessed_ms = DEAL_BASE + 50 * MIN          # deal 5050
    assert _ckpt_ms(ledger["stored"]["watermark"]) <= first_unprocessed_ms, (
        f"checkpoint {ledger['stored']['watermark']} is past deal 5050")

    second = svc.sync_deals(batch_id=2, max_association_lookups=10_000)
    assert second["status"] == "success" and second["complete"] is True
    written = {r["deal_id"] for r in ledger["written"]}
    assert written == set(fake.current), (
        f"{len(set(fake.current) - written)} deals never written")


def test_08_a_deal_read_cut_short_checkpoints_at_its_keyset_position(
        monkeypatch, ledger):  # noqa: F811
    """The out-of-place deal is on page 1; a 429 that never clears stops the
    read on page 3. Every deal it processed was clean, so the old checkpoint
    was the prefix's newest timestamp — the out-of-place deal's, past every
    deal on pages 3+. The read's keyset position bounds it now."""
    fake = _LaggingDealSearch(
        300, fail_from=(3, hp.HUBSPOT_API_EXCEPTIONS[0](
            status=429, reason="Too Many Requests")))
    fake.current["5010"] = DEAL_BASE + 30 * 60 * MIN
    _install_deals(monkeypatch, fake)
    svc = ledger["svc"]

    first = svc.sync_deals(batch_id=1)
    assert first["status"] == "partial" and first["complete"] is False
    written = {r["deal_id"] for r in ledger["written"]}
    unread = set(fake.current) - written
    assert unread, "the read was meant to stop early"
    first_unread_ms = min(fake.indexed[d] for d in unread)
    assert first["watermark_is_checkpoint"] is True
    assert _ckpt_ms(ledger["stored"]["watermark"]) <= first_unread_ms, (
        f"checkpoint {ledger['stored']['watermark']} is past unread deals")

    fake.fail_from = None
    second = svc.sync_deals(batch_id=2)
    assert second["status"] == "success"
    written = {r["deal_id"] for r in ledger["written"]}
    assert written == set(fake.current), (
        f"{len(set(fake.current) - written)} deals never written")


def test_09_the_backfill_never_proves_a_bootstrap_complete_over_a_gap(
        monkeypatch, ledger):  # noqa: F811
    """`scripts/backfill_canonical_deal_ledger.py` drives capped passes, each
    resuming from the last checkpoint, until one proves completion — and that
    pass completes the bootstrap. A checkpoint past unprocessed deals made the
    next pass `success`/`complete` over a window that held none of them:
    `completes_bootstrap` fired with 250 deals never written."""
    import db.connection as connection
    import db.deal_ledger_repository as repo
    from scripts import backfill_canonical_deal_ledger as cli

    fake = _LaggingDealSearch(300)
    fake.current["5010"] = DEAL_BASE + 30 * 60 * MIN
    _install_deals(monkeypatch, fake)
    monkeypatch.setattr(connection, "init_pool", lambda: None)

    completions = []
    real_record = repo.record_sync_state

    def recording(**kw):
        out = real_record(**kw)
        if ledger["state"] and ledger["state"][-1].get("completes"):
            completions.append({r["deal_id"] for r in ledger["written"]})
        return out
    monkeypatch.setattr(repo, "record_sync_state", recording)

    out = cli.run(max_passes=40, max_association_lookups=50, restart=False)

    assert out["pass_proved_complete"] is True, out["reason"]
    assert completions, "no pass completed the bootstrap"
    for written in completions:
        assert written == set(fake.current), (
            f"the bootstrap was completed with "
            f"{len(set(fake.current) - written)} deals never written")
