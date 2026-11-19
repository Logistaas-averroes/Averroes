"""PR-ADS-163C — contacts deleted or merged away in HubSpot leave the funnel.

Production, 2026-11-18: ``hubspot_contact_funnel`` held 30 contacts that no
longer exist in HubSpot (564 contacts created in October against HubSpot's
557). The contact sync is modified-since, and a deleted or merged contact is
never "modified": it stops being returned and nothing says so.

The fake HubSpot below serves BOTH searches the code sends — the sync's
``lastmodifieddate`` keyset scan and the existence check's ``hs_object_id IN``
— from one set of contacts that the tests change between runs. It honours the
filters, reports ``total``, and any call that is not a search is recorded and
refused, so a write could not pass unnoticed.

Every database case runs the real schema, the real funnel writer and the real
readers on a throwaway PostgreSQL cluster.
"""
from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from hubspot.crm.contacts import ApiException

from connectors import hubspot_pull as hp
from tests import test_pr_ads_153b_pg_integration as pgmod

ROOT = Path(__file__).resolve().parent.parent

#: The morning the defect was measured.
DAY_1 = datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)
HOURS = timedelta(hours=1)
BASE_MS = 1_790_000_000_000          # 2026-09-21T14:13:20Z
FIRST_ID = 9_000_001

SEARCH = "crm.contacts.search_api.do_search"
GET = "crm.contacts.basic_api.get_by_id"


# ═════════════════════════════════════════════════════════════════════════════
# A HubSpot that answers searches and refuses everything else
# ═════════════════════════════════════════════════════════════════════════════
class _Obj:
    def __init__(self, payload):
        self._payload = payload
        self.id = payload.get("id")

    def to_dict(self):
        return self._payload


class _Resp:
    def __init__(self, results, total, paging=None):
        self.results = results
        self.total = total
        self.paging = paging


class _Trap:
    """Every attribute is another trap; calling one is a forbidden HubSpot
    call, recorded before it is refused."""

    def __init__(self, fake, path):
        self._fake, self._path = fake, path

    def __getattr__(self, name):
        return _Trap(self._fake, f"{self._path}.{name}")

    def __call__(self, *args, **kwargs):
        self._fake.calls.append(self._path)
        raise AssertionError(f"forbidden HubSpot call: {self._path}")


class _SearchApi(_Trap):
    def __init__(self, fake):
        super().__init__(fake, "crm.contacts.search_api")

    def do_search(self, public_object_search_request=None, **kwargs):
        return self._fake.search(public_object_search_request or {}, kwargs)


class _BasicApi(_Trap):
    """Only the read by id exists; create / update / archive stay traps."""

    def __init__(self, fake):
        super().__init__(fake, "crm.contacts.basic_api")

    def get_by_id(self, contact_id=None, properties=None, **kwargs):
        return self._fake.direct_read(str(contact_id), properties, kwargs)


class _Contacts(_Trap):
    def __init__(self, fake):
        super().__init__(fake, "crm.contacts")
        self.search_api = _SearchApi(fake)
        self.basic_api = _BasicApi(fake)


class _Crm(_Trap):
    def __init__(self, fake):
        super().__init__(fake, "crm")
        self.contacts = _Contacts(fake)


class _Client(_Trap):
    def __init__(self, fake):
        super().__init__(fake, "client")
        self.crm = _Crm(fake)


class _FakeHubSpot:
    """Behaves like HubSpot's contact search for the two queries the code sends.

    ``lastmodifieddate`` GTE / GT / EQ and ``hs_object_id`` GT with a sort (the
    sync's keyset scan), and ``hs_object_id IN`` (the existence check). Both
    report ``total``. Tests change the data between runs with ``delete`` /
    ``restore``; ``faults`` makes the existence batch that contains a given id
    answer badly, for as long as the fault is set.
    """

    def __init__(self, contacts):
        self.contacts = {c["id"]: c for c in contacts}
        self.deleted: dict = {}
        self.calls: list = []                # the path of every call made
        self.existence_requests: list = []   # (request, kwargs) per IN search
        self.faults: dict = {}               # contact id -> fault mode
        self.fault_all: str | None = None
        #: Live contacts a DEGRADED search index leaves out of otherwise
        #: well-formed answers (``total`` still equals the results).
        self.omitted: set = set()
        #: Called with (ids asked, index of this IN search); may raise, and
        #: returns the ids to answer for.
        self.existence_hook = None
        self.direct_requests: list = []      # (id, properties, kwargs)
        self.direct_faults: dict = {}        # contact id -> fault mode
        #: merged-away id -> (survivor id, does the survivor name it?)
        self.merged: dict = {}
        self.client = _Client(self)

    # ── the data moving under the code ──────────────────────────────────────
    def delete(self, *ids):
        for cid in ids:
            self.deleted[str(cid)] = self.contacts.pop(str(cid))

    def restore(self, cid, *, modified_ms):
        contact = self.deleted.pop(str(cid))
        contact["properties"]["lastmodifieddate"] = str(modified_ms)
        self.contacts[str(cid)] = contact

    def merge(self, loser, into, *, survivor_names_it=True):
        """``loser`` is merged into ``into``: it is gone from every search,
        and a read of its id returns the survivor."""
        self.delete(loser)
        self.merged[str(loser)] = (str(into), survivor_names_it)

    # ── the read by id: the object itself, not the search index ─────────────
    def direct_read(self, contact_id, properties, kwargs):
        self.calls.append(GET)
        self.direct_requests.append((contact_id, properties, kwargs))
        fault = self.direct_faults.get(contact_id)
        if fault == "error":
            raise ApiException(status=500, reason="Internal Server Error")
        if fault == "rate_limited":
            raise ApiException(status=429, reason="Too Many Requests")
        if fault == "no_id":
            return _Obj({"properties": {}})
        if contact_id in self.contacts:
            # `omitted` is the SEARCH INDEX's fault; the object is still here.
            return _Obj({"id": contact_id,
                         "properties": {"hs_object_id": contact_id}})
        if contact_id in self.merged:
            survivor, names_it = self.merged[contact_id]
            return _Obj({"id": survivor, "properties": {
                "hs_object_id": survivor,
                "hs_merged_object_ids":
                    f"555;{contact_id};777" if names_it else "555;777"}})
        raise ApiException(status=404, reason="Not Found")

    # ── the search endpoint ─────────────────────────────────────────────────
    def search(self, req, kwargs):
        self.calls.append(SEARCH)
        filters = req["filterGroups"][0]["filters"]
        if any(f["operator"] == "IN" for f in filters):
            return self._existence(req, kwargs)
        rows = list(self.contacts.values())
        for f in filters:
            prop, op, val = f["propertyName"], f["operator"], int(f["value"])
            if prop == "lastmodifieddate":
                keep = {"GTE": lambda ms: ms >= val, "GT": lambda ms: ms > val,
                        "EQ": lambda ms: ms == val}[op]
                rows = [c for c in rows
                        if keep(int(c["properties"]["lastmodifieddate"]))]
            elif prop == "hs_object_id" and op == "GT":
                rows = [c for c in rows if int(c["id"]) > val]
            else:
                raise AssertionError(f"unexpected filter {prop} {op}")
        if req["sorts"][0]["propertyName"] == "hs_object_id":
            rows.sort(key=lambda c: int(c["id"]))
        else:
            rows.sort(key=lambda c: int(c["properties"]["lastmodifieddate"]))
        assert not req.get("after"), "the scan never pages by offset"
        return _Resp([_Obj(c) for c in rows[:int(req["limit"])]], len(rows))

    def _existence(self, req, kwargs):
        self.existence_requests.append((req, kwargs))
        (only,) = req["filterGroups"][0]["filters"]
        assert only["propertyName"] == "hs_object_id" and only["operator"] == "IN"
        # HubSpot reads `hs_object_id` as a NUMBER and answers with the
        # canonical id, whatever spelling it was asked with.
        asked = list(dict.fromkeys(str(int(str(v))) for v in only["values"]))
        mode = self.fault_all or next(
            (self.faults[i] for i in asked if i in self.faults), None)
        if self.existence_hook is not None:
            asked = self.existence_hook(asked, len(self.existence_requests) - 1)
        found = [_Obj({"id": i, "properties": {"hs_object_id": i}})
                 for i in asked if i in self.contacts
                 and i not in self.omitted][:int(req["limit"])]
        if mode == "error":
            raise ApiException(status=500, reason="Internal Server Error")
        if mode == "unauthorized":
            raise ApiException(status=401, reason="Unauthorized")
        if mode == "rate_limited":
            raise ApiException(status=429, reason="Too Many Requests")
        if mode == "no_results":
            return _Resp(None, None)
        if mode == "no_total":
            return _Resp(found, None)
        if mode == "total_above_results":
            return _Resp(found, len(found) + 1)
        if mode == "total_below_results":
            return _Resp(found, len(found) - 1)
        if mode == "foreign_contact":
            return _Resp(found[:-1] + [_Obj({"id": "424242", "properties": {}})],
                         len(found))
        if mode == "duplicate_contact":
            return _Resp(found[:-1] + [found[0]], len(found))
        if mode == "result_without_id":
            return _Resp(found[:-1] + [_Obj({"properties": {}})], len(found))
        if mode == "another_page":
            paging = type("P", (), {"next": type("N", (), {"after": "100"})()})()
            return _Resp(found, len(found), paging)
        if mode == "padded_ids":
            # The same contacts, each id spelled with leading zeros.
            return _Resp([_Obj({"id": "00" + o.id, "properties": {}})
                          for o in found], len(found))
        if mode == "well_formed_but_empty":
            # A usable answer that happens to be wrong: HubSpot says nothing
            # matched. This is the "one odd response" the two strikes are for.
            return _Resp([], 0)
        assert mode is None, mode
        return _Resp(found, len(found))


UNUSABLE = ("error", "unauthorized", "no_results", "no_total",
            "total_above_results", "total_below_results", "foreign_contact",
            "duplicate_contact", "result_without_id", "another_page")


def _contact(n, **props):
    cid = str(FIRST_ID + n)
    base = {
        "lifecyclestage": "lead",
        "mql_status": None,
        "createdate": f"2026-10-{1 + n % 28:02d}T08:00:00Z",
        "lastmodifieddate": str(BASE_MS + n * 60_000),
        "hs_analytics_source": "PAID_SEARCH" if n % 2 == 0 else "ORGANIC_SEARCH",
        "hs_analytics_source_data_1": "Brand - US" if n % 2 == 0 else None,
        "hs_analytics_source_data_2": "tms",
        "company": f"Co {cid}",
    }
    base.update(props)
    return {"id": cid, "properties": base}


def _cid(n) -> str:
    return str(FIRST_ID + n)


def _portal(n=400, special=None):
    """``n`` contacts, all created in October 2026. ``special`` maps an index
    to extra properties (a lifecycle stage, an SQL date …)."""
    special = special or {}
    return _FakeHubSpot([_contact(i, **special.get(i, {})) for i in range(n)])


#: Index 137 is the contact that carries SQL evidence, as one of production's
#: thirty did.
SQL_CONTACT = {137: {"lifecyclestage": "salesqualifiedlead",
                     "hs_v2_date_entered_salesqualifiedlead":
                         "2026-10-20T10:00:00Z"}}


# ═════════════════════════════════════════════════════════════════════════════
# Database fixture: one cluster for the module, one empty database per test
# ═════════════════════════════════════════════════════════════════════════════
needs_pg = pytest.mark.skipif(
    not pgmod._have_postgres(),
    reason="PostgreSQL server binaries / unprivileged postgres user unavailable")


@pytest.fixture(scope="module")
def _cluster():
    cluster = pgmod._PgCluster().start()
    try:
        yield cluster
    finally:
        cluster.stop()


class _Db:
    def __init__(self, url):
        self.url = url

    def connect(self):
        import psycopg2
        return psycopg2.connect(self.url)

    def scalar(self, sql, params=None):
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params) if params else cur.execute(sql)
            row = cur.fetchone()
            return row[0] if row else None

    def rows(self, sql, params=None):
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params) if params else cur.execute(sql)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def execute(self, sql, params=None):
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params) if params else cur.execute(sql)

    def funnel_count(self):
        return self.scalar("SELECT COUNT(*) FROM hubspot_contact_funnel")

    def strikes(self):
        return {r["contact_id"]: r for r in self.rows(
            "SELECT * FROM hubspot_contact_absence")}

    def run_row(self, reconciliation_run_id):
        (row,) = self.rows(
            "SELECT * FROM hubspot_contact_reconciliation_runs WHERE id = %s",
            (reconciliation_run_id,))
        return row


_DB_SEQ = iter(range(1, 10_000))


@pytest.fixture()
def db(_cluster, monkeypatch):
    """A fresh database with the real schema, and ``db.connection`` on it."""
    import psycopg2
    import db.connection as connection

    name = f"t163c_{next(_DB_SEQ)}"
    admin = psycopg2.connect(_cluster.url)
    admin.autocommit = True
    admin.cursor().execute(f"CREATE DATABASE {name}")
    admin.close()
    url = _cluster.url.rsplit("/", 1)[0] + f"/{name}"

    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(connection, "_pool", None, raising=False)
    connection.init_pool()
    from db.schema import init_db
    init_db()
    # The pacing sleep is real time; no test needs to wait for it.
    # (`raising=False` so test_30 can be run unchanged on the parent commit.)
    monkeypatch.setattr(hp, "CONTACT_EXISTENCE_PACING_SECONDS", 0,
                        raising=False)
    try:
        yield _Db(url)
    finally:
        if getattr(connection, "_pool", None) is not None:
            connection._pool.closeall()
            connection._pool = None


# ═════════════════════════════════════════════════════════════════════════════
# The real paths, driven by the fake portal
# ═════════════════════════════════════════════════════════════════════════════
def _sync(fake, now, mode="incremental"):
    """The REAL contact sync: the real service, the real keyset iterator, the
    real normaliser and the real funnel writer."""
    from services import hubspot_contact_funnel_sync_service as sync_service

    def pages(since, max_pages=None):
        return hp.iter_contacts_modified_since(
            since, max_pages=max_pages, client=fake.client)
    result = sync_service.run_contact_funnel_sync(
        mode=mode, page_iterator=pages, now=now)
    assert result["status"] == "success", result
    return result


def _reconcile(fake, now, **kwargs):
    from services import hubspot_contact_reconciliation_service as service
    return service.run_contact_reconciliation(
        now=now, client=fake.client, **kwargs)


def _load(fake, now=DAY_1 - 2 * HOURS):
    """Fill the funnel the way production did: a bootstrap, then an
    incremental run, so the freshness the readers publish on is the real
    assessor's verdict over real sync state."""
    _sync(fake, now, mode="bootstrap")
    _sync(fake, now + HOURS)


def _day(fake, now):
    """One morning as production runs it: the contact sync, then the check."""
    _sync(fake, now)
    return _reconcile(fake, now)


def _channel_totals(now):
    from services import channel_funnel_service as channel_funnel
    payload = channel_funnel.build_channel_funnel("all_time", now=now)
    assert payload["publication"] == {"status": "published", "reason": None}, \
        payload["publication"]
    return payload["totals"]


def _cohort(now):
    from services import marketing_outcome_cohort_service as cohort_service
    out = cohort_service.build_window_outcomes(
        None, date(2026, 12, 31),
        resolve_label=lambda label: ("google_ads", "campaign-1"), now=now)
    assert out["sql_publication"] == ("published", None), out["sql_publication"]
    assert out["reconciliation_problems"] == []
    return out["cohort"]


def _funnel_rows(db, ids):
    return {r["contact_id"]: r for r in db.rows(
        "SELECT * FROM hubspot_contact_funnel WHERE contact_id = ANY(%s)",
        (list(ids),))}


def _use_config(monkeypatch, tmp_path, **values):
    """Point the service at a thresholds file with these limits, through its
    real loader."""
    from services import hubspot_contact_reconciliation_service as service
    path = tmp_path / "thresholds.yaml"
    path.write_text("contact_reconciliation:\n" + "".join(
        f"  {k}: {v}\n" for k, v in values.items()))
    monkeypatch.setattr(service, "_CONFIG_PATH", path)


# ═════════════════════════════════════════════════════════════════════════════
# The connector: one read-only question, and three different answers
# ═════════════════════════════════════════════════════════════════════════════
def test_01_the_check_is_one_search_per_batch_of_at_most_100_ids_for_one_property():
    fake = _portal(250)
    sleeps: list = []
    out = hp.check_contacts_exist(
        [_cid(i) for i in range(250)], client=fake.client, sleep=sleeps.append)

    assert out["search_calls"] == 3 and len(fake.existence_requests) == 3
    assert [len(r["filterGroups"][0]["filters"][0]["values"])
            for r, _ in fake.existence_requests] == [100, 100, 50]
    for req, kwargs in fake.existence_requests:
        assert req["filterGroups"] == [{"filters": [
            {"propertyName": "hs_object_id", "operator": "IN",
             "values": req["filterGroups"][0]["filters"][0]["values"]}]}]
        assert req["properties"] == ["hs_object_id"]
        assert req["limit"] == 100 == hp.CONTACT_EXISTENCE_BATCH_SIZE
        assert not req.get("after")
        assert kwargs == {"_request_timeout": hp.HUBSPOT_REQUEST_TIMEOUT_SECONDS}
    # Paced between calls: not before the first, not after the last.
    assert sleeps == [hp.CONTACT_EXISTENCE_PACING_SECONDS] * 2
    assert hp.CONTACT_EXISTENCE_PACING_SECONDS > 0
    assert len(out["existing"]) == 250 and out["missing"] == [] \
        and out["unknown"] == []
    assert (out["batches"], out["batches_answered"], out["batches_unknown"]) \
        == (3, 3, 0)


def test_02_a_contact_hubspot_does_not_return_is_missing_and_only_that_one():
    fake = _portal(250)
    fake.delete(_cid(7), _cid(120), _cid(249))
    out = hp.check_contacts_exist(
        [_cid(i) for i in range(250)], client=fake.client, sleep=lambda s: None)
    assert out["missing"] == [_cid(7), _cid(120), _cid(249)]
    assert len(out["existing"]) == 247 and out["unknown"] == []
    assert out["batches_unknown"] == 0


@pytest.mark.parametrize("mode", UNUSABLE)
def test_03_a_batch_without_a_usable_answer_is_unknown_never_all_missing(mode):
    """Batch 2 (ids 100-199) answers badly. Every id in it is UNKNOWN — the
    deleted one included — and the batches around it are still answered."""
    fake = _portal(300)
    fake.delete(_cid(20), _cid(150), _cid(250))
    fake.faults[_cid(101)] = mode
    out = hp.check_contacts_exist(
        [_cid(i) for i in range(300)], client=fake.client, sleep=lambda s: None)

    assert out["unknown"] == [_cid(i) for i in range(100, 200)]
    assert out["missing"] == [_cid(20), _cid(250)]
    assert _cid(150) not in out["missing"]
    assert len(out["existing"]) == 198
    assert (out["batches"], out["batches_answered"], out["batches_unknown"]) \
        == (3, 2, 1)
    assert out["search_calls"] == 3 and out["stopped_early"] is False
    assert out["errors"]


def test_04_a_rate_limit_is_retried_a_bounded_number_of_times_then_unknown(
        monkeypatch):
    waits: list = []
    monkeypatch.setattr(hp.time, "sleep", waits.append)
    fake = _portal(200)
    fake.faults[_cid(0)] = "rate_limited"
    out = hp.check_contacts_exist(
        [_cid(i) for i in range(200)], client=fake.client, sleep=lambda s: None)
    # MAX_RETRIES + 1 attempts for the limited batch, one for the other.
    assert len(fake.existence_requests) == hp.MAX_RETRIES + 2
    assert len(waits) == hp.MAX_RETRIES
    assert out["unknown"] == [_cid(i) for i in range(100)]
    assert out["missing"] == [] and len(out["existing"]) == 100
    assert out["search_calls"] == 2          # batches sent, retries not counted


def test_05_after_three_unanswered_batches_in_a_row_the_rest_are_not_sent():
    fake = _portal(1000)
    fake.fault_all = "unauthorized"
    out = hp.check_contacts_exist(
        [_cid(i) for i in range(1000)], client=fake.client, sleep=lambda s: None)
    assert len(fake.existence_requests) == 3 \
        == hp.CONTACT_EXISTENCE_MAX_CONSECUTIVE_UNKNOWN
    assert out["stopped_early"] is True
    assert len(out["unknown"]) == 1000
    assert out["missing"] == [] and out["existing"] == []
    assert (out["batches"], out["batches_answered"], out["batches_unknown"]) \
        == (10, 0, 10)

    # One bad batch between good ones does not trip it.
    fake = _portal(1000)
    for n in (0, 200, 400, 600, 800):
        fake.faults[_cid(n)] = "error"
    out = hp.check_contacts_exist(
        [_cid(i) for i in range(1000)], client=fake.client, sleep=lambda s: None)
    assert out["stopped_early"] is False and out["batches_answered"] == 5
    assert len(fake.existence_requests) == 10


def test_06_an_id_that_is_not_a_hubspot_object_id_is_never_sent_and_is_unknown():
    fake = _portal(3)
    out = hp.check_contacts_exist(
        [_cid(0), "c17", _cid(1), "", None, " 12x ", _cid(2), _cid(0)],
        client=fake.client, sleep=lambda s: None)
    (req, _kwargs), = fake.existence_requests
    assert req["filterGroups"][0]["filters"][0]["values"] == \
        [_cid(0), _cid(1), _cid(2)]
    assert out["unverifiable"] == ["c17", "12x"] == out["unknown"]
    assert out["missing"] == [] and len(out["existing"]) == 3
    assert out["batches_unknown"] == 1 and out["batches"] == 2


def test_08_an_id_stored_with_leading_zeros_is_unknown_on_its_own_and_does_not_poison_its_batch():
    """``0009000005`` is the number 9000005 to HubSpot, which answers with
    ``9000005``. On the first commit of this slice the padded id was sent, the
    canonical answer was "a contact that was not asked for", and all 100 ids
    of the batch were unknown on every run. Run, not read: the fake answers
    as HubSpot does."""
    fake = _portal(100)
    fake.delete(_cid(50))
    padded = "000" + _cid(5)                 # the live contact 5, mis-stored
    ids = [_cid(i) for i in range(100) if i != 5] + [padded]
    out = hp.check_contacts_exist(ids, client=fake.client, sleep=lambda s: None)

    assert out["unknown"] == [padded] == out["unverifiable"]
    assert out["missing"] == [_cid(50)]          # the real deletion, still found
    assert len(out["existing"]) == 98
    assert out["batches_answered"] == 1
    (req, _kwargs), = fake.existence_requests
    assert padded not in req["filterGroups"][0]["filters"][0]["values"]


def test_09_an_answer_is_matched_to_the_ids_asked_as_numbers_not_as_text():
    """HubSpot spelling an id differently from how it was asked is the same
    contact. Compared as text it was "not asked for" and the batch unknown."""
    fake = _portal(100)
    fake.delete(_cid(50))
    fake.fault_all = "padded_ids"
    out = hp.check_contacts_exist(
        [_cid(i) for i in range(100)], client=fake.client, sleep=lambda s: None)
    assert out["unknown"] == [] and out["batches_unknown"] == 0
    assert out["missing"] == [_cid(50)] and len(out["existing"]) == 99
    assert set(out["existing"]) <= {_cid(i) for i in range(100)}


WINDOW_DEFAULTS = {"removal_window_days": 7.0, "max_removals_per_window": 100,
                   "max_removal_window_fraction": 0.02}


def test_07_the_limits_come_from_the_thresholds_file_and_a_bad_value_does_not_loosen_them(
        monkeypatch, tmp_path):
    from services import hubspot_contact_reconciliation_service as service
    assert service.load_config() == {
        "max_removals_per_run": 50, "max_removal_fraction": 0.01,
        "min_hours_between_strikes": 20.0, **WINDOW_DEFAULTS}
    assert (service.DEFAULT_MAX_REMOVALS_PER_RUN,
            service.DEFAULT_MAX_REMOVAL_FRACTION,
            service.DEFAULT_MIN_HOURS_BETWEEN_STRIKES) == (50, 0.01, 20.0)

    _use_config(monkeypatch, tmp_path, max_removals_per_run=7,
                max_removal_fraction=0.5, min_hours_between_strikes=30)
    assert service.load_config() == {
        "max_removals_per_run": 7, "max_removal_fraction": 0.5,
        "min_hours_between_strikes": 30.0, **WINDOW_DEFAULTS}
    _use_config(monkeypatch, tmp_path, removal_window_days=3,
                max_removals_per_window=9, max_removal_window_fraction=0.1)
    assert {k: service.load_config()[k] for k in WINDOW_DEFAULTS} == {
        "removal_window_days": 3.0, "max_removals_per_window": 9,
        "max_removal_window_fraction": 0.1}
    _use_config(monkeypatch, tmp_path, removal_window_days=0,
                max_removals_per_window=-1, max_removal_window_fraction=3)
    assert {k: service.load_config()[k] for k in WINDOW_DEFAULTS} \
        == WINDOW_DEFAULTS

    _use_config(monkeypatch, tmp_path, max_removals_per_run="'lots'",
                max_removal_fraction=7, min_hours_between_strikes=0)
    assert service.load_config() == {
        "max_removals_per_run": 50, "max_removal_fraction": 0.01,
        "min_hours_between_strikes": 20.0, **WINDOW_DEFAULTS}

    monkeypatch.setattr(service, "_CONFIG_PATH", tmp_path / "absent.yaml")
    assert service.load_config()["max_removals_per_run"] == 50


# ═════════════════════════════════════════════════════════════════════════════
# The production shape
# ═════════════════════════════════════════════════════════════════════════════
GONE = (3, 137, 250, 399)        # four of 400, one in each batch of 100


@needs_pg
def test_10_deleted_contacts_are_struck_then_moved_and_the_funnel_reads_drop_by_exactly_them(
        db):
    """400 contacts ingested by the real sync; four are then deleted in
    HubSpot. The first check strikes them and removes nothing. A second check
    under 20 hours later removes nothing. A check at 20 hours moves exactly
    those four, with every column they had, and the channel funnel and the
    acquisition cohort drop by exactly four — and by none before."""
    fake = _portal(400, SQL_CONTACT)
    gone = [_cid(n) for n in GONE]
    _load(fake)

    clean = _day(fake, DAY_1 - HOURS)
    assert clean["status"] == "success"
    assert (clean["contacts_stored"], clean["contacts_checked"],
            clean["missing"], clean["removed"]) == (400, 400, 0, 0)
    before = _channel_totals(DAY_1)
    before_cohort = _cohort(DAY_1)
    assert before["contacts"] == 400 and before["reached_sql"] == 1
    assert before_cohort["all_sources"]["contacts_acquired"] == 400
    assert before_cohort["google_ads"]["contacts_acquired"] == 200
    stored = _funnel_rows(db, gone)
    assert sorted(stored) == sorted(gone)

    fake.delete(*gone)

    # ── check 1: strikes, no removal ────────────────────────────────────────
    first = _day(fake, DAY_1)
    assert first["status"] == "success"
    assert (first["missing"], first["newly_missing"], first["would_remove"],
            first["removed"]) == (4, 4, 0, 0)
    assert sorted(db.strikes()) == sorted(gone)
    assert {s["checks"] for s in db.strikes().values()} == {1}
    assert db.funnel_count() == 400
    assert _channel_totals(DAY_1) == before

    # ── check 2, 19h59m later: still no removal ─────────────────────────────
    early = _day(fake, DAY_1 + timedelta(hours=19, minutes=59))
    assert early["status"] == "success"
    assert (early["missing"], early["newly_missing"], early["would_remove"],
            early["removed"]) == (4, 0, 0, 0)
    assert {s["checks"] for s in db.strikes().values()} == {2}
    assert db.funnel_count() == 400
    assert _channel_totals(DAY_1 + 20 * HOURS) == before

    # ── check 3, 20h after the first: exactly those four move ───────────────
    third = _day(fake, DAY_1 + 20 * HOURS)
    assert third["status"] == "success"
    assert (third["missing"], third["newly_missing"], third["would_remove"],
            third["removed"]) == (4, 0, 4, 4)
    assert db.funnel_count() == 396
    assert _funnel_rows(db, gone) == {}
    assert db.strikes() == {}, "a removed contact keeps no strike"

    archived = {r["contact_id"]: r for r in db.rows(
        "SELECT * FROM hubspot_contact_funnel_removed")}
    assert sorted(archived) == sorted(gone)
    for cid, was in stored.items():
        now_is = archived[cid]
        # Every column the funnel row had, with the value it had.
        assert {k: now_is[k] for k in was} == was
        assert now_is["removal_reason"] == "absent_from_hubspot"
        assert now_is["reconciliation_run_id"] == third["reconciliation_run_id"]
        assert now_is["absence_checks"] == 3
        assert now_is["first_missing_at"] == DAY_1
        assert now_is["last_missing_at"] == DAY_1 + 20 * HOURS
        assert now_is["removed_at"] is not None
    assert archived[_cid(137)]["date_entered_sql"] is not None

    after = _channel_totals(DAY_1 + 21 * HOURS)
    after_cohort = _cohort(DAY_1 + 21 * HOURS)
    assert after["contacts"] == before["contacts"] - 4 == 396
    assert after["reached_sql"] == before["reached_sql"] - 1 == 0
    assert after_cohort["all_sources"]["contacts_acquired"] == 396
    # 137 is odd (organic); 250 is the one Google Ads contact among the four.
    assert after_cohort["google_ads"]["contacts_acquired"] == 199
    assert after_cohort["all_sources"]["sqls"] == \
        before_cohort["all_sources"]["sqls"] - 1

    # The durable run record says the same thing the result did.
    row = db.run_row(third["reconciliation_run_id"])
    assert (row["status"], row["contacts_stored"], row["contacts_checked"],
            row["batches"], row["batches_unknown"], row["missing"],
            row["newly_missing"], row["returned"], row["would_remove"],
            row["removed"], row["error"]) == \
        ("success", 400, 400, 4, 0, 4, 0, 0, 4, 4, None)
    assert row["finished_at"] >= row["started_at"]

    # A fourth day: nothing left to find, nothing re-added by the sync.
    fourth = _day(fake, DAY_1 + 44 * HOURS)
    assert (fourth["contacts_stored"], fourth["missing"], fourth["removed"]) \
        == (396, 0, 0)
    assert db.funnel_count() == 396


@needs_pg
def test_11_a_contact_missing_once_and_present_again_is_never_removed_and_loses_its_strike(
        db):
    fake = _portal(300)
    _load(fake)
    odd = _cid(42)

    held = fake.contacts.pop(odd)                 # HubSpot omits it once
    first = _day(fake, DAY_1)
    assert (first["missing"], first["newly_missing"]) == (1, 1)
    assert list(db.strikes()) == [odd]

    fake.contacts[odd] = held                     # and returns it again
    second = _day(fake, DAY_1 + 24 * HOURS)
    assert second["status"] == "success"
    assert (second["missing"], second["returned"], second["removed"]) == (0, 1, 0)
    assert db.strikes() == {}
    assert db.funnel_count() == 300

    # Missing again two days after the FIRST miss: that is a first strike, not
    # a second one. The earlier miss was answered by its return.
    fake.contacts.pop(odd)
    third = _day(fake, DAY_1 + 48 * HOURS)
    assert (third["missing"], third["newly_missing"], third["would_remove"],
            third["removed"]) == (1, 1, 0, 0)
    assert db.strikes()[odd]["checks"] == 1
    assert db.strikes()[odd]["first_missing_at"] == DAY_1 + 48 * HOURS
    assert db.funnel_count() == 300
    assert db.scalar("SELECT COUNT(*) FROM hubspot_contact_funnel_removed") == 0


@needs_pg
def test_12_one_well_formed_but_wrong_answer_strikes_a_whole_batch_removes_nobody_and_is_not_green(
        db):
    """HubSpot answers one batch "nothing matches" with a consistent total. It
    is a usable answer, so 100 contacts are struck — and the next day they are
    all returned, every strike is cleared and no contact ever left.

    100 first strikes in one run is over the limits a removal is held to (50,
    or 1% of the table), so that first run is ``partial`` and says why: the
    warning arrives on day one, not as the refusal on day two. (Review F4: it
    was ``success``.)"""
    fake = _portal(300)
    _load(fake)
    fake.faults[_cid(100)] = "well_formed_but_empty"
    first = _day(fake, DAY_1)
    assert (first["missing"], first["newly_missing"], first["removed"]) \
        == (100, 100, 0)
    assert first["status"] == "partial"
    assert "100 stored contacts were not returned by HubSpot for the first " \
        "time in one run" in first["error"]
    assert db.run_row(first["reconciliation_run_id"])["status"] == "partial"
    assert db.funnel_count() == 300

    fake.faults.clear()
    second = _day(fake, DAY_1 + 24 * HOURS)
    assert (second["status"], second["missing"], second["returned"],
            second["removed"]) == ("success", 0, 100, 0)
    assert db.strikes() == {} and db.funnel_count() == 300

    # At the limit is not over it: 3 of 300 is exactly 1%, and green.
    fake.delete(_cid(1), _cid(101), _cid(201))
    third = _day(fake, DAY_1 + 48 * HOURS)
    assert (third["status"], third["newly_missing"]) == ("success", 3)
    # Four more is over 1% while far under 50: either limit is enough.
    fake.delete(_cid(2), _cid(102), _cid(202), _cid(203))
    fourth = _day(fake, DAY_1 + 49 * HOURS)
    assert (fourth["status"], fourth["newly_missing"], fourth["removed"]) \
        == ("partial", 4, 0)
    assert "1.00% of 300 stored contacts" in fourth["error"]


@needs_pg
def test_12b_more_first_strikes_than_the_configured_limit_is_partial(
        db, monkeypatch, tmp_path):
    fake = _portal(400)
    _load(fake)
    _use_config(monkeypatch, tmp_path, max_removals_per_run=2,
                max_removal_fraction=0.5)
    fake.delete(_cid(1), _cid(2))
    at_limit = _day(fake, DAY_1)
    assert (at_limit["status"], at_limit["newly_missing"]) == ("success", 2)
    fake.delete(_cid(3), _cid(4), _cid(5))
    over = _day(fake, DAY_1 + HOURS)
    assert (over["status"], over["newly_missing"], over["removed"]) \
        == ("partial", 3, 0)
    assert "limits: 2" in over["error"]


@needs_pg
@pytest.mark.parametrize("mode", ("error", "no_results", "total_above_results",
                                  "total_below_results", "foreign_contact"))
def test_13_a_run_with_an_unanswered_batch_strikes_the_answered_ones_and_removes_nothing(
        db, mode):
    """Review F1: a run that could not check every contact used to remove the
    answered share. It records strikes for the answered batches, reports what
    it would have removed, removes NOTHING, and ends ``partial``. The removal
    happens on the first run in which every batch is answered."""
    fake = _portal(400)
    _load(fake)
    in_bad_batch, in_good_batch = _cid(150), _cid(250)
    fake.delete(in_bad_batch, in_good_batch)
    fake.faults[_cid(101)] = mode                 # ids 100-199, both mornings

    first = _day(fake, DAY_1)
    assert first["status"] == "partial"
    assert (first["batches"], first["batches_unknown"], first["contacts_checked"],
            first["contacts_unknown"]) == (4, 1, 300, 100)
    assert (first["missing"], first["newly_missing"], first["removed"]) == (1, 1, 0)
    assert list(db.strikes()) == [in_good_batch]

    second = _day(fake, DAY_1 + 24 * HOURS)
    assert second["status"] == "partial"
    assert second["batches_unknown"] == 1
    # Twice missing, 24 hours apart, missing in this run — and still here.
    assert (second["missing"], second["would_remove"], second["removed"]) \
        == (1, 1, 0)
    assert db.funnel_count() == 400
    assert db.strikes()[in_good_batch]["checks"] == 2
    assert in_bad_batch not in db.strikes()
    row = db.run_row(second["reconciliation_run_id"])
    assert (row["status"], row["batches_unknown"], row["would_remove"],
            row["removed"]) == ("partial", 1, 1, 0)
    assert "no usable answer" in row["error"]
    assert "removes nothing" in row["error"]
    assert "1 would have been removed" in row["error"]
    assert db.scalar("SELECT COUNT(*) FROM hubspot_contact_funnel_removed") == 0

    # Every batch answered: the one in the good batch goes now; the one whose
    # batch was never answered before gets its FIRST strike.
    fake.faults.clear()
    third = _day(fake, DAY_1 + 48 * HOURS)
    assert (third["status"], third["newly_missing"], third["would_remove"],
            third["removed"]) == ("success", 1, 1, 1)
    assert list(_funnel_rows(db, [in_bad_batch, in_good_batch])) == [in_bad_batch]
    assert list(db.strikes()) == [in_bad_batch]


@needs_pg
def test_14_a_contact_is_removed_only_if_it_was_missing_again_in_the_current_run(
        db, monkeypatch, tmp_path):
    """Two strikes 24 hours apart, but the removal that morning was refused.
    The NEXT morning the contact's batch gets no answer. Its two old strikes
    must not remove it: this run did not check it."""
    fake = _portal(300)
    _load(fake)
    victim = _cid(150)
    fake.delete(victim)

    _day(fake, DAY_1)
    _use_config(monkeypatch, tmp_path, max_removals_per_run=0)
    refused = _day(fake, DAY_1 + 24 * HOURS)
    assert refused["status"] == "refused" and refused["removed"] == 0
    assert db.strikes()[victim]["checks"] == 2

    _use_config(monkeypatch, tmp_path, max_removals_per_run=50)
    fake.faults[_cid(101)] = "error"
    blind = _day(fake, DAY_1 + 48 * HOURS)
    assert blind["status"] == "partial"
    assert (blind["would_remove"], blind["removed"]) == (0, 0)
    assert db.funnel_count() == 300
    assert db.strikes()[victim]["checks"] == 2      # untouched, not renewed

    fake.faults.clear()
    seen = _day(fake, DAY_1 + 72 * HOURS)
    assert seen["status"] == "success"
    assert (seen["would_remove"], seen["removed"]) == (1, 1)
    assert db.funnel_count() == 299


@needs_pg
def test_15_more_than_one_percent_missing_removes_nothing_and_the_run_is_refused(
        db):
    """Five of 400 is over 1%. Nothing moves; the strikes stay recorded."""
    fake = _portal(400)
    _load(fake)
    gone = [_cid(n) for n in (1, 101, 201, 301, 302)]
    fake.delete(*gone)

    _day(fake, DAY_1)
    refused = _day(fake, DAY_1 + 24 * HOURS)
    assert refused["status"] == "refused"
    assert (refused["missing"], refused["would_remove"], refused["removed"],
            refused["classification_rows_removed"]) == (5, 5, 0, 0)
    assert "1.00% of the 400 stored contacts" in refused["refusal_reason"]
    assert "over the limit" not in refused["refusal_reason"]
    assert db.funnel_count() == 400
    assert db.scalar("SELECT COUNT(*) FROM hubspot_contact_funnel_removed") == 0
    assert sorted(db.strikes()) == sorted(gone)
    assert {s["checks"] for s in db.strikes().values()} == {2}
    row = db.run_row(refused["reconciliation_run_id"])
    assert (row["status"], row["would_remove"], row["removed"]) == \
        ("refused", 5, 0)
    assert "5 stored contacts now qualify for removal" in row["error"]
    assert refused["backlog_qualified"] == 5 == row["backlog_qualified"]

    # Four of 400 is exactly 1%: allowed. One contact comes back in HubSpot.
    fake.restore(_cid(302), modified_ms=BASE_MS + 10_000_000)
    allowed = _day(fake, DAY_1 + 48 * HOURS)
    assert allowed["status"] == "success"
    assert (allowed["returned"], allowed["would_remove"], allowed["removed"]) \
        == (1, 4, 4)
    assert db.funnel_count() == 396


@needs_pg
def test_16_more_than_the_configured_limit_missing_removes_nothing_and_strikes_are_kept(
        db, monkeypatch, tmp_path):
    fake = _portal(400)
    _load(fake)
    gone = [_cid(n) for n in (10, 110, 210)]
    fake.delete(*gone)
    _use_config(monkeypatch, tmp_path, max_removals_per_run=2,
                max_removal_fraction=0.5)

    _day(fake, DAY_1)
    refused = _day(fake, DAY_1 + 24 * HOURS)
    assert refused["status"] == "refused"
    assert (refused["would_remove"], refused["removed"]) == (3, 0)
    assert "over the limit of 2" in refused["refusal_reason"]
    assert db.funnel_count() == 400
    assert sorted(db.strikes()) == sorted(gone)

    # The limit is the whole run's, not per batch: at exactly the limit it goes.
    _use_config(monkeypatch, tmp_path, max_removals_per_run=3,
                max_removal_fraction=0.5)
    allowed = _day(fake, DAY_1 + 48 * HOURS)
    assert (allowed["status"], allowed["removed"]) == ("success", 3)


@needs_pg
def test_17_a_removed_contact_that_reappears_is_ingested_again_and_counted_again(
        db):
    fake = _portal(300)
    _load(fake)
    back = _cid(120)
    fake.delete(back)
    _day(fake, DAY_1)
    removed = _day(fake, DAY_1 + 24 * HOURS)
    assert removed["removed"] == 1 and db.funnel_count() == 299
    assert _channel_totals(DAY_1 + 25 * HOURS)["contacts"] == 299
    old_row_id = db.scalar(
        "SELECT id FROM hubspot_contact_funnel_removed WHERE contact_id = %s",
        (back,))

    # Restored in HubSpot: the modified-since sync sees it like any contact.
    fake.restore(back, modified_ms=BASE_MS + 20_000_000)
    again = _day(fake, DAY_1 + 48 * HOURS)
    assert again["status"] == "success"
    assert (again["contacts_stored"], again["missing"], again["removed"]) \
        == (300, 0, 0)
    assert db.funnel_count() == 300
    row = _funnel_rows(db, [back])[back]
    assert row["id"] != old_row_id                # a normal, new funnel row
    assert db.strikes() == {}                     # and it starts clean
    assert _channel_totals(DAY_1 + 49 * HOURS)["contacts"] == 300
    # The archive keeps the record of the earlier removal.
    assert db.scalar(
        "SELECT COUNT(*) FROM hubspot_contact_funnel_removed") == 1

    # Deleted again: it takes two fresh strikes, not one against old history.
    fake.delete(back)
    one = _day(fake, DAY_1 + 72 * HOURS)
    assert (one["newly_missing"], one["would_remove"], one["removed"]) == (1, 0, 0)
    assert db.funnel_count() == 300
    two = _day(fake, DAY_1 + 96 * HOURS)
    assert two["removed"] == 1 and db.funnel_count() == 299
    assert db.scalar(
        "SELECT COUNT(*) FROM hubspot_contact_funnel_removed "
        "WHERE contact_id = %s", (back,)) == 2

    # Restored once more, ingested by the sync, and deleted again BEFORE any
    # check has seen it present. The strikes that removed it last time went
    # with that removal, so this is a first strike and the contact stays.
    fake.restore(back, modified_ms=BASE_MS + 30_000_000)
    _sync(fake, DAY_1 + 120 * HOURS)
    assert db.funnel_count() == 300
    fake.delete(back)
    blink = _day(fake, DAY_1 + 121 * HOURS)
    assert (blink["missing"], blink["newly_missing"], blink["would_remove"],
            blink["removed"]) == (1, 1, 0, 0)
    assert db.funnel_count() == 300
    assert db.strikes()[back]["checks"] == 1


@needs_pg
def test_18_classification_rows_move_with_their_contact_and_legacy_leads_are_untouched(
        db):
    from db import writers as db_writers

    fake = _portal(300)
    _load(fake)
    gone, kept = _cid(77), _cid(78)
    run_id, error = db_writers.write_run_detailed({
        "run_type": "daily", "started_at": "2026-11-17T06:00:00Z",
        "status": "success"})
    assert run_id and not error
    for cid in (gone, kept):
        assert db_writers.upsert_contact_source_classification([{
            "contact_key": cid, "contact_id": cid,
            "source_primary_raw": "PAID_SEARCH", "source_detail_raw": "Brand - US",
            "acquisition_group": "google_ads",
            "classification_rule_version": "v1",
            "contact_created_at": "2026-10-09T08:00:00Z",
            "status_category": "qualified"}]) == 1
    # The legacy snapshot table holds several rows per contact (one per run).
    for _ in range(3):
        assert db_writers.write_leads(run_id, [
            {"id": gone, "properties": {"mql_status": "OPEN - Connecting"}},
            {"id": kept, "properties": {"mql_status": "OPEN - Connecting"}}]) == 2
    leads_before = db.rows(
        "SELECT * FROM leads ORDER BY id")
    assert len(leads_before) == 6
    was = db.rows("SELECT * FROM contact_source_classification "
                  "WHERE contact_id = %s", (gone,))[0]

    fake.delete(gone)
    first = _day(fake, DAY_1)
    assert first["classification_rows_removed"] == 0
    assert db.scalar("SELECT COUNT(*) FROM contact_source_classification") == 2

    second = _day(fake, DAY_1 + 24 * HOURS)
    assert (second["removed"], second["classification_rows_removed"]) == (1, 1)
    assert [r["contact_id"] for r in db.rows(
        "SELECT contact_id FROM contact_source_classification")] == [kept]
    (archived,) = db.rows("SELECT * FROM contact_source_classification_removed")
    assert {k: archived[k] for k in was} == was
    assert archived["removal_reason"] == "absent_from_hubspot"
    assert archived["reconciliation_run_id"] == second["reconciliation_run_id"]
    assert db.run_row(second["reconciliation_run_id"])[
        "classification_rows_removed"] == 1

    # The Revenue by Source lead read sees one contact fewer.
    from db import revenue_repository
    leads = revenue_repository.fetch_source_leads(None, date(2026, 12, 31))
    assert leads["available"] and len(leads["rows"]) == 1

    # Not covered by this slice, and not touched by it.
    assert db.rows("SELECT * FROM leads ORDER BY id") == leads_before


# ═════════════════════════════════════════════════════════════════════════════
# The independent review of the first commit (3b90ee8): F1, F2, F3, F7
# ═════════════════════════════════════════════════════════════════════════════
@needs_pg
def test_50_a_degraded_hubspot_answered_one_batch_a_day_cannot_drain_live_contacts_in_slices(
        db, monkeypatch, tmp_path):
    """Review F1 (blocker), the reviewer's probe B scaled down. A degraded
    search index gives well-formed answers that omit four LIVE contacts from
    each of four batches. Day 1 every batch answers. On days 2-4 only one
    batch a day answers and the rest fail. With a limit of 5 per run, the
    first commit removed four live contacts on each of those days — twelve
    contacts that all still exist — because the valve counted only the batch
    answered that day. Nothing may leave, and a fully answered run must be
    refused on the whole backlog."""
    fake = _portal(400)
    _load(fake)
    _use_config(monkeypatch, tmp_path, max_removals_per_run=5,
                max_removal_fraction=0.5)
    fake.omitted = {_cid(b * 100 + k) for b in range(4) for k in range(4)}

    first = _day(fake, DAY_1)
    assert (first["missing"], first["removed"]) == (16, 0)

    for day in range(1, 4):
        answering = day - 1
        calls_before = len(fake.existence_requests)

        def hook(asked, index, answering=answering, base=calls_before):
            if index - base != answering:
                raise ApiException(status=500, reason="Internal Server Error")
            return asked
        fake.existence_hook = hook
        run = _day(fake, DAY_1 + day * 24 * HOURS)
        # The first such day is a partial run that removes nothing. From the
        # second on, the strikes of two days' batches both qualify and the
        # backlog (8, 12, 16) is itself over the limit of 5.
        assert run["removed"] == 0, f"day {day + 1} removed live contacts"
        assert db.funnel_count() == 400
        assert run["status"] == ("partial" if day == 1 else "refused")
        assert run["backlog_qualified"] == 4 * day

    assert all(cid in fake.contacts for cid in fake.omitted)
    assert db.scalar("SELECT COUNT(*) FROM hubspot_contact_funnel_removed") == 0

    # Every batch answers again, the index still degraded: the whole backlog
    # is judged, and refused.
    fake.existence_hook = None
    full = _day(fake, DAY_1 + 4 * 24 * HOURS)
    assert full["status"] == "refused"
    assert (full["backlog_qualified"], full["would_remove"], full["removed"]) \
        == (16, 16, 0)
    # More candidates than one run may remove: the valve refuses whatever
    # HubSpot says, so no direct read is spent on them.
    assert fake.direct_requests == []
    assert "16 stored contacts now qualify for removal" in full["refusal_reason"]
    assert db.funnel_count() == 400


@needs_pg
def test_51_the_valve_counts_every_contact_that_qualifies_not_only_those_struck_by_this_run(
        db):
    """Review F1(a), at the statement itself. Eight contacts carry two strikes
    a day apart; the current run struck only three of them again. Three is
    under a limit of 5 — the backlog of eight is not, and nothing moves."""
    from db import writers as db_writers

    fake = _portal(300)
    _load(fake)
    eight = [_cid(n) for n in range(10, 18)]
    run_1 = db_writers.start_contact_reconciliation_run()
    db_writers.record_contact_absence(run_1, missing_ids=eight, returned_ids=[],
                                      observed_at=DAY_1)
    run_2 = db_writers.start_contact_reconciliation_run()
    db_writers.record_contact_absence(run_2, missing_ids=eight, returned_ids=[],
                                      observed_at=DAY_1 + 24 * HOURS)
    run_3 = db_writers.start_contact_reconciliation_run()
    db_writers.record_contact_absence(run_3, missing_ids=eight[:3],
                                      returned_ids=[],
                                      observed_at=DAY_1 + 48 * HOURS)

    moved = db_writers.move_absent_contacts(
        run_3, min_hours_between_strikes=20, max_removals=5, max_fraction=1,
        every_batch_answered=True, confirmed_absent_ids=eight)
    assert moved["ok"] and moved["refused"] is True
    assert (moved["would_remove"], moved["backlog_qualified"], moved["removed"]) \
        == (3, 8, 0)
    assert db.funnel_count() == 300

    # With room for the whole backlog, only this run's three are moved.
    moved = db_writers.move_absent_contacts(
        run_3, min_hours_between_strikes=20, max_removals=8, max_fraction=1,
        every_batch_answered=True, confirmed_absent_ids=eight)
    assert (moved["refused"], moved["would_remove"], moved["backlog_qualified"],
            moved["removed"]) == (False, 3, 8, 3)
    assert sorted(_funnel_rows(db, eight)) == eight[3:]


@needs_pg
def test_52_a_contact_ingested_by_the_sync_while_the_check_runs_is_neither_struck_nor_removed(
        db):
    """Review F2, the reviewer's probe C through the real sync. A contact is
    struck on day 1. On day 2 HubSpot answers "missing" — and before that
    answer is written down the contact is restored and the real sync ingests
    it again. The first commit moved the freshly ingested row out although the
    contact exists."""
    fake = _portal(300)
    _load(fake)
    x = _cid(42)
    fake.delete(x)
    first = _day(fake, DAY_1)
    assert first["newly_missing"] == 1

    def racing_check(contact_ids, *, client=None):
        answer = hp.check_contacts_exist(contact_ids, client=client)
        assert x in answer["missing"]
        fake.restore(x, modified_ms=BASE_MS + 20_000_000)
        _sync(fake, DAY_1 + 24 * HOURS)      # the real sync re-ingests it
        return answer
    _sync(fake, DAY_1 + 23 * HOURS)
    second = _reconcile(fake, DAY_1 + 24 * HOURS, existence_check=racing_check)

    assert x in fake.contacts
    assert (second["would_remove"], second["removed"]) == (0, 0)
    assert list(_funnel_rows(db, [x])) == [x]
    assert db.scalar("SELECT COUNT(*) FROM hubspot_contact_funnel_removed") == 0
    # Not struck by that run either: the day-1 strike is as it was.
    strike = db.strikes()[x]
    assert strike["checks"] == 1 and strike["last_missing_at"] == DAY_1
    assert second["missing"] == 0

    third = _day(fake, DAY_1 + 48 * HOURS)
    assert (third["status"], third["returned"], third["removed"]) \
        == ("success", 1, 0)
    assert db.strikes() == {} and db.funnel_count() == 300


def _portal_with_an_open_incident(db, wall):
    """400 contacts, a coverage boundary, and contact 139 reaching SQL after
    it with no entry date — an open post-boundary incident, made by the real
    boundary service."""
    from services import sql_coverage_boundary_service as boundary

    fake = _portal(400, SQL_CONTACT)
    _load(fake, wall - 30 * HOURS)
    assert boundary.establish_boundary(apply=True)["ok"]
    held = _cid(139)
    fake.contacts[held]["properties"].update(
        lifecyclestage="salesqualifiedlead",
        lastmodifieddate=str(int((wall - 28 * HOURS).timestamp() * 1000)))
    _sync(fake, wall - 27 * HOURS)
    gaps = boundary.detect_post_boundary_gaps(apply=True, history_budget=0)
    assert gaps["ok"] and gaps["unresolved_post_boundary_incidents"] == 1
    assert db.rows("SELECT contact_id, status FROM sql_post_boundary_incident") \
        == [{"contact_id": held, "status": "open"}]
    return fake, held


@needs_pg
def test_53_a_contact_with_an_open_post_boundary_incident_is_held_not_removed(db):
    """Review F3, the reviewer's probe A. Both incident resolvers read the
    funnel row, so removing a contact with an open post-boundary SQL incident
    left that incident open for ever. Such a contact stays in the funnel,
    keeps its strikes, is counted as held, and the run is not a success."""
    from services import sql_coverage_boundary_service as boundary

    wall = datetime.now(tz=timezone.utc)
    fake, held = _portal_with_an_open_incident(db, wall)

    fake.delete(_cid(137), held)
    first = _day(fake, wall - 21 * HOURS)
    assert first["newly_missing"] == 2
    second = _day(fake, wall)

    assert list(_funnel_rows(db, [_cid(137), held])) == [held], \
        "a contact with an open incident was removed"
    assert (second["would_remove"], second["held_open_incident"],
            second["removed"]) == (1, 1, 1)
    assert second["status"] == "partial"
    assert "1 contact(s) HubSpot no longer returns were NOT removed" \
        in second["error"]
    assert "open post-boundary SQL incident" in second["error"]
    assert list(_funnel_rows(db, [_cid(137), held])) == [held]
    assert db.strikes()[held]["checks"] == 2            # strikes kept
    assert db.rows("SELECT contact_id, status FROM sql_post_boundary_incident") \
        == [{"contact_id": held, "status": "open"}]
    row = db.run_row(second["reconciliation_run_id"])
    assert (row["status"], row["held_open_incident"], row["removed"]) \
        == ("partial", 1, 1)
    # The resolvers can still see it: the incident store is consistent.
    again = boundary.detect_post_boundary_gaps(apply=True, history_budget=0)
    assert again["ok"] and again["unresolved_post_boundary_incidents"] == 1
    assert not again.get("open_incidents_outside_population")

    # It stays held, and reported, on every later run.
    third = _day(fake, wall + 24 * HOURS)
    assert (third["status"], third["held_open_incident"], third["removed"]) \
        == ("partial", 1, 0)
    assert db.funnel_count() == 399


@needs_pg
def test_53b_a_held_contact_does_not_count_against_the_limit_for_the_others(
        db, monkeypatch, tmp_path):
    """A held contact is never removed, so it is not part of what the valve
    weighs: with a limit of one, the one removable contact still goes."""
    wall = datetime.now(tz=timezone.utc)
    fake, held = _portal_with_an_open_incident(db, wall)
    fake.delete(_cid(137), held)
    _day(fake, wall - 21 * HOURS)
    _use_config(monkeypatch, tmp_path, max_removals_per_run=1,
                max_removal_fraction=0.5)
    second = _day(fake, wall)
    assert (second["status"], second["backlog_qualified"],
            second["held_open_incident"], second["removed"]) \
        == ("partial", 1, 1, 1)
    assert list(_funnel_rows(db, [_cid(137), held])) == [held]


@needs_pg
def test_52b_a_row_ingested_after_its_strike_was_written_is_not_moved(db):
    """The same guard at the move itself: the strike of this run is already
    recorded when the real sync ingests the contact again. The move must
    still refuse the row."""
    from db import writers as db_writers

    fake = _portal(300)
    _load(fake)
    x = _cid(42)
    fake.delete(x)
    _day(fake, DAY_1)
    run = db_writers.start_contact_reconciliation_run()
    struck = db_writers.record_contact_absence(
        run, missing_ids=[x], returned_ids=[], observed_at=DAY_1 + 24 * HOURS)
    assert (struck["missing"], struck["newly_missing"]) == (1, 0)
    fake.restore(x, modified_ms=BASE_MS + 20_000_000)
    _sync(fake, DAY_1 + 24 * HOURS)

    moved = db_writers.move_absent_contacts(
        run, min_hours_between_strikes=20, max_removals=50, max_fraction=1,
        every_batch_answered=True, confirmed_absent_ids=[x])
    assert moved["ok"] and (moved["would_remove"], moved["removed"]) == (0, 0)
    assert list(_funnel_rows(db, [x])) == [x]


@needs_pg
def test_54_a_committed_removal_is_on_its_run_row_even_when_the_finishing_write_is_lost(
        db, monkeypatch):
    """Review F7. The move commits, then the write that closes the run fails.
    The run row used to stay ``running`` with NULL counts while archive rows
    existed under it."""
    from db import writers as db_writers

    fake = _portal(300)
    _load(fake)
    fake.delete(_cid(9))
    _day(fake, DAY_1)
    monkeypatch.setattr(db_writers, "finish_contact_reconciliation_run",
                        lambda *a, **k: False)
    result = _day(fake, DAY_1 + 24 * HOURS)

    assert result["status"] == "failed"
    assert result["run_record_write_failed"] is True
    assert result["removed"] == 1                 # the result does not hide it
    assert db.scalar("SELECT COUNT(*) FROM hubspot_contact_funnel_removed "
                     "WHERE reconciliation_run_id = %s",
                     (result["reconciliation_run_id"],)) == 1
    row = db.run_row(result["reconciliation_run_id"])
    assert row["status"] == "running" and row["finished_at"] is None
    assert (row["would_remove"], row["removed"],
            row["classification_rows_removed"]) == (1, 1, 0)
    assert (row["backlog_qualified"], row["held_open_incident"]) == (1, 0)


# ═════════════════════════════════════════════════════════════════════════════
# The second review (e190bdf): the direct read, and the trailing window
# ═════════════════════════════════════════════════════════════════════════════
def test_60_the_direct_read_tells_absent_merged_existing_and_unknown_apart(
        monkeypatch):
    waits: list = []
    monkeypatch.setattr(hp.time, "sleep", waits.append)
    fake = _portal(20)
    fake.delete(_cid(1))                                   # really deleted
    fake.merge(_cid(2), _cid(10))                          # merged, survivor names it
    fake.merge(_cid(3), _cid(11), survivor_names_it=False) # a different record
    fake.direct_faults[_cid(5)] = "error"
    fake.direct_faults[_cid(6)] = "rate_limited"
    fake.direct_faults[_cid(7)] = "no_id"
    paces: list = []
    asked = [_cid(n) for n in (1, 2, 3, 4, 5, 8, 6, 9, 7)] + ["007", "x1"]
    out = hp.confirm_contacts_absent(asked, client=fake.client,
                                     sleep=paces.append)

    assert out["absent"] == [_cid(1)]
    assert out["merged_away"] == [_cid(2)]
    assert out["existing"] == [_cid(4), _cid(8), _cid(9)]
    # A different record that does not name the id, an error, an exhausted
    # rate limit, a record with no id, and ids that cannot be asked about:
    # none of them is a confirmation, and none of them is "exists".
    assert out["unknown"] == [_cid(3), _cid(5), _cid(6), _cid(7), "007", "x1"]
    assert out["direct_reads"] == 9
    assert [r[0] for r in fake.direct_requests].count(_cid(6)) \
        == hp.MAX_RETRIES + 1
    assert len(waits) == hp.MAX_RETRIES                    # the 429 backoff
    assert paces == [hp.CONTACT_EXISTENCE_PACING_SECONDS] * 8
    assert all(props == ["hs_object_id", "hs_merged_object_ids"]
               and kwargs == {"_request_timeout":
                              hp.HUBSPOT_REQUEST_TIMEOUT_SECONDS}
               for _cid_, props, kwargs in fake.direct_requests)
    assert set(fake.calls) == {GET}

    # Three unknown answers in a row end the reading.
    fake = _portal(20)
    for n in range(10):
        fake.direct_faults[_cid(n)] = "error"
    out = hp.confirm_contacts_absent([_cid(n) for n in range(10)],
                                     client=fake.client, sleep=lambda s: None)
    assert out["direct_reads"] == 3 and len(out["unknown"]) == 10
    assert out["absent"] == [] and out["existing"] == []


@needs_pg
def test_61_a_search_index_that_keeps_omitting_the_same_live_contacts_removes_none_of_them(
        db):
    """Review N1, the reviewer's probe B2 scaled down. Every run is fully
    answered and well-formed, and every run the index leaves the same four
    live contacts out. On e190bdf days 2, 4, 6 and 8 each removed four live
    contacts as ``success``. A direct read of each finds it: nothing leaves,
    the strikes are cleared, and the run says the search is wrong."""
    fake = _portal(400)
    _load(fake)
    fake.omitted = {_cid(n) for n in range(4)}

    runs = []
    for day in range(8):
        run = _day(fake, DAY_1 + day * 24 * HOURS)
        assert run["removed"] == 0, f"day {day + 1} removed live contacts"
        assert db.funnel_count() == 400
        runs.append(run)
    assert [(r["status"], r["search_disagreements"]) for r in runs] \
        == [("success", 0), ("partial", 4)] * 4
    assert "HubSpot's search did not return 4 contact(s) that a direct read " \
        "by id then FOUND" in run["error"]
    assert db.strikes() == {}                      # cleared, to start again
    assert db.run_row(run["reconciliation_run_id"])["search_disagreements"] == 4
    assert db.scalar("SELECT COUNT(*) FROM hubspot_contact_funnel_removed") == 0
    assert all(cid in fake.contacts for cid in fake.omitted)


@needs_pg
def test_62_a_refused_backlog_that_shrinks_under_the_limit_is_still_not_removed_if_it_exists(
        db, monkeypatch, tmp_path):
    """Review N1, probe B3 scaled down (limit 10 per run). Forty live contacts
    are omitted and the removal is refused; the index then recovers for all
    but four. Four is under every limit — e190bdf removed them as
    ``success``."""
    fake = _portal(400)
    _load(fake)
    _use_config(monkeypatch, tmp_path, max_removals_per_run=10,
                max_removal_fraction=0.5)
    big = {_cid(n) for n in range(100, 140)}
    small = {_cid(n) for n in range(100, 104)}
    runs = []
    for day, omitted in enumerate([big, big, big, small, small]):
        fake.omitted = omitted
        run = _day(fake, DAY_1 + day * 24 * HOURS)
        assert run["removed"] == 0, f"day {day + 1} removed live contacts"
        runs.append(run)
    seen = [(r["status"], r["backlog_qualified"], r["search_disagreements"])
            for r in runs]
    assert seen[1][:2] == ("refused", 40) and seen[2][:2] == ("refused", 40)
    assert seen[3] == ("partial", 0, 4)            # found by the direct read
    assert len(fake.direct_requests) == 4          # only once under the limit
    assert db.funnel_count() == 400


@needs_pg
def test_63_a_contact_merged_away_is_removed_only_when_its_survivor_names_it(db):
    fake = _portal(400)
    _load(fake)
    named, unnamed, deleted = _cid(20), _cid(120), _cid(220)
    fake.merge(named, _cid(21))
    fake.merge(unnamed, _cid(121), survivor_names_it=False)
    fake.delete(deleted)
    _day(fake, DAY_1)
    second = _day(fake, DAY_1 + 24 * HOURS)

    assert list(_funnel_rows(db, [named, unnamed, deleted])) == [unnamed], \
        "a record that does not name the id is not proof of a merge"
    assert (second["would_remove"], second["direct_reads"],
            second["confirmed_absent"], second["unconfirmed"],
            second["removed"]) == (3, 3, 2, 1, 2)
    assert second["status"] == "partial"
    assert "1 contact(s) could not be confirmed absent by a direct read" \
        in second["error"]
    assert "does not name" in second["error"]
    assert db.strikes()[unnamed]["checks"] == 2    # kept, not cleared
    # The survivors are untouched.
    assert sorted(_funnel_rows(db, [_cid(21), _cid(121)])) \
        == sorted([_cid(21), _cid(121)])


@needs_pg
def test_64_an_unknown_direct_read_keeps_that_contact_and_does_not_block_the_others(
        db):
    fake = _portal(400)
    _load(fake)
    blind, plain = _cid(30), _cid(130)
    fake.delete(blind, plain)
    fake.direct_faults[blind] = "error"
    _day(fake, DAY_1)
    second = _day(fake, DAY_1 + 24 * HOURS)
    assert list(_funnel_rows(db, [blind, plain])) == [blind], \
        "a contact whose direct read failed was removed"
    assert (second["status"], second["would_remove"], second["confirmed_absent"],
            second["unconfirmed"], second["removed"]) == ("partial", 2, 1, 1, 1)
    assert db.strikes()[blind]["checks"] == 2

    # The read answers the next day: 404, and the contact goes.
    fake.direct_faults.clear()
    third = _day(fake, DAY_1 + 48 * HOURS)
    assert (third["status"], third["removed"]) == ("success", 1)
    assert db.funnel_count() == 398


@needs_pg
def test_65_a_contact_the_sync_saw_but_could_not_write_is_caught_by_the_direct_read(
        db):
    """Review C2, the reviewer's probe. During the day-2 check the sync
    delivers the contact with an OLDER modification time than the stored row:
    the funnel writer rejects it as stale, ``last_ingested_at`` does not move,
    and the ingest-time guard cannot see it. The contact exists. e190bdf
    removed it."""
    from db import writers as db_writers

    fake = _portal(300)
    _load(fake)
    x = _cid(42)
    stored = _funnel_rows(db, [x])[x]
    fake.delete(x)
    _day(fake, DAY_1)

    def racing_check(contact_ids, *, client=None):
        answer = hp.check_contacts_exist(contact_ids, client=client)
        assert x in answer["missing"]
        row = {k: stored.get(k) for k in db_writers._CONTACT_FUNNEL_COLUMNS}
        row["last_modified_at"] = stored["last_modified_at"] - timedelta(seconds=1)
        write = db_writers.upsert_hubspot_contact_funnel([row])
        assert write == {"ok": True, "attempted": 1, "persisted": 0,
                         "error": None}                    # rejected as stale
        fake.restore(x, modified_ms=int(
            row["last_modified_at"].timestamp() * 1000))
        return answer
    second = _reconcile(fake, DAY_1 + 24 * HOURS, existence_check=racing_check)

    assert x in fake.contacts
    assert list(_funnel_rows(db, [x])) == [x], "a contact that exists was removed"
    assert _funnel_rows(db, [x])[x]["last_ingested_at"] \
        == stored["last_ingested_at"]                      # the guard was blind
    assert (second["status"], second["direct_reads"], second["removed"],
            second["search_disagreements"]) == ("partial", 1, 0, 1)
    # Its strike is cleared before anything is counted, so it is no longer
    # something this run "would remove".
    assert second["would_remove"] == 0 and db.strikes() == {}


@needs_pg
def test_66_removals_over_a_trailing_week_are_capped_whatever_each_run_removes(
        db, monkeypatch, tmp_path):
    """Review N1(b). Two contacts were removed this week and the window allows
    two. A third real deletion is under the per-run limit and is confirmed by
    the direct read — and is refused, until the week has passed."""
    fake = _portal(400)
    _load(fake)
    _use_config(monkeypatch, tmp_path, max_removals_per_window=2,
                max_removal_window_fraction=0.5, removal_window_days=7)
    fake.delete(_cid(1), _cid(2))
    _day(fake, DAY_1)
    first = _day(fake, DAY_1 + 24 * HOURS)
    assert (first["status"], first["removed"]) == ("success", 2)

    fake.delete(_cid(3))
    _day(fake, DAY_1 + 48 * HOURS)
    refused = _day(fake, DAY_1 + 72 * HOURS)
    assert db.funnel_count() == 398, "a third removal inside the week"
    assert (refused["status"], refused["would_remove"],
            refused["confirmed_absent"], refused["removed"]) \
        == ("refused", 1, 1, 0)
    assert "2 contacts were removed in the last 7 days and this run would " \
        "remove 1 more: over the limit of 2" in refused["refusal_reason"]
    assert db.funnel_count() == 398

    # The same, by the share of the table: 3 of 398 is over 0.5%.
    _use_config(monkeypatch, tmp_path, max_removals_per_window=100,
                max_removal_window_fraction=0.005, removal_window_days=7)
    by_share = _day(fake, DAY_1 + 96 * HOURS)
    assert (by_share["status"], by_share["removed"]) == ("refused", 0)
    assert "0.50% of the 398 stored" in by_share["refusal_reason"]

    # The archive's own clock is what the window reads. Eight days later:
    _use_config(monkeypatch, tmp_path, max_removals_per_window=2,
                max_removal_window_fraction=0.5, removal_window_days=7)
    db.execute("UPDATE hubspot_contact_funnel_removed "
               "SET removed_at = removed_at - interval '8 days'")
    allowed = _day(fake, DAY_1 + 120 * HOURS)
    assert (allowed["status"], allowed["removed"]) == ("success", 1)
    assert db.funnel_count() == 397


@needs_pg
def test_67_a_direct_read_that_does_not_account_for_its_candidates_authorises_nothing(
        db):
    fake = _portal(400)
    _load(fake)
    gone = [_cid(1), _cid(101)]
    fake.delete(*gone)
    _day(fake, DAY_1)

    def short_answer(contact_ids, *, client=None):
        return {"absent": [contact_ids[0]], "merged_away": [], "existing": [],
                "unknown": [], "direct_reads": 1, "errors": []}
    _sync(fake, DAY_1 + 24 * HOURS)
    result = _reconcile(fake, DAY_1 + 24 * HOURS,
                        absence_confirmation=short_answer)
    assert result["status"] == "failed"
    assert "direct read accounted for 1 of 2 removal candidates" in result["error"]
    assert db.funnel_count() == 400

    def stranger(contact_ids, *, client=None):
        return {"absent": list(contact_ids) + [_cid(300)], "merged_away": [],
                "existing": [], "unknown": [], "direct_reads": 3, "errors": []}
    result = _reconcile(fake, DAY_1 + 25 * HOURS, absence_confirmation=stranger)
    assert result["status"] == "failed" and db.funnel_count() == 400


# ═════════════════════════════════════════════════════════════════════════════
# The writers' own guarantees
# ═════════════════════════════════════════════════════════════════════════════
@needs_pg
def test_19_two_strikes_are_required_even_when_the_interval_is_zero(db):
    from db import writers as db_writers

    fake = _portal(300)
    _load(fake)
    run = db_writers.start_contact_reconciliation_run()
    assert db_writers.record_contact_absence(
        run, missing_ids=[_cid(5)], returned_ids=[])["newly_missing"] == 1
    moved = db_writers.move_absent_contacts(
        run, min_hours_between_strikes=0, max_removals=50, max_fraction=1,
        every_batch_answered=True)
    assert moved["ok"] and (moved["would_remove"], moved["removed"]) == (0, 0)
    assert db.funnel_count() == 300

    # The same contact, found missing a second time by a second run: now two.
    run_2 = db_writers.start_contact_reconciliation_run()
    db_writers.record_contact_absence(run_2, missing_ids=[_cid(5)],
                                      returned_ids=[])
    moved = db_writers.move_absent_contacts(
        run_2, min_hours_between_strikes=0, max_removals=50, max_fraction=1,
        confirmed_absent_ids=[_cid(5)])
    # The writer moves nothing unless its caller asserts every batch answered
    # — even a contact the direct read confirmed.
    assert (moved["would_remove"], moved["confirmed_absent"], moved["removed"]) \
        == (1, 1, 0)
    # … and nothing without the direct read's confirmation.
    moved = db_writers.move_absent_contacts(
        run_2, min_hours_between_strikes=0, max_removals=50, max_fraction=1,
        every_batch_answered=True)
    assert (moved["would_remove"], moved["confirmed_absent"], moved["removed"]) \
        == (1, 0, 0)
    moved = db_writers.move_absent_contacts(
        run_2, min_hours_between_strikes=0, max_removals=50, max_fraction=1,
        every_batch_answered=True, confirmed_absent_ids=[_cid(5), _cid(6)])
    assert (moved["would_remove"], moved["confirmed_absent"], moved["removed"]) \
        == (1, 1, 1)


@needs_pg
def test_20_the_counts_reported_are_what_the_database_wrote_not_what_was_asked(
        db):
    from db import writers as db_writers

    fake = _portal(300)
    _load(fake)
    run = db_writers.start_contact_reconciliation_run()
    # Three ids are called missing; only two are stored contacts. Two ids are
    # called returned; neither carries a strike.
    out = db_writers.record_contact_absence(
        run, missing_ids=[_cid(1), _cid(2), "5550001"],
        returned_ids=[_cid(3), _cid(4)])
    assert out == {"ok": True, "missing": 2, "newly_missing": 2, "returned": 0,
                   "table_rows": 300, "error": None}
    assert sorted(db.strikes()) == [_cid(1), _cid(2)]

    run_2 = db_writers.start_contact_reconciliation_run()
    out = db_writers.record_contact_absence(
        run_2, missing_ids=[_cid(1)], returned_ids=[_cid(2), _cid(3)])
    assert (out["missing"], out["newly_missing"], out["returned"]) == (1, 0, 1)

    # A contact that is no longer stored is neither eligible nor counted,
    # whatever strikes it still carries: `would_remove` and `removed` are the
    # database's own counts over the rows that exist.
    db.execute("UPDATE hubspot_contact_absence SET first_missing_at = "
               "first_missing_at - interval '30 hours'")
    db.execute("DELETE FROM hubspot_contact_funnel WHERE contact_id = %s",
               (_cid(1),))
    moved = db_writers.move_absent_contacts(
        run_2, min_hours_between_strikes=20, max_removals=50, max_fraction=1,
        every_batch_answered=True)
    assert moved["ok"] and (moved["would_remove"], moved["removed"]) == (0, 0)
    assert db.scalar("SELECT COUNT(*) FROM hubspot_contact_funnel_removed") == 0

    # An unrecognised status is recorded as failed, never as itself.
    assert db_writers.finish_contact_reconciliation_run(
        run_2, status="looks_fine", removed=0) is True
    assert db.run_row(run_2)["status"] == "failed"
    # And a run that does not exist was not "finished".
    assert db_writers.finish_contact_reconciliation_run(
        987_654, status="success") is False


@needs_pg
def test_21_a_strike_is_stamped_by_the_database_after_the_hubspot_read(db):
    """Production passes no clock: the strike instant is the database's
    ``clock_timestamp()``, taken after HubSpot answered."""
    fake = _portal(300)
    _load(fake)
    fake.delete(_cid(9))
    asked_at: list = []
    real_search = fake.search

    def stamping_search(req, kwargs):
        response = real_search(req, kwargs)
        asked_at.append(db.scalar("SELECT clock_timestamp()"))
        return response
    fake.search = stamping_search

    result = _reconcile(fake, None)
    assert result["status"] == "success" and result["newly_missing"] == 1
    strike = db.strikes()[_cid(9)]
    assert strike["first_missing_at"] == strike["last_missing_at"]
    assert strike["first_missing_at"] > max(asked_at)
    assert strike["last_run_id"] == result["reconciliation_run_id"]
    row = db.run_row(result["reconciliation_run_id"])
    assert row["started_at"] < min(asked_at) < row["finished_at"]


@needs_pg
def test_22_the_archive_tables_carry_every_column_of_the_tables_they_archive(db):
    """A column added to ``hubspot_contact_funnel`` (or to the classification
    table) and not to its archive would be dropped on removal. This fails
    first."""
    from db import writers as db_writers

    def columns(table):
        return {r["column_name"]: r["data_type"] for r in db.rows(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s",
            (table,))}

    funnel, archive = columns("hubspot_contact_funnel"), \
        columns("hubspot_contact_funnel_removed")
    assert funnel and set(funnel) <= set(archive)
    assert {c: archive[c] for c in funnel} == funnel
    assert set(archive) - set(funnel) == {
        "removal_id", "removed_at", "removal_reason", "reconciliation_run_id",
        "first_missing_at", "last_missing_at", "absence_checks"}
    assert set(db_writers._CONTACT_FUNNEL_ARCHIVE_COLUMNS) == set(funnel)

    source, source_archive = columns("contact_source_classification"), \
        columns("contact_source_classification_removed")
    assert source and {c: source_archive[c] for c in source} == source
    assert set(source_archive) - set(source) == {
        "removal_id", "removed_at", "removal_reason", "reconciliation_run_id"}
    assert set(db_writers._CLASSIFICATION_ARCHIVE_COLUMNS) == set(source)

    # Nothing in the archive can stop a contact being removed twice.
    assert db.scalar("""
        SELECT COUNT(*) FROM pg_indexes
         WHERE tablename = 'hubspot_contact_funnel_removed'
           AND indexdef ILIKE '%UNIQUE%' AND indexdef NOT ILIKE '%removal_id%'
    """) == 0


# ═════════════════════════════════════════════════════════════════════════════
# Failing closed
# ═════════════════════════════════════════════════════════════════════════════
@needs_pg
def test_23_when_no_batch_is_answered_the_run_fails_and_nothing_is_struck(db):
    fake = _portal(400)
    _load(fake)
    fake.delete(_cid(1))
    fake.fault_all = "unauthorized"
    result = _reconcile(fake, DAY_1)
    assert result["status"] == "failed"
    assert "no batch could be answered" in result["error"]
    assert (result["contacts_stored"], result["contacts_checked"],
            result["contacts_unknown"], result["batches_unknown"]) \
        == (400, 0, 400, 4)
    # Unknown is not zero.
    assert result["missing"] is None and result["removed"] is None
    assert db.strikes() == {} and db.funnel_count() == 400
    row = db.run_row(result["reconciliation_run_id"])
    assert (row["status"], row["missing"], row["removed"]) == ("failed", None, None)


def test_24_without_a_durable_run_record_hubspot_is_never_asked(monkeypatch):
    import db.connection as connection
    monkeypatch.setattr(connection, "_pool", None, raising=False)
    fake = _portal(10)
    result = _reconcile(fake, DAY_1)
    assert result["status"] == "failed"
    assert "run record could not be created" in result["error"]
    assert result["reconciliation_run_id"] is None
    assert result["removed"] is None and result["contacts_checked"] is None
    assert fake.calls == []


@needs_pg
def test_25_an_existence_check_that_does_not_account_for_every_id_changes_nothing(
        db):
    fake = _portal(300)
    _load(fake)

    def short_check(contact_ids, *, client=None):
        return {"existing": list(contact_ids)[:200], "missing": [_cid(250)],
                "unknown": [], "batches": 3, "batches_unknown": 0,
                "search_calls": 3, "errors": []}
    result = _reconcile(fake, DAY_1, existence_check=short_check)
    assert result["status"] == "failed"
    assert "accounted for 201 of 300" in result["error"]
    assert db.strikes() == {} and db.funnel_count() == 300

    def exploding_check(contact_ids, *, client=None):
        raise RuntimeError("HUBSPOT_API_KEY is not set")
    result = _reconcile(fake, DAY_1, existence_check=exploding_check)
    assert result["status"] == "failed"
    assert "HUBSPOT_API_KEY is not set" in result["error"]
    assert db.run_row(result["reconciliation_run_id"])["status"] == "failed"


# ═════════════════════════════════════════════════════════════════════════════
# Inside the daily incremental sync
# ═════════════════════════════════════════════════════════════════════════════
def _stub_the_other_steps(monkeypatch, sync):
    """Every step except the contact sync and the removed-contact check is
    replaced by a green stub: this module is about those two, and the others
    would reach Google Ads and HubSpot."""
    ran: list = []
    for name, fn in inspect.getmembers(sync, inspect.isfunction):
        if name in ("_sync_contact_funnel", "_sync_contact_reconciliation"):
            continue
        if name.startswith(("_sync_", "_publish_", "_detect_")):
            def stub(*, _name=name, **kwargs):
                ran.append(_name)
                return {"status": "success"}
            monkeypatch.setattr(sync, name, stub)
    return ran


def _a_day_passes(db):
    """The scheduler takes no clock, so the 20 hours between two mornings are
    made by moving every recorded check 21 hours back."""
    if db.scalar("SELECT to_regclass('hubspot_contact_absence')") is None:
        return                 # the parent commit: there is no check to age
    db.execute("UPDATE hubspot_contact_absence SET "
               "first_missing_at = first_missing_at - interval '21 hours', "
               "last_missing_at = last_missing_at - interval '21 hours'")


@needs_pg
def test_30_two_daily_syncs_after_hubspot_deletes_contacts_leave_the_funnel_without_them(
        db, monkeypatch):
    """THE DEFECT, end to end as the scheduler runs it, on nothing but code
    that existed before the fix: the real ``run_daily_incremental_sync`` twice,
    a day apart, then the real channel-funnel read.

    On the parent commit this fails on the first count below: the funnel still
    holds 300 contacts and the channel funnel still publishes 300, three of
    which HubSpot no longer has.
    """
    import scheduler.incremental_sync as sync

    fake = _portal(300, SQL_CONTACT)
    # The scheduler stamps the wall clock, so this test loads on it too.
    wall_clock = datetime.now(tz=timezone.utc)
    _load(fake, wall_clock - 3 * HOURS)
    monkeypatch.setattr(hp, "get_client", lambda: fake.client)
    _stub_the_other_steps(monkeypatch, sync)
    gone = [_cid(n) for n in (3, 137, 250)]
    assert _channel_totals(wall_clock)["contacts"] == 300

    fake.delete(*gone)
    first = sync.run_daily_incremental_sync(run_reason="test")
    assert first["status"] == "success", first["errors"]
    # One morning is one strike: nothing has left yet.
    assert db.funnel_count() == 300

    _a_day_passes(db)
    second = sync.run_daily_incremental_sync(run_reason="test")
    assert second["status"] == "success", second["errors"]

    assert db.funnel_count() == 297
    assert _funnel_rows(db, gone) == {}
    totals = _channel_totals(datetime.now(tz=timezone.utc))
    assert totals["contacts"] == 297 and totals["reached_sql"] == 0
    assert set(fake.calls) <= {SEARCH, GET}


@needs_pg
def test_31_the_check_runs_after_the_contact_sync_and_before_anything_reads_the_funnel(
        db, monkeypatch):
    import scheduler.incremental_sync as sync

    fake = _portal(300)
    _load(fake)
    monkeypatch.setattr(hp, "get_client", lambda: fake.client)
    _stub_the_other_steps(monkeypatch, sync)
    fake.delete(_cid(3), _cid(137), _cid(250))

    first = sync.run_daily_incremental_sync(run_reason="test")
    block = first["datasets"][sync.LABEL_CONTACT_RECONCILIATION]
    assert sync.LABEL_CONTACT_RECONCILIATION == "hubspot/contact_reconciliation"
    assert (block["status"], block["contacts_checked"], block["missing"],
            block["removed"]) == ("success", 300, 3, 0)

    order = list(first["datasets"])
    assert order.index("hubspot/contact_funnel") \
        < order.index("hubspot/contact_reconciliation") \
        < order.index("hubspot/sql_coverage_gaps")
    # Nothing that could read the funnel is scheduled between the two.
    assert order.index("hubspot/contact_reconciliation") \
        == order.index("hubspot/contact_funnel") + 1

    _a_day_passes(db)
    second = sync.run_daily_incremental_sync(run_reason="test")
    block = second["datasets"]["hubspot/contact_reconciliation"]
    assert (block["status"], block["missing"], block["would_remove"],
            block["removed"]) == ("success", 3, 3, 3)
    assert block["hubspot_writes_performed"] is False
    # The check's own run row points at the scheduler run that ran it.
    assert db.run_row(block["reconciliation_run_id"])["run_id"] \
        == second["run_id"]


@needs_pg
def test_32_a_failing_check_does_not_abort_the_sync_and_the_run_is_recorded_partial_under_its_own_run_type(
        db, monkeypatch):
    import scheduler.incremental_sync as sync

    fake = _portal(300)
    _load(fake)
    monkeypatch.setattr(hp, "get_client", lambda: fake.client)
    ran = _stub_the_other_steps(monkeypatch, sync)
    fake.fault_all = "error"                    # every existence batch fails

    out = sync.run_daily_incremental_sync(run_reason="test")

    block = out["datasets"]["hubspot/contact_reconciliation"]
    assert block["status"] == "failed" and block["removed"] is None
    assert out["status"] == "partial"
    assert [e for e in out["errors"]
            if e.startswith("hubspot/contact_reconciliation: run ended 'failed'")]
    # The steps after it still ran.
    assert "_detect_sql_coverage_gaps" in ran and "_sync_mailchimp" in ran
    assert out["datasets"]["hubspot/contact_funnel"]["status"] == "success"
    assert db.funnel_count() == 300

    # The durable run row: the persisted run type, and partial — not failed,
    # not success.
    (row,) = db.rows("SELECT run_type, status, error_message FROM runs "
                     "WHERE id = %s", (out["run_id"],))
    assert row["run_type"] == "daily_incremental_sync" == sync.RUN_TYPE
    assert row["status"] == "partial"
    assert "hubspot/contact_reconciliation" in row["error_message"]


@needs_pg
@pytest.mark.parametrize("scenario,expected", [("refused", "refused"),
                                               ("partial", "partial")])
def test_33_a_refused_or_partial_check_is_an_error_on_the_run_not_a_green_step(
        db, monkeypatch, tmp_path, scenario, expected):
    import scheduler.incremental_sync as sync

    fake = _portal(300)
    _load(fake)
    monkeypatch.setattr(hp, "get_client", lambda: fake.client)
    _stub_the_other_steps(monkeypatch, sync)
    fake.delete(_cid(250))
    if scenario == "partial":
        fake.faults[_cid(0)] = "no_results"
    sync.run_daily_incremental_sync(run_reason="test")
    _a_day_passes(db)
    if scenario == "refused":
        _use_config(monkeypatch, tmp_path, max_removals_per_run=0)

    out = sync.run_daily_incremental_sync(run_reason="test")
    block = out["datasets"]["hubspot/contact_reconciliation"]
    assert block["status"] == expected
    assert out["status"] == "partial"
    assert [e for e in out["errors"] if e.startswith(
        f"hubspot/contact_reconciliation: run ended '{expected}'")]
    # Review F1: this used to assert that the PARTIAL run removed its contact
    # (299). That assertion encoded the defect — a run with an unanswered
    # batch removing the answered share. Neither a refused nor a partial run
    # removes anything.
    assert block["removed"] == 0 and block["would_remove"] == 1
    assert db.funnel_count() == 300


def test_34_a_check_that_raises_is_a_failed_step_with_unknown_counts(monkeypatch):
    import scheduler.incremental_sync as sync
    from services import hubspot_contact_reconciliation_service as service

    def boom(**kwargs):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(service, "run_contact_reconciliation", boom)
    errors: list = []
    block = sync._sync_contact_reconciliation(run_id=1, errors=errors)
    assert block["status"] == "failed"
    assert block["removed"] is None and block["contacts_checked"] is None
    assert errors == ["hubspot/contact_reconciliation: unexpected"]
    assert sync._overall_status({"a": {"status": "success"}, "b": block}) \
        == "partial"


# ═════════════════════════════════════════════════════════════════════════════
# Read-only governance, and the operator's view
# ═════════════════════════════════════════════════════════════════════════════
@needs_pg
def test_40_the_whole_path_calls_only_the_search_and_the_read_by_id(db):
    fake = _portal(300)
    _load(fake)
    fake.delete(_cid(4))
    first = _day(fake, DAY_1)
    assert first["direct_reads"] == 0 and fake.direct_requests == []
    removed = _day(fake, DAY_1 + 24 * HOURS)
    assert (removed["removed"], removed["direct_reads"],
            removed["confirmed_absent"]) == (1, 1, 1)
    assert removed["hubspot_writes_performed"] is False
    # Every call, across two syncs, two checks and a removal, was a search —
    # except the one direct read of the one contact that was removed.
    assert set(fake.calls) == {SEARCH, GET}
    assert fake.calls.count(GET) == 1
    assert fake.direct_requests == [
        (_cid(4), ["hs_object_id", "hs_merged_object_ids"],
         {"_request_timeout": hp.HUBSPOT_REQUEST_TIMEOUT_SECONDS})]
    for req, _kwargs in fake.existence_requests:
        (only,) = req["filterGroups"][0]["filters"]
        assert only["operator"] == "IN" and len(only["values"]) <= 100
        assert req["properties"] == ["hs_object_id"]


def test_41_no_write_call_exists_in_the_new_code():
    search = (inspect.getsource(hp.check_contacts_exist)
              + inspect.getsource(hp._existence_batch))
    direct = (inspect.getsource(hp.confirm_contacts_absent)
              + inspect.getsource(hp._direct_read_contact))
    for token in ("batch_api", "merge_api", "public_object_api",
                  ".merge(", ".create(", ".update(", ".archive(", ".upsert(",
                  ".purge(", "requests.", "gdpr"):
        assert token not in (search + direct).lower(), token
    # The only two HubSpot calls: the contacts search, and the read by id.
    assert search.count("client.crm.") == 1
    assert "client.crm.contacts.search_api.do_search" in search
    assert "basic_api" not in search
    assert direct.count("client.crm.") == 1
    assert "client.crm.contacts.basic_api.get_by_id(" in direct
    assert direct.count("client.crm.contacts.basic_api.") == 1

    # The service never holds a HubSpot client's API at all: it reaches
    # HubSpot through those two connector functions and nothing else.
    service = (ROOT / "services"
               / "hubspot_contact_reconciliation_service.py").read_text()
    for token in ("crm.", "search_api", "basic_api", "batch_api", "merge_api",
                  "requests", "import hubspot", "hubspot.client", "get_client"):
        assert token not in service.lower(), token
    imports = [line.strip() for line in service.splitlines()
               if "connectors." in line and "import" in line]
    assert imports == ["from connectors.hubspot_pull import (  # noqa: PLC0415"] * 2
    assert service.count("check_contacts_exist") == 3     # doc, import, use
    assert service.count("confirm_contacts_absent") == 3  # doc, import, use


@needs_pg
def test_42_the_system_overview_carries_the_latest_check_as_plain_facts(db):
    from services import system_overview_service as overview

    never = overview.build_system_overview(DAY_1)["contact_reconciliation"]
    assert never == {"available": True, "status": None, "reason": None,
                     "started_at": None, "finished_at": None,
                     "contacts_checked": None, "batches_unknown": None,
                     "would_remove": None, "removed": None,
                     "backlog_qualified": None, "held_open_incident": None,
                     "search_disagreements": None, "pending_strikes": 0}

    fake = _portal(300)
    _load(fake)
    fake.delete(_cid(4), _cid(150))
    _day(fake, DAY_1)
    block = overview.build_system_overview(DAY_1)["contact_reconciliation"]
    assert (block["status"], block["contacts_checked"], block["batches_unknown"],
            block["removed"], block["pending_strikes"]) == ("success", 300, 0, 0, 2)
    assert block["started_at"] and block["finished_at"]

    fake.faults[_cid(100)] = "error"
    _day(fake, DAY_1 + 24 * HOURS)
    block = overview.build_system_overview(DAY_1)["contact_reconciliation"]
    assert (block["status"], block["contacts_checked"], block["batches_unknown"],
            block["removed"], block["pending_strikes"]) == ("partial", 200, 1, 0, 2)
    # A run that is not `success` says why, in the run's own words.
    assert "removes nothing" in block["reason"]
    assert (block["would_remove"], block["backlog_qualified"],
            block["held_open_incident"], block["search_disagreements"]) \
        == (1, 1, 0, 0)


def test_43_an_unreadable_check_record_is_unavailable_not_never_run(monkeypatch):
    import db.connection as connection
    from services import system_overview_service as overview
    monkeypatch.setattr(connection, "_pool", None, raising=False)
    block = overview.contact_reconciliation_block()
    assert block["available"] is False
    assert block["removed"] is None and block["pending_strikes"] is None
