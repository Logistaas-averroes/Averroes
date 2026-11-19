"""PR-ADS-163A — a legacy HubSpot pull that could not read its result raises.

Five pulls in ``connectors/hubspot_pull.py`` logged a HubSpot error, stopped,
and returned what they had: ``[]`` when the first page failed, the first pages
when a later one did. A caller cannot tell that list from a complete one. With
an expired token the monthly job recorded ``success``, stamped
``hubspot/contacts`` fresh, overwrote ``data/crm_contacts.json`` with ``[]``
and stored a 0-of-0 GCLID coverage snapshot (PR-ADS-162H, "Known gaps" 1).

Every test here drives the REAL connector function. Only HubSpot is fake: an
in-memory portal that honours the filters each pull sends, pages by ``after``,
raises the exception class the SDK raises for the API that was called, and can
fail a chosen request once, a few times, or for good. No pull is stubbed, and
the callers (weekly, monthly, daily, incremental sync, backfill, revenue
recovery) are run through their real entry points.

A failed pull is asserted in two steps, so the pre-fix tree fails for the
reason that matters: first that the pull did not RETURN, then that what it
raised is the named ``HubSpotPullIncompleteError``.

What this does NOT prove: that a pull which returns has read every record.
These pulls page by offset and never read HubSpot's ``total``, so a record
deleted or re-sorted while the scan runs can still shift a page (the
PR-ADS-161I failure). The portal here does not change during a scan.
"""
from __future__ import annotations

import ast
import json
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
import urllib3
from hubspot.crm.contacts import exceptions as contact_errors
from hubspot.crm.deals import exceptions as deal_errors

from connectors import hubspot_pull as hp

ROOT = Path(__file__).resolve().parents[1]

TODAY = datetime.now(timezone.utc).date()
WINDOW_FROM = TODAY - timedelta(days=25)
WON_STAGE = "326093516"

#: The five pulls this slice changes.
PULLS = (
    "pull_paid_search_contacts",
    "pull_paid_search_contacts_in_range",
    "pull_deals_with_gclid",
    "pull_closed_won_deals_in_range",
    "pull_all_contacts_in_range",
)

#: The connector functions, captured when this module is imported — before any
#: test body has run, so before any test could have replaced one.
REAL = {name: getattr(hp, name) for name in PULLS + (
    "get_lead_quality_summary", "save_output",
    "pull_closed_won_deals_with_sources_in_range")}

#: The two properties the source read asks for. A contact read that asks for
#: exactly these is the SOURCE read; any other is the click-evidence
#: ("primary contact") read. The portal can fail one and not the other.
SOURCE_PROPERTIES = frozenset({"hs_analytics_source", "hs_analytics_source_data_1"})

#: A request that never succeeds is answered this many times and no more. The
#: pre-fix loops retried a 429 without limit; the ceiling turns "never returns"
#: into a failed assertion.
ATTEMPT_CEILING = 25


# ═════════════════════════════════════════════════════════════════════════════
# A HubSpot-like portal
# ═════════════════════════════════════════════════════════════════════════════
#: A search fault that is not an error: a 200 with no `results` list.
NO_RESULTS_LIST = object()


class Always:
    """A fault that does not clear: every attempt at the request fails."""

    def __init__(self, what):
        self.what = what


class _Obj:
    def __init__(self, payload):
        self._payload = payload

    def to_dict(self):
        return json.loads(json.dumps(self._payload))


class _Resp:
    def __init__(self, results, after, total):
        self.results = results
        self.paging = (SimpleNamespace(next=SimpleNamespace(after=after))
                       if after is not None else None)
        self.total = total


class _HttpResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"{self.status_code} Error for url", response=self)


_REASONS = {401: "Unauthorized", 404: "Not Found", 429: "Too Many Requests",
            500: "Internal Server Error"}


def _sdk_error(family: str, status: int):
    """The exception the SDK raises — the class depends on WHICH api was
    called. ``hubspot.crm.deals`` has its own ``ApiException``, unrelated to
    the contacts one, and the mapping below is the SDK's own (rest.py)."""
    mod = contact_errors if family == "contacts" else deal_errors
    cls = {401: mod.UnauthorizedException, 403: mod.ForbiddenException,
           404: mod.NotFoundException}.get(status)
    if cls is None:
        cls = mod.ServiceException if 500 <= status <= 599 else mod.ApiException
    return cls(status=status, reason=_REASONS.get(status, "Error"))


def _iso(day: date) -> str:
    return f"{day.isoformat()}T12:00:00.000Z"


def _ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, 12,
                        tzinfo=timezone.utc).timestamp() * 1000)


class FakeHubSpot:
    """An in-memory portal that answers the requests the legacy pulls send.

    * contact / deal search honours every filter it is sent (``EQ``, ``IN``,
      ``GTE``, ``LTE``), returns only the requested properties, pages by
      ``after`` and reports ``total``; an unknown filter is an error;
    * ``basic_api.get_by_id`` and the v4 association GETs answer by id;
    * nothing that writes exists: the client has no write api, and the
      ``requests`` verbs other than GET are replaced by a failing stub.

    ``faults`` maps a request to what it fails with, in order — ``[429, 429]``
    fails twice and then succeeds, ``Always(500)`` never succeeds, and an
    exception instance (a ``urllib3`` or ``requests`` transport error) is
    raised as it is. ``token_expired`` fails every request with 401.

    A contact read is addressable three ways: ``("contacts.get", id)`` for any
    read of that contact, ``("contacts.primary", id)`` for the click-evidence
    read only, ``("contacts.source", id)`` for the source read only.
    ``assoc_payload`` replaces the body of one association response, to serve
    a 200 that is not an association list.
    """

    PAID = [str(i) for i in range(1001, 1231)]        # 230, in the window
    ORGANIC = [str(i) for i in range(2001, 2041)]     # 40, in the window
    OLD_PAID = [str(i) for i in range(3001, 3016)]    # 15, 400 days old
    WON = [str(i) for i in range(5001, 5216)]         # 215, closed in window
    LOST = [str(i) for i in range(6001, 6011)]        # 10, closed in window
    OLD_WON = [str(i) for i in range(7001, 7011)]     # 10, closed 400 days ago

    def __init__(self):
        self.faults: dict = {}
        self.attempts: Counter = Counter()
        self.token_expired = False
        self.assoc_has_next_page: set = set()
        self.assoc_payload: dict = {}
        self.sleeps: list = []
        self.reads: list = []         # (family, id, kind) for every get_by_id
        self.timeouts: list = []      # (api, _request_timeout) per SDK call
        self.rows = {"contacts": {}, "deals": {}}
        self.date_ms = {"contacts": {}, "deals": {}}
        self.contact_deals: dict = {}
        self.deal_contacts: dict = {}

        for n, cid in enumerate(self.PAID, start=1):
            self._contact(cid, "PAID_SEARCH", TODAY - timedelta(days=n % 20 + 1),
                          gclid=f"G-{cid}" if n % 3 == 1 else None,
                          first_url=(f"https://logistaas.com/?gclid=URL-{cid}"
                                     if n % 3 == 2 else "https://logistaas.com/"))
        for cid in self.ORGANIC:
            self._contact(cid, "ORGANIC_SEARCH", TODAY - timedelta(days=5))
        for cid in self.OLD_PAID:
            self._contact(cid, "PAID_SEARCH", TODAY - timedelta(days=400),
                          gclid=f"G-{cid}")

        for n, did in enumerate(self.WON, start=1):
            self._deal(did, WON_STAGE, TODAY - timedelta(days=n % 20 + 1), n * 10)
            if n % 50:                      # every 50th won deal has no contact
                self._associate(self.PAID[n - 1], did)
        for did in self.LOST:
            self._deal(did, "379124201", TODAY - timedelta(days=3), 5)
        for did in self.OLD_WON:
            self._deal(did, WON_STAGE, TODAY - timedelta(days=400), 5)
        # An open deal as well, for every sixth paid contact: some GCLID
        # contacts have two deals, some one, some none.
        for n, cid in enumerate(self.PAID, start=1):
            if n % 6 == 1:
                did = str(8000 + n)
                self._deal(did, "qualifiedtobuy", TODAY + timedelta(days=30), 77)
                self._associate(cid, did)

        # `_request_timeout` is the SDK's own keyword; every generated api
        # method accepts it. Recorded, so a call without one is visible.
        search = lambda family: SimpleNamespace(      # noqa: E731
            do_search=lambda public_object_search_request=None,
            _request_timeout=None:
            self._search(family, public_object_search_request, _request_timeout))
        self.client = SimpleNamespace(crm=SimpleNamespace(
            contacts=SimpleNamespace(
                search_api=search("contacts"),
                basic_api=SimpleNamespace(
                    get_by_id=lambda contact_id=None, properties=None,
                    _request_timeout=None:
                    self._get("contacts", contact_id, properties,
                              _request_timeout))),
            deals=SimpleNamespace(
                search_api=search("deals"),
                basic_api=SimpleNamespace(
                    get_by_id=lambda deal_id=None, properties=None,
                    _request_timeout=None:
                    self._get("deals", deal_id, properties, _request_timeout))),
        ))

    # ── data ────────────────────────────────────────────────────────────────
    def _contact(self, cid, source, created, gclid=None, first_url=None):
        self.rows["contacts"][cid] = {
            "firstname": f"First{cid}", "lastname": f"Last{cid}",
            "email": f"contact{cid}@example.test", "company": f"Company {cid}",
            "hs_google_click_id": gclid, "mql_status": "OPEN-Connecting",
            "hs_lead_status": "NEW", "lifecyclestage": "lead",
            "hs_analytics_source": source,
            "hs_analytics_source_data_1": f"campaign-{cid}",
            "hs_analytics_source_data_2": f"keyword-{cid}",
            "hs_analytics_first_url": first_url,
            "ip_country": "gb", "country": "United Kingdom",
            "createdate": _iso(created),
        }
        self.date_ms["contacts"][cid] = {"createdate": _ms(created)}

    def _deal(self, did, stage, closed, amount):
        self.rows["deals"][did] = {
            "dealname": f"Deal {did}", "dealstage": stage, "amount": str(amount),
            "closedate": _iso(closed), "createdate": _iso(closed - timedelta(days=30)),
            "pipeline": "default", "hs_deal_stage_probability": "0.5",
        }
        self.date_ms["deals"][did] = {"closedate": _ms(closed)}

    def _associate(self, cid, did):
        self.contact_deals.setdefault(cid, []).append(did)
        self.deal_contacts.setdefault(did, []).append(cid)

    def public(self, family, oid, properties) -> dict:
        row = self.rows[family][oid]
        return {"id": oid, "properties": {p: row.get(p) for p in properties}}

    # ── faults ──────────────────────────────────────────────────────────────
    def _fault(self, key):
        self.attempts[key] += 1
        if self.token_expired:
            return 401
        spec = self.faults.get(key)
        if isinstance(spec, Always):
            if self.attempts[key] > ATTEMPT_CEILING:
                raise AssertionError(
                    f"{key}: {self.attempts[key]} attempts at a request that "
                    "never succeeds — the retry is not bounded")
            return spec.what
        if spec:
            return spec.pop(0)
        return None

    # ── the API ─────────────────────────────────────────────────────────────
    def _matches(self, family, oid, filters) -> bool:
        row, dates = self.rows[family][oid], self.date_ms[family][oid]
        for f in filters:
            prop, op = f["propertyName"], f["operator"]
            if op == "EQ":
                ok = str(row.get(prop)) == f["value"]
            elif op == "IN":
                ok = row.get(prop) in f["values"]
            elif op in ("GTE", "LTE"):
                value = int(f["value"])
                ok = dates[prop] >= value if op == "GTE" else dates[prop] <= value
            else:
                raise AssertionError(f"unexpected filter {f}")
            if not ok:
                return False
        return True

    @staticmethod
    def _fail(family, fault):
        """Raise what the SDK would: a transport error as it is, a status as
        the api package's own exception class."""
        if isinstance(fault, BaseException):
            raise fault
        raise _sdk_error(family, fault)

    def _search(self, family, req, timeout=None):
        self.timeouts.append((f"{family}.search", timeout))
        limit, offset = int(req["limit"]), int(req.get("after") or 0)
        fault = self._fault((f"{family}.search", offset // limit))
        if fault is NO_RESULTS_LIST:
            # What SDK 13.0.0 hands back for a 200 whose body has no `results`
            # key (for example `{"total": 0}`): a response with results=None.
            return _Resp(None, None, 0)
        if fault is not None:
            self._fail(family, fault)
        (group,) = req["filterGroups"]
        ids = [oid for oid in self.rows[family]
               if self._matches(family, oid, group["filters"])]
        page = ids[offset:offset + limit]
        nxt = offset + limit
        return _Resp([_Obj(self.public(family, oid, req["properties"]))
                      for oid in page],
                     str(nxt) if nxt < len(ids) else None, len(ids))

    def _get(self, family, oid, properties, timeout=None):
        self.timeouts.append((f"{family}.get", timeout))
        kind = "deal"
        if family == "contacts":
            kind = "source" if set(properties) == SOURCE_PROPERTIES else "primary"
        self.reads.append((family, str(oid), kind))
        fault = self._fault((f"{family}.get", str(oid)))
        if fault is None and family == "contacts":
            fault = self._fault((f"contacts.{kind}", str(oid)))
        if fault is not None:
            self._fail(family, fault)
        if str(oid) not in self.rows[family]:
            raise _sdk_error(family, 404)
        return _Obj(self.public(family, str(oid), properties))

    def http_get(self, url, headers=None, timeout=None, **kwargs):
        match = re.fullmatch(
            r"https://api\.hubapi\.com/crm/v4/objects/(contacts|deals)/([^/]+)"
            r"/associations/(deals|contacts)", url)
        assert match, f"unexpected GET {url}"
        family, oid, _to = match.groups()
        fault = self._fault(("assoc", family, oid))
        if isinstance(fault, BaseException):
            raise fault
        if fault is not None:
            return _HttpResponse(fault, {"status": "error"})
        if (family, oid) in self.assoc_payload:
            return _HttpResponse(200, self.assoc_payload[(family, oid)])
        linked = (self.contact_deals if family == "contacts"
                  else self.deal_contacts).get(oid, [])
        payload = {"results": [{"toObjectId": int(i), "associationTypes": []}
                               for i in linked]}
        if (family, oid) in self.assoc_has_next_page:
            payload["paging"] = {"next": {"after": "500"}}
        return _HttpResponse(200, payload)

    @staticmethod
    def forbidden_write(*args, **kwargs):
        raise AssertionError("a HubSpot write was attempted — read-only governance")

    # ── what a complete pull returns, restated independently ────────────────
    def expected_contacts(self, ids) -> list:
        return [self.public("contacts", cid, hp.CONTACT_PROPERTIES) for cid in ids]

    def paid_contacts(self) -> list:
        return self.expected_contacts(self.PAID)

    def gclid_contact_ids(self) -> list:
        return [cid for cid in self.PAID
                if self.rows["contacts"][cid]["hs_google_click_id"]]

    def expected_gclid_deals(self, contact_ids=None) -> list:
        labels = {WON_STAGE: "Deal Won / Payment Received",
                  "qualifiedtobuy": "Proposal / Implementation Plan"}
        out = []
        for cid in (contact_ids or self.gclid_contact_ids()):
            for did in self.contact_deals.get(cid, []):
                row = self.public("deals", did, [
                    "dealname", "dealstage", "amount", "closedate", "createdate",
                    "pipeline", "hs_deal_stage_probability"])
                row["gclid"] = self.rows["contacts"][cid]["hs_google_click_id"]
                row["contact_id"] = cid
                row["stage_label"] = labels[row["properties"]["dealstage"]]
                out.append(row)
        return out

    def expected_sources(self) -> list:
        """What ``pull_closed_won_deals_with_sources_in_range`` returns when
        every read answers: each won deal with every associated contact's
        source, and no failed lookup."""
        out = []
        for did in self.WON:
            deal = self.rows["deals"][did]
            out.append({
                "deal_id": did,
                "deal_close_date": deal["closedate"],
                "deal_amount_usd": deal["amount"],
                "lookup_failed": False,
                "contacts": [{
                    "contact_id": cid,
                    "source_primary": self.rows["contacts"][cid]["hs_analytics_source"],
                    "source_detail":
                        self.rows["contacts"][cid]["hs_analytics_source_data_1"],
                } for cid in self.deal_contacts.get(did, [])],
            })
        return out

    def expected_won_rows(self) -> list:
        out = []
        for did in self.WON:
            deal = self.rows["deals"][did]
            linked = self.deal_contacts.get(did, [])
            contact = self.rows["contacts"][linked[0]] if linked else {}
            direct = contact.get("hs_google_click_id")
            first_url = contact.get("hs_analytics_first_url") or ""
            if direct:
                gclid, source = direct, "gclid"
            elif "gclid=" in first_url:
                gclid, source = first_url.split("gclid=")[1], "first_url"
            else:
                gclid, source = None, None
            out.append({
                "deal_id": did,
                "contact_id": linked[0] if linked else None,
                "gclid": gclid, "match_source": source,
                "first_url": first_url or None,
                "campaign_name": contact.get("hs_analytics_source_data_1"),
                "keyword": contact.get("hs_analytics_source_data_2"),
                "country": contact.get("ip_country"),
                "company": contact.get("company"),
                "deal_close_date": deal["closedate"],
                "deal_amount_usd": deal["amount"],
                "deal_stage": WON_STAGE,
                "deal_stage_label": "Deal Won / Payment Received",
            })
        return out


@pytest.fixture(autouse=True)
def _the_connector_under_test_is_the_real_one():
    """Two older tests used to leave a lambda in place of a connector function
    for the rest of the session. A test of the real pull that silently ran a
    stub would prove nothing, so this fails first."""
    for name, real in REAL.items():
        current = getattr(hp, name)
        assert current is real and current.__name__ == name, (
            f"connectors.hubspot_pull.{name} has been replaced by "
            f"{current!r} — another test leaked a stub")
    assert hp._fetch_primary_contact_for_deal.__name__ == (
        "_fetch_primary_contact_for_deal")


@pytest.fixture
def hubspot(monkeypatch):
    fake = FakeHubSpot()
    monkeypatch.setattr(hp, "get_client", lambda: fake.client)
    monkeypatch.setattr(hp.requests, "get", fake.http_get)
    for verb in ("post", "put", "patch", "delete", "request"):
        monkeypatch.setattr(hp.requests, verb, fake.forbidden_write)
    monkeypatch.setattr(hp.time, "sleep", fake.sleeps.append)
    return fake


def _attempt(call):
    """``(returned, raised)`` — exactly one is not None/``_NOTHING``."""
    try:
        return call(), None
    except Exception as exc:  # noqa: BLE001
        return _NOTHING, exc


_NOTHING = object()


def _must_raise_incomplete(call, *, pull, records_read=None, describe=None):
    """The pull must not return, and must raise the named error.

    Two assertions on purpose. The first is the truth defect: a pull that did
    not finish RETURNED a list. The second is the contract: the failure is the
    one named exception, carrying the function, how much was read, and why."""
    returned, raised = _attempt(call)
    assert returned is _NOTHING, (
        f"{pull} RETURNED {len(returned)} record(s) from a pull that did not "
        "finish — a truncated list presented as the whole result"
        + (f": {describe(returned)}" if describe else ""))
    incomplete = getattr(hp, "HubSpotPullIncompleteError", None)
    assert incomplete is not None and isinstance(raised, incomplete), (
        f"{pull} raised {type(raised).__name__}: {raised!s:.200} — not the "
        "named HubSpotPullIncompleteError")
    assert raised.pull == pull
    assert pull in str(raised)
    if records_read is not None:
        assert raised.records_read == records_read
        assert f"{records_read} record(s) read" in str(raised)
    return raised


def _names_status(err, status: int) -> bool:
    """The message carries the HTTP status as a number of its own — not as
    part of an object id that happens to contain the digits."""
    return re.search(rf"(?<!\d){status}(?!\d)", str(err)) is not None


def _backoff(retries: int) -> list:
    return [hp.INITIAL_BACKOFF_SECONDS * 2 ** i for i in range(retries)]


# ═════════════════════════════════════════════════════════════════════════════
# 1 — the four paginated search pulls
# ═════════════════════════════════════════════════════════════════════════════
#: name → (the API it pages, how a caller invokes it, what a complete pull is)
SEARCH_PULLS = {
    "pull_paid_search_contacts": (
        "contacts",
        lambda: hp.pull_paid_search_contacts(days_back=30),
        lambda fake: fake.paid_contacts()),
    "pull_paid_search_contacts_in_range": (
        "contacts",
        lambda: hp.pull_paid_search_contacts_in_range(
            WINDOW_FROM.isoformat(), TODAY.isoformat()),
        lambda fake: fake.paid_contacts()),
    "pull_all_contacts_in_range": (
        "contacts",
        lambda: hp.pull_all_contacts_in_range(
            WINDOW_FROM.isoformat(), TODAY.isoformat()),
        lambda fake: fake.expected_contacts(fake.PAID + fake.ORGANIC)),
    "pull_closed_won_deals_in_range": (
        "deals",
        lambda: hp.pull_closed_won_deals_in_range(
            WINDOW_FROM.isoformat(), TODAY.isoformat()),
        lambda fake: fake.expected_won_rows()),
}

each_search_pull = pytest.mark.parametrize("pull", sorted(SEARCH_PULLS))


@each_search_pull
def test_01_a_clean_multi_page_pull_returns_every_record_unchanged(pull, hubspot):
    """The success path is what it was: the same records, the same shape, the
    same order, three pages, no waiting. This case passes on the pre-fix tree
    too — that is the point of it."""
    family, call, expected = SEARCH_PULLS[pull]
    result = call()
    assert result == expected(hubspot)
    assert len(result) > 200                           # three pages of 100
    assert sorted(k[1] for k in hubspot.attempts if k[0] == f"{family}.search") \
        == [0, 1, 2]
    assert hubspot.attempts[(f"{family}.search", 0)] == 1
    assert hubspot.sleeps == []


@each_search_pull
def test_02_a_401_on_the_first_page_raises_and_does_not_return_an_empty_list(
        pull, hubspot):
    """The expired token. The pull used to log it and return ``[]``, which is
    also what a quiet month looks like."""
    family, call, _expected = SEARCH_PULLS[pull]
    hubspot.faults[(f"{family}.search", 0)] = Always(401)
    err = _must_raise_incomplete(call, pull=pull, records_read=0)
    assert _names_status(err, 401)
    assert getattr(err.cause, "status", None) == 401
    assert hubspot.attempts[(f"{family}.search", 0)] == 1    # not retried
    assert hubspot.sleeps == []


@each_search_pull
def test_03_a_500_on_page_three_raises_and_does_not_return_the_first_two_pages(
        pull, hubspot):
    family, call, _expected = SEARCH_PULLS[pull]
    hubspot.faults[(f"{family}.search", 2)] = Always(500)
    err = _must_raise_incomplete(call, pull=pull, records_read=200)
    assert _names_status(err, 500)
    assert hubspot.attempts[(f"{family}.search", 2)] == 1


@each_search_pull
@pytest.mark.parametrize("page", [0, 1])
def test_04_a_persistent_429_raises_after_a_bounded_number_of_attempts(
        pull, page, hubspot):
    """These loops slept and retried a 429 with no limit. The budget is now
    MAX_RETRIES retries per page, with exponential backoff, then a raise — and
    the page already read is not returned."""
    family, call, _expected = SEARCH_PULLS[pull]
    key = (f"{family}.search", page)
    hubspot.faults[key] = Always(429)
    err = _must_raise_incomplete(call, pull=pull, records_read=100 * page)
    assert hubspot.attempts[key] == hp.MAX_RETRIES + 1
    assert hubspot.sleeps == _backoff(hp.MAX_RETRIES)
    assert _names_status(err, 429)


@each_search_pull
def test_05_a_transient_429_is_retried_and_the_complete_result_returned(
        pull, hubspot):
    family, call, expected = SEARCH_PULLS[pull]
    hubspot.faults[(f"{family}.search", 1)] = [429, 429]
    assert call() == expected(hubspot)
    assert hubspot.attempts[(f"{family}.search", 1)] == 3
    assert len(hubspot.sleeps) == 2        # the schedule itself: test_04


@each_search_pull
def test_06_the_429_budget_is_per_page_not_per_pull(pull, hubspot):
    """A long pull that is rate limited once on every page is healthy. A
    budget spent across the whole pull would fail it on the third page."""
    family, call, expected = SEARCH_PULLS[pull]
    for page in (0, 1, 2):
        hubspot.faults[(f"{family}.search", page)] = [429] * hp.MAX_RETRIES
    assert call() == expected(hubspot)
    assert len(hubspot.sleeps) == 3 * hp.MAX_RETRIES


# ═════════════════════════════════════════════════════════════════════════════
# 2 — pull_deals_with_gclid: every GCLID contact's deals, or an exception
# ═════════════════════════════════════════════════════════════════════════════
def _gclid_pull(fake):
    return lambda: hp.pull_deals_with_gclid(fake.paid_contacts())


def _victim(fake, with_deals=2):
    """A GCLID contact in the middle of the scan that has ``with_deals``
    deals, and how many deals the contacts before it carry."""
    ids = fake.gclid_contact_ids()
    for position, cid in enumerate(ids):
        if position > 10 and len(fake.contact_deals.get(cid, [])) == with_deals:
            return cid, len(fake.expected_gclid_deals(ids[:position]))
    raise AssertionError("fixture has no such contact")


def test_07_every_gclid_contacts_deals_are_returned_when_all_reads_succeed(hubspot):
    result = _gclid_pull(hubspot)()
    assert result == hubspot.expected_gclid_deals()
    # The fixture is not trivial: contacts with two deals, one and none.
    per_contact = Counter(len(hubspot.contact_deals.get(c, []))
                          for c in hubspot.gclid_contact_ids())
    assert set(per_contact) == {0, 1, 2}
    # Contacts without a GCLID are never looked up.
    asked = {k[2] for k in hubspot.attempts if k[:2] == ("assoc", "contacts")}
    assert asked == set(hubspot.gclid_contact_ids())
    assert hubspot.sleeps == []


@pytest.mark.parametrize("status", [401, 500])
def test_08_one_contacts_failed_association_read_raises_instead_of_being_skipped(
        status, hubspot):
    """It was logged and the loop moved on, so the list silently lacked that
    contact's deals."""
    victim, before = _victim(hubspot)
    hubspot.faults[("assoc", "contacts", victim)] = Always(status)
    err = _must_raise_incomplete(_gclid_pull(hubspot), pull="pull_deals_with_gclid",
                                 records_read=before)
    assert before > 0                      # earlier contacts' deals were read
    assert victim in str(err) and _names_status(err, status)
    assert hubspot.attempts[("assoc", "contacts", victim)] == hp.MAX_RETRIES


def test_09_a_persistently_rate_limited_association_read_raises_after_max_retries(
        hubspot):
    victim, before = _victim(hubspot)
    hubspot.faults[("assoc", "contacts", victim)] = Always(429)
    _must_raise_incomplete(_gclid_pull(hubspot), pull="pull_deals_with_gclid",
                           records_read=before)
    assert hubspot.attempts[("assoc", "contacts", victim)] == hp.MAX_RETRIES
    assert hubspot.sleeps == _backoff(hp.MAX_RETRIES - 1)


def test_10_a_connection_error_on_an_association_read_raises_after_max_retries(
        hubspot):
    victim, before = _victim(hubspot)
    hubspot.faults[("assoc", "contacts", victim)] = Always(
        requests.exceptions.ConnectionError("connection reset"))
    err = _must_raise_incomplete(_gclid_pull(hubspot), pull="pull_deals_with_gclid",
                                 records_read=before)
    assert isinstance(err.cause, requests.exceptions.ConnectionError)
    assert hubspot.attempts[("assoc", "contacts", victim)] == hp.MAX_RETRIES


@pytest.mark.parametrize("fault", [
    [429], [429, 429], [500],
    [requests.exceptions.ConnectionError("reset")]],
    ids=["one 429", "two 429s", "one 500", "one connection error"])
def test_11_a_transient_association_failure_is_retried_and_nothing_is_lost(
        fault, hubspot):
    victim, _before = _victim(hubspot)
    hubspot.faults[("assoc", "contacts", victim)] = list(fault)
    assert _gclid_pull(hubspot)() == hubspot.expected_gclid_deals()


@pytest.mark.parametrize("status", [404, 500])
def test_12_one_deal_that_cannot_be_fetched_raises(status, hubspot):
    """The deal read goes through the SDK's DEALS api, whose ApiException is a
    different class from the contacts one the connector caught."""
    victim, before = _victim(hubspot)
    second_deal = hubspot.contact_deals[victim][1]
    hubspot.faults[("deals.get", second_deal)] = Always(status)
    err = _must_raise_incomplete(_gclid_pull(hubspot), pull="pull_deals_with_gclid",
                                 records_read=before)
    assert victim in str(err) and _names_status(err, status)
    assert hubspot.attempts[("deals.get", second_deal)] == 1


def test_13_a_persistently_rate_limited_deal_fetch_raises_after_max_retries(hubspot):
    victim, before = _victim(hubspot)
    second_deal = hubspot.contact_deals[victim][1]
    hubspot.faults[("deals.get", second_deal)] = Always(429)
    _must_raise_incomplete(_gclid_pull(hubspot), pull="pull_deals_with_gclid",
                           records_read=before)
    assert hubspot.attempts[("deals.get", second_deal)] == hp.MAX_RETRIES
    assert hubspot.sleeps == _backoff(hp.MAX_RETRIES - 1)


def test_14_a_rate_limited_second_deal_is_retried_without_returning_the_first_twice(
        hubspot):
    """A 429 on a deal read is retried now (it used to end the pull). The
    retry re-reads the contact from the start, so the deals of the failed
    attempt must not stay in the list: two deals, not three."""
    victim, _before = _victim(hubspot)
    first_deal, second_deal = hubspot.contact_deals[victim]
    hubspot.faults[("deals.get", second_deal)] = [429]
    result = _gclid_pull(hubspot)()
    assert result == hubspot.expected_gclid_deals()
    assert [d["id"] for d in result if d["contact_id"] == victim] == [
        first_deal, second_deal]


def test_15_an_unread_second_page_of_associations_raises(hubspot):
    """The pull reads one page of a contact's associations. A response that
    says there is another is a list this pull would return short."""
    victim, before = _victim(hubspot)
    hubspot.assoc_has_next_page.add(("contacts", victim))
    err = _must_raise_incomplete(_gclid_pull(hubspot), pull="pull_deals_with_gclid",
                                 records_read=before)
    assert "page" in str(err) and victim in str(err)
    assert hubspot.attempts[("assoc", "contacts", victim)] == 1


# ═════════════════════════════════════════════════════════════════════════════
# 3 — pull_closed_won_deals_in_range: a deal's contact is part of the deal
# ═════════════════════════════════════════════════════════════════════════════
def _won_pull():
    return hp.pull_closed_won_deals_in_range(
        WINDOW_FROM.isoformat(), TODAY.isoformat())


def _won_victim(fake):
    """A won deal whose contact carries a GCLID, in the middle of page one."""
    for position, did in enumerate(fake.WON):
        cid = (fake.deal_contacts.get(did) or [None])[0]
        if position > 10 and cid and fake.rows["contacts"][cid]["hs_google_click_id"]:
            return did, cid, position
    raise AssertionError("fixture has no such deal")


@pytest.mark.parametrize("where", ["association", "contact"])
@pytest.mark.parametrize("status", [429, 500])
def test_16_a_won_deal_whose_contact_cannot_be_read_is_not_returned_as_no_gclid(
        where, status, hubspot):
    """``gclid=None`` means the contact was read and has no click evidence.
    A lookup that failed used to produce the same row — a rate-limited won
    deal was reported as unattributable."""
    did, cid, position = _won_victim(hubspot)
    key = ("assoc", "deals", did) if where == "association" else ("contacts.get", cid)
    hubspot.faults[key] = Always(status)

    def what_came_back(rows):
        row = next(r for r in rows if r["deal_id"] == did)
        return (f"deal {did} came back with gclid={row['gclid']!r} although "
                f"its contact {cid} was never read")
    err = _must_raise_incomplete(_won_pull, pull="pull_closed_won_deals_in_range",
                                 records_read=position, describe=what_came_back)
    assert f"deal {did}" in str(err) and _names_status(err, status)
    assert hubspot.attempts[key] == hp.MAX_RETRIES


@pytest.mark.parametrize("where", ["association", "contact"])
def test_17_a_transient_contact_lookup_failure_is_retried_and_the_gclid_kept(
        where, hubspot):
    did, cid, _position = _won_victim(hubspot)
    key = ("assoc", "deals", did) if where == "association" else ("contacts.get", cid)
    hubspot.faults[key] = [429, 500]
    result = _won_pull()
    assert result == hubspot.expected_won_rows()
    assert next(r for r in result if r["deal_id"] == did)["gclid"] == f"G-{cid}"


def test_18_a_won_deal_with_no_associated_contact_is_still_returned_unattributed(
        hubspot):
    """The success path that looks most like the failure: the lookup answered,
    and the answer was "no contact". That is a finding, not an error."""
    lonely = [did for did in hubspot.WON if did not in hubspot.deal_contacts]
    assert lonely
    rows = {r["deal_id"]: r for r in _won_pull()}
    for did in lonely:
        assert rows[did]["contact_id"] is None and rows[did]["gclid"] is None


def test_19_a_failed_page_of_deals_fails_the_sources_pull_under_its_own_name(hubspot):
    """``pull_closed_won_deals_with_sources_in_range`` runs the same won-deal
    search. A failed page must not become a shorter source backfill, and the
    error names the pull that failed."""
    hubspot.faults[("deals.search", 1)] = Always(500)
    _must_raise_incomplete(
        lambda: hp.pull_closed_won_deals_with_sources_in_range(
            WINDOW_FROM.isoformat(), TODAY.isoformat()),
        pull="pull_closed_won_deals_with_sources_in_range", records_read=100)


# ═════════════════════════════════════════════════════════════════════════════
# 4 — structure: no handler in these functions ends a pull quietly
# ═════════════════════════════════════════════════════════════════════════════
def test_20_no_except_handler_in_a_legacy_pull_breaks_or_returns():
    """Every swallowing site was an ``except`` that ended in ``break`` (return
    what there is) or ``return None``. A handler in these functions may retry
    or raise; it may not finish the pull."""
    tree = ast.parse((ROOT / "connectors/hubspot_pull.py").read_text(encoding="utf-8"))
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    checked = set(PULLS) | {
        "_fetch_primary_contact_for_deal",
        "pull_closed_won_deals_with_sources_in_range",
        "_fetch_associated_contact_ids", "_fetch_contact_source_props"} | (
        {"_legacy_search_page", "_deals_for_gclid_contact",
         "_iter_closed_won_deal_pages"} & set(functions))
    offenders = []
    for name in sorted(checked):
        for handler in (n for n in ast.walk(functions[name])
                        if isinstance(n, ast.ExceptHandler)):
            for node in ast.walk(handler):
                if isinstance(node, (ast.Break, ast.Return)):
                    offenders.append(f"{name}: line {node.lineno} "
                                     f"{type(node).__name__.lower()}")
    assert offenders == []
    # And no retry is left inside an unbounded loop: a handler that says
    # `continue` under a `while` is the 429 loop that never ended.
    for name in sorted(checked):
        for loop in (n for n in ast.walk(functions[name]) if isinstance(n, ast.While)):
            for handler in (n for n in ast.walk(loop)
                            if isinstance(n, ast.ExceptHandler)):
                assert not any(isinstance(n, ast.Continue) for n in ast.walk(handler)), (
                    f"{name}: an except handler retries inside a `while` loop "
                    f"(line {handler.lineno}) — nothing bounds it")


# ═════════════════════════════════════════════════════════════════════════════
# 5 — the monthly job, with the real pulls over a failing HubSpot
#     (PR-ADS-165A: the weekly job ran through these cases too, until it was
#     retired; the monthly job's assertions are unchanged)
# ═════════════════════════════════════════════════════════════════════════════
PREVIOUS_SNAPSHOT = [{"id": "previous-run-contact"}]


class _JobRun:
    """The PR-ADS-162H harness with the HubSpot stubs taken back out.

    The harness stubs every external call the two jobs make. Here the two
    pulls, the lead summary and the JSON save are the real functions again, so
    the only fake between the job and HubSpot is the portal."""

    def __init__(self, monkeypatch, tmp_path, job, fake):
        import test_pr_ads_162h_report_generation_retired as t162h

        self.h = t162h._Harness(monkeypatch, tmp_path, job)
        self.job = job
        self.saves: list = []
        self.leads: list = []
        for name in ("pull_paid_search_contacts", "pull_deals_with_gclid",
                     "get_lead_quality_summary"):
            monkeypatch.setattr(hp, name, REAL[name])

        def save(contacts, deals, summary):
            self.saves.append((len(contacts), len(deals)))
            return REAL["save_output"](contacts, deals, summary)
        monkeypatch.setattr(hp, "save_output", save)
        self.h.overrides["write_leads"] = (
            lambda run_id, rows: self.leads.append(list(rows)) or len(rows))
        self.snapshot = tmp_path / "data" / "crm_contacts.json"
        self.snapshot.write_text(json.dumps(PREVIOUS_SNAPSHOT), encoding="utf-8")

    def run(self):
        return _attempt(self.h.run)


@pytest.fixture
def job_run(monkeypatch, tmp_path, hubspot):
    return lambda job: _JobRun(monkeypatch, tmp_path, job, hubspot)


def _expired_token(fake):
    fake.token_expired = True


def _page_three_fails(fake):
    fake.faults[("contacts.search", 2)] = Always(500)


def _one_contacts_deals_unreadable(fake):
    victim, _before = _victim(fake)
    fake.faults[("assoc", "contacts", victim)] = Always(500)


JOB_FAILURES = [
    ("expired token (401 on the first page)", _expired_token,
     "pull_paid_search_contacts"),
    ("500 on page three of the contacts", _page_three_fails,
     "pull_paid_search_contacts"),
    ("one GCLID contact's deals unreadable", _one_contacts_deals_unreadable,
     "pull_deals_with_gclid"),
]


@pytest.mark.parametrize("job", ["monthly"])
@pytest.mark.parametrize("case, break_hubspot, pull", JOB_FAILURES,
                         ids=[c[0] for c in JOB_FAILURES])
def test_21_a_failed_hubspot_pull_fails_the_job_and_writes_nothing(
        job, case, break_hubspot, pull, job_run, hubspot):
    """The reproduced production defect, end to end. With an expired token the
    monthly job recorded ``success``, stamped ``hubspot/contacts`` fresh,
    overwrote ``data/crm_contacts.json`` with ``[]`` and stored a 0-of-0
    coverage snapshot."""
    run = job_run(job)
    break_hubspot(hubspot)
    outcome, raised = run.run()
    h = run.h

    # The run is failed — in the run record and in what the caller is told.
    assert h.record["status"] == "failed", (
        f"the {job} job recorded {h.record['status']!r} over a HubSpot pull "
        "that did not finish")
    assert pull in h.record["error_message"]
    # The monthly job returns its outcome; it does not raise.
    assert raised is None
    assert outcome["status"] == "failed"
    assert "Step 2/4" in h.record["failed_step"]

    # Nothing from the truncated or empty pull was stored.
    assert run.leads == [] and "write_leads" not in h.calls
    assert "write_deals" not in h.calls
    assert run.saves == []
    assert json.loads(run.snapshot.read_text(encoding="utf-8")) == PREVIOUS_SNAPSHOT
    # No freshness stamp: the hubspot batches were never opened, let alone
    # finished `success`.
    assert h.batch_status(("hubspot", "contacts")) == []
    assert h.batch_status(("hubspot", "deals")) == []
    assert "success" not in {status for _key, status in h.finished_batches}
    # And the run stopped there: no GCLID match, no coverage snapshot.
    assert "run_gclid_match" not in h.calls
    assert "write_gclid_coverage_snapshot" not in h.calls
    # No `runs` row is updated to anything but failed.
    assert {rec["status"] for _rid, rec in h.updated} <= {"failed"}


@pytest.mark.parametrize("job", ["monthly"])
def test_22_the_same_job_over_a_healthy_hubspot_succeeds_and_stores_everything(
        job, job_run, hubspot):
    """The control for test_21: the harness can succeed with the real pulls,
    so a failure there is the pull's doing and not the harness's."""
    run = job_run(job)
    outcome, raised = run.run()
    h = run.h
    assert raised is None
    assert outcome == {"run_type": job, "status": "success", "failed_steps": []}
    assert h.record["status"] == "success"
    assert run.leads == [hubspot.paid_contacts()]
    assert run.saves == [(230, len(hubspot.expected_gclid_deals()))]
    assert json.loads(run.snapshot.read_text(encoding="utf-8")) == \
        hubspot.paid_contacts()
    assert h.batch_status(("hubspot", "contacts")) == ["success"]
    assert h.batch_status(("hubspot", "deals")) == ["success"]
    assert "write_gclid_coverage_snapshot" in h.calls


# ═════════════════════════════════════════════════════════════════════════════
# 6 — the daily pulse
# ═════════════════════════════════════════════════════════════════════════════
class _DailyRun:
    def __init__(self, monkeypatch):
        import connectors.google_ads_source as gads
        import scheduler.daily as daily

        self.daily = daily
        self.records, self.updates, self.batches = [], [], []
        self.leads, self.reports = [], []
        # The pulse reads config/junk_patterns.yaml relative to the repo root.
        # Its own result file is not written: `save_daily_report` is recorded.
        monkeypatch.chdir(ROOT)
        monkeypatch.setattr(gads, "pull_campaign_performance", lambda **kw: [])
        w = daily.db_writers
        monkeypatch.setattr(w, "write_run", lambda record: 4242)
        monkeypatch.setattr(
            w, "update_run",
            lambda run_id, record: self.updates.append((run_id, record["status"])))
        monkeypatch.setattr(
            w, "start_sync_batch",
            lambda **kw: self.batches.append(
                ("start", kw["source"], kw["dataset"])) or 9)
        monkeypatch.setattr(
            w, "finish_sync_batch",
            lambda **kw: self.batches.append(("finish", kw["status"])) or True)
        monkeypatch.setattr(
            w, "write_leads",
            lambda run_id, rows: self.leads.append(list(rows)) or len(rows))
        monkeypatch.setattr(
            "services.search_term_sync_service.sync_recent_search_terms",
            lambda *a, **kw: {"ok": True, "rows": []})
        monkeypatch.setattr(
            "services.keyword_sync_service.sync_recent_keyword_facts",
            lambda *a, **kw: {"ok": True})
        monkeypatch.setattr(daily, "save_daily_report", self.reports.append)
        monkeypatch.setattr(daily, "deliver_report", lambda result: None)
        monkeypatch.setattr("scheduler.run_history.write_run_record",
                            lambda record: self.records.append(dict(record)))

    def run(self):
        return _attempt(self.daily.run_daily_pulse)


@pytest.mark.parametrize("break_hubspot", [_expired_token, _page_three_fails],
                         ids=["expired token", "500 on page three"])
def test_23_the_daily_pulse_does_not_report_a_day_over_a_failed_contact_pull(
        break_hubspot, monkeypatch, hubspot):
    """It treats a raising pull the way it treats a raising Google Ads pull:
    the run row is updated `failed` and the exception propagates. Before, the
    pulse ran on over ``[]``, recorded `success` and saved a result."""
    if break_hubspot is _page_three_fails:
        # The pulse asks for two days; make three pages of contacts that new.
        for cid in hubspot.PAID:
            hubspot.date_ms["contacts"][cid]["createdate"] = _ms(TODAY)
    run = _DailyRun(monkeypatch)
    break_hubspot(hubspot)
    result, raised = run.run()

    assert result is _NOTHING, (
        f"the daily pulse returned {result!r} over a contact pull that did "
        "not finish")
    assert isinstance(raised, hp.HubSpotPullIncompleteError)
    assert raised.pull == "pull_paid_search_contacts"
    assert [r["status"] for r in run.records] == ["failed"]
    assert "pull_paid_search_contacts" in run.records[0]["error_message"]
    assert run.updates == [(4242, "failed")]        # the row that was inserted
    assert run.leads == [] and run.batches == [] and run.reports == []


def test_24_the_daily_pulse_over_a_healthy_hubspot_still_completes(
        monkeypatch, hubspot):
    run = _DailyRun(monkeypatch)
    result, raised = run.run()
    assert raised is None and result["status"] in ("clean", "flagged")
    assert [r["status"] for r in run.records] == ["success"]
    assert run.updates == [(4242, "success")]
    assert len(run.leads) == 1
    assert ("finish", "success") in run.batches


# ═════════════════════════════════════════════════════════════════════════════
# 7 — the daily incremental sync (the production pipeline)
# ═════════════════════════════════════════════════════════════════════════════
class _IncrementalRun:
    """The existing incremental-sync fixture, with HubSpot's five legacy pulls
    put back and the batch ledger recorded.

    ``_patch_all_datasets_success`` (tests/test_daily_incremental_sync.py)
    stubs every step at its service seam, including these pulls. The pulls are
    the real functions again here; every other step keeps its stub."""

    HUBSPOT_PULLS = PULLS + ("pull_closed_won_deals_with_sources_in_range",)

    def __init__(self, monkeypatch):
        import test_daily_incremental_sync as t_inc
        import scheduler.incremental_sync as sync

        self.sync = sync
        t_inc._patch_all_datasets_success(monkeypatch)
        for name in self.HUBSPOT_PULLS:
            monkeypatch.setattr(hp, name, REAL[name])

        self.run_rows, self.run_updates = [], []
        self.opened: dict = {}
        self.finished: list = []
        self.written = {"leads": [], "deals": [], "gclid": [], "classified": [],
                        "attributed": []}

        def write_run_detailed(record):
            self.run_rows.append(dict(record))
            return 61, None
        monkeypatch.setattr("db.writers.write_run_detailed", write_run_detailed)
        monkeypatch.setattr(
            "db.writers.update_run",
            lambda run_id, fields: self.run_updates.append((run_id, dict(fields))))

        def start(*, source, dataset, **kwargs):
            batch_id = 500 + len(self.opened)
            self.opened[batch_id] = (source, dataset)
            return batch_id
        monkeypatch.setattr("db.writers.start_sync_batch", start)

        def finish(batch_id=None, status=None, **kwargs):
            self.finished.append((self.opened[batch_id], status, kwargs))
            return True
        monkeypatch.setattr("db.writers.finish_sync_batch", finish)

        def writer(kind):
            def write(run_id, rows, *args, **kwargs):
                self.written[kind].append(list(rows))
                return len(rows)
            return write
        monkeypatch.setattr("db.writers.write_leads", writer("leads"))
        monkeypatch.setattr("db.writers.write_deals", writer("deals"))
        monkeypatch.setattr("db.writers.write_gclid_attribution", writer("gclid"))
        monkeypatch.setattr(
            "db.writers.upsert_contact_source_classification",
            lambda rows: self.written["classified"].append(list(rows)) or len(rows))
        monkeypatch.setattr(
            "db.writers.upsert_deal_source_attribution",
            lambda rows: self.written["attributed"].append(list(rows)) or len(rows))

    def run(self):
        return self.sync.run_daily_incremental_sync(
            lookback_days_hubspot_contacts=25, lookback_days_hubspot_deals=25,
            run_reason="test")

    def batches(self, source, dataset) -> list:
        return [(status, kwargs) for key, status, kwargs in self.finished
                if key == (source, dataset)]


#: The four datasets of the incremental run that read HubSpot through a legacy
#: pull, and the (source, dataset) batch each one stamps.
LEGACY_INCREMENTAL_DATASETS = {
    "hubspot/contacts": ("hubspot", "contacts"),
    "hubspot/deals": ("hubspot", "deals"),
    "gclid/matches": ("gclid", "matches"),
    "hubspot/source_classification": ("hubspot", "source_classification"),
}


def test_25_an_expired_token_fails_every_legacy_hubspot_step_of_the_incremental_sync(
        monkeypatch, hubspot):
    """The run used to report these four `success` over empty lists and stamp
    each dataset's watermark with today's date."""
    run = _IncrementalRun(monkeypatch)
    hubspot.token_expired = True
    result = run.run()

    # The persisted run type, exactly as the scheduler writes it.
    assert result["run_type"] == run.sync.RUN_TYPE == "daily_incremental_sync"
    assert [r["run_type"] for r in run.run_rows] == ["daily_incremental_sync"]

    # Not success. Other steps (stubbed healthy) landed, so by the sync's own
    # rule the run is partial — and the durable row says the same word.
    assert result["status"] == result["execution_status"] == "partial"
    assert run.run_updates[-1][0] == 61
    assert run.run_updates[-1][1]["status"] == "partial"

    for name, (source, dataset) in LEGACY_INCREMENTAL_DATASETS.items():
        entry = result["datasets"][name]
        assert entry["status"] == "failed", (name, entry)
        assert "401" in entry["error"], (name, entry)
        # The sync's own mechanism: the step's batch is finished `failed`, and
        # carries no source date, so `sync_state` moves no watermark for it.
        finished = run.batches(source, dataset)
        assert [status for status, _kw in finished] == ["failed"], (name, finished)
        assert finished[0][1].get("last_source_date") is None
        assert finished[0][1].get("error_message")
        assert any(e.startswith(f"{name}: ") for e in result["errors"]), name
    assert "pull_paid_search_contacts_in_range" in \
        result["datasets"]["hubspot/contacts"]["error"]
    assert run.written == {"leads": [], "deals": [], "gclid": [], "classified": [],
                           "attributed": []}


def test_26_a_page_that_fails_mid_scan_does_not_persist_the_pages_before_it(
        monkeypatch, hubspot):
    run = _IncrementalRun(monkeypatch)
    hubspot.faults[("contacts.search", 1)] = Always(500)
    result = run.run()

    assert result["status"] == "partial"
    for name in ("hubspot/contacts", "hubspot/deals",
                 "hubspot/source_classification"):
        assert result["datasets"][name]["status"] == "failed", name
    assert "100 record(s) read" in result["datasets"]["hubspot/contacts"]["error"]
    assert run.written["leads"] == [] and run.written["classified"] == []
    for source, dataset in (("hubspot", "contacts"), ("hubspot", "deals"),
                            ("hubspot", "source_classification")):
        (status, kwargs), = run.batches(source, dataset)
        assert status == "failed" and kwargs.get("last_source_date") is None
    # The deals search was not broken, so the step that reads only deals is
    # unaffected: one failed dataset does not fail its neighbours.
    assert result["datasets"]["gclid/matches"]["status"] == "success"


def test_27_one_unreadable_contact_fails_the_legacy_deals_step_only(
        monkeypatch, hubspot):
    """A per-contact association failure used to be skipped: the step was
    `success` with that contact's deals missing."""
    run = _IncrementalRun(monkeypatch)
    victim, _before = _victim(hubspot)
    hubspot.faults[("assoc", "contacts", victim)] = Always(429)
    result = run.run()

    assert result["status"] == "partial"
    assert result["datasets"]["hubspot/deals"]["status"] == "failed"
    assert "pull_deals_with_gclid" in result["datasets"]["hubspot/deals"]["error"]
    assert result["datasets"]["hubspot/deals"]["authoritative"] is False
    assert run.written["deals"] == []
    (status, kwargs), = run.batches("hubspot", "deals")
    assert status == "failed" and kwargs.get("last_source_date") is None
    assert result["datasets"]["hubspot/contacts"]["status"] == "success"


def test_28_the_incremental_sync_over_a_healthy_hubspot_persists_complete_pulls(
        monkeypatch, hubspot):
    """The control: with nothing failing, the same fixture is `success`, every
    record reaches its writer and each batch carries its source date."""
    run = _IncrementalRun(monkeypatch)
    result = run.run()

    assert result["status"] == "success", result["errors"]
    assert run.run_updates[-1][1]["status"] == "success"
    assert run.written["leads"] == [hubspot.paid_contacts()]
    assert run.written["deals"] == [hubspot.expected_gclid_deals()]
    assert len(run.written["classified"][0]) == 270
    assert [row["deal_id"] for row in run.written["attributed"][0]] == hubspot.WON
    assert len(run.written["gclid"][0]) == sum(
        1 for row in hubspot.expected_won_rows() if row["gclid"])
    for name, (source, dataset) in LEGACY_INCREMENTAL_DATASETS.items():
        assert result["datasets"][name]["status"] == "success", name
        (status, kwargs), = run.batches(source, dataset)
        assert status == "success" and kwargs.get("last_source_date") is not None


# ═════════════════════════════════════════════════════════════════════════════
# 8 — the backfill script, revenue recovery, the Mailchimp identity bridge
# ═════════════════════════════════════════════════════════════════════════════
class _Backfill:
    def __init__(self, monkeypatch):
        self.finished, self.written, self.run_status = [], [], []
        monkeypatch.setattr("db.writers.write_run", lambda record: 31)
        monkeypatch.setattr(
            "db.writers.update_run",
            lambda run_id, fields: self.run_status.append(fields["status"]))
        monkeypatch.setattr("db.writers.start_sync_batch", lambda **kw: 77)
        monkeypatch.setattr(
            "db.writers.finish_sync_batch",
            lambda batch_id, status, **kw: self.finished.append((status, kw)) or True)
        for name in ("write_leads", "write_deals"):
            monkeypatch.setattr(
                f"db.writers.{name}",
                lambda run_id, rows: self.written.append(len(rows)) or len(rows))

    def run(self, dataset):
        from scripts.backfill_hubspot import run_hubspot_backfill
        return run_hubspot_backfill(dataset, [(WINDOW_FROM, TODAY)])


@pytest.mark.parametrize("dataset, break_hubspot, pull", [
    ("contacts", _expired_token, "pull_paid_search_contacts_in_range"),
    ("contacts", _page_three_fails, "pull_paid_search_contacts_in_range"),
    ("deals", _one_contacts_deals_unreadable, "pull_deals_with_gclid"),
], ids=["contacts: expired token", "contacts: 500 on page three",
        "deals: one contact unreadable"])
def test_29_the_backfill_reports_a_failed_chunk_not_rows_from_a_truncated_pull(
        dataset, break_hubspot, pull, monkeypatch, hubspot):
    backfill = _Backfill(monkeypatch)
    break_hubspot(hubspot)
    result = backfill.run(dataset)

    assert result["status"] == "failed", result
    assert result["chunks_completed"] == 0
    assert result["rows_pulled"] == 0 and result["rows_written"] == 0
    assert len(result["errors"]) == 1 and pull in result["errors"][0]
    assert backfill.written == []
    assert [status for status, _kw in backfill.finished] == ["failed"]
    assert "last_source_date" not in backfill.finished[0][1]
    assert backfill.run_status == ["failed"]


def test_30_the_backfill_over_a_healthy_hubspot_writes_the_complete_chunk(
        monkeypatch, hubspot):
    backfill = _Backfill(monkeypatch)
    result = backfill.run("contacts")
    assert result["status"] == "success" and result["errors"] == []
    assert result["rows_pulled"] == result["rows_written"] == 230
    assert [status for status, _kw in backfill.finished] == ["success"]


class _Recovery:
    def __init__(self, monkeypatch):
        self.leads, self.attribution, self.run_status = [], [], []
        self.store = {"completed": []}
        monkeypatch.setattr("db.writers.write_run", lambda record: 88)
        monkeypatch.setattr(
            "db.writers.update_run",
            lambda run_id, fields: self.run_status.append(fields["status"]))
        monkeypatch.setattr(
            "db.writers.write_leads",
            lambda run_id, rows: self.leads.append(len(rows)) or len(rows))
        monkeypatch.setattr(
            "db.writers.write_gclid_attribution",
            lambda run_id, rows, *a, **kw:
            self.attribution.append(len(rows)) or len(rows))

    def run(self, resume=False):
        from services.revenue_recovery_service import run_revenue_recovery

        def checkpoint(job_id, snapshot):
            self.store["completed"] = list(snapshot.get("completed_chunks", []))
        return run_revenue_recovery(
            date_from=WINDOW_FROM.isoformat(), date_to=TODAY.isoformat(),
            dry_run=False, chunk_months=12, resume=resume, job_id="job-163a",
            checkpoint=checkpoint,
            load_completed=lambda: list(self.store["completed"]))


@pytest.mark.parametrize("phase, key, pull", [
    ("contacts", ("contacts.search", 1), "pull_paid_search_contacts_in_range"),
    ("revenue", ("deals.search", 1), "pull_closed_won_deals_in_range"),
])
def test_31_revenue_recovery_does_not_write_or_complete_a_chunk_whose_pull_failed(
        phase, key, pull, monkeypatch, hubspot):
    """Two things. The truncated pull is not written. And the chunk is not
    recorded complete — it used to be added to the completed set whatever
    happened, so a resume skipped it and its rows were never read."""
    recovery = _Recovery(monkeypatch)
    hubspot.faults[key] = Always(500)
    result = recovery.run()

    assert result["status"] == "partial"
    assert recovery.run_status == ["partial"]
    assert len(result["errors"]) == 1
    assert result["errors"][0].startswith(f"{phase} ") and pull in result["errors"][0]
    chunk = result["chunks"][0]
    if phase == "contacts":
        assert recovery.leads == [] and chunk["contacts_read"] == 0
        assert result["summary"]["paid_contacts_recovered"] == 0
    else:
        assert recovery.attribution == [] and chunk["deals_found"] == 0
        assert result["summary"]["closed_won_deals_found"] == 0
    assert recovery.store["completed"] == [], (
        "a chunk whose pull failed was checkpointed as completed — resume "
        "would skip it")

    # HubSpot recovers; the resumed job reads the chunk it could not read.
    del hubspot.faults[key]
    resumed = recovery.run(resume=True)
    assert resumed["status"] == "success" and resumed["errors"] == []
    assert len(resumed["chunks"]) == 1
    assert resumed["chunks"][0]["contacts_read"] == 230
    assert resumed["chunks"][0]["deals_found"] == 215
    assert len(recovery.store["completed"]) == 1


def test_32_the_mailchimp_identity_bridge_is_unavailable_not_partial(
        monkeypatch, hubspot):
    """It built the bridge from whatever the pull returned and called it
    available. Its own handler already withholds outcomes when the pull
    raises; the pull now gives it the chance."""
    from services.mailchimp_audit_service import _build_identity_index_live

    monkeypatch.setenv("HUBSPOT_API_KEY", "test-key-not-a-secret")
    hubspot.faults[("contacts.search", 1)] = Always(500)
    index, available, note = _build_identity_index_live(WINDOW_FROM, TODAY)
    assert (index, available) == ({}, False)
    assert "outcomes withheld" in note and "pull_all_contacts_in_range" in note

    del hubspot.faults[("contacts.search", 1)]
    index, available, _note = _build_identity_index_live(WINDOW_FROM, TODAY)
    assert available is True and len(index) == 270


# ═════════════════════════════════════════════════════════════════════════════
# 9 — the sources pull and the source-classification step
# ═════════════════════════════════════════════════════════════════════════════
def _sources_pull():
    return hp.pull_closed_won_deals_with_sources_in_range(
        WINDOW_FROM.isoformat(), TODAY.isoformat())


def test_33_an_unread_source_lookup_fails_the_step_instead_of_writing_unclassified(
        monkeypatch, hubspot):
    """The connector returns a deal whose contact sources it could not read
    with ``lookup_failed: True`` and no contacts, so the caller can withhold
    it. The incremental step never read the flag: it attributed the deal from
    its empty contact list, upserted it ``unclassified / no_classified_contact``
    over the stored attribution, and ended ``success``.

    Only the SOURCE read of the contact fails here; the click-evidence read of
    the same contact answers, so ``gclid/matches`` is unaffected."""
    run = _IncrementalRun(monkeypatch)
    did, cid, _position = _won_victim(hubspot)
    hubspot.faults[("contacts.source", cid)] = Always(500)
    result = run.run()

    written = {row["deal_id"]: row for rows in run.written["attributed"]
               for row in rows}
    entry = result["datasets"]["hubspot/source_classification"]
    assert entry["status"] == "failed", (
        f"the step ended {entry['status']!r} although deal {did}'s contact "
        f"sources were never read; it wrote that deal as "
        f"{(written.get(did) or {}).get('attribution_status')!r} / "
        f"{(written.get(did) or {}).get('attribution_reason')!r}")
    assert f"deal id(s): {did}" in entry["error"] and "1 of 215" in entry["error"]
    assert hubspot.attempts[("contacts.source", cid)] == hp.MAX_RETRIES

    # Nothing from that pull was written — not the unread deal, not the others.
    assert run.written["attributed"] == []
    (status, kwargs), = run.batches("hubspot", "source_classification")
    assert status == "failed" and kwargs.get("last_source_date") is None
    assert any(e.startswith("hubspot/source_classification: ")
               for e in result["errors"])
    # The run is partial, in the result and on the durable row.
    assert result["status"] == "partial"
    assert run.run_updates[-1][1]["status"] == "partial"
    assert result["datasets"]["gclid/matches"]["status"] == "success"


def test_34_the_sources_pull_marks_the_unread_deal_and_only_that_deal(hubspot):
    """The connector half of test_33, unchanged by this slice: the flag is on
    the one deal whose lookup failed, and every other deal is whole."""
    did, cid, _position = _won_victim(hubspot)
    hubspot.faults[("contacts.source", cid)] = Always(500)
    rows = _sources_pull()
    expected = hubspot.expected_sources()
    for row, want in zip(rows, expected, strict=True):
        if row["deal_id"] == did:
            assert row == {**want, "lookup_failed": True, "contacts": []}
        else:
            assert row == want


def test_35_the_sources_pull_reads_no_primary_contact(hubspot):
    """It needs each deal's id, close date and amount, then every associated
    contact's source. It used to get the deals through
    ``pull_closed_won_deals_in_range``, which also resolves each deal's primary
    contact for click evidence — a second association read and a full contact
    read per deal, both thrown away."""
    assert _sources_pull() == hubspot.expected_sources()
    primary = [oid for family, oid, kind in hubspot.reads if kind == "primary"]
    assert primary == [], (
        f"{len(primary)} primary-contact read(s) for a pull that reads sources")
    for did in hubspot.WON:
        assert hubspot.attempts[("assoc", "deals", did)] == 1, did
    assert {kind for _f, _o, kind in hubspot.reads} == {"source"}


def test_36_a_failed_click_evidence_lookup_fails_gclid_matches_and_not_the_source_step(
        monkeypatch, hubspot):
    """The other side of test_35. A click-evidence read that fails must fail
    the dataset that reads click evidence, and only that one."""
    run = _IncrementalRun(monkeypatch)
    did, cid, _position = _won_victim(hubspot)
    hubspot.faults[("contacts.primary", cid)] = Always(500)
    result = run.run()

    matches = result["datasets"]["gclid/matches"]
    assert matches["status"] == "failed"
    assert "pull_closed_won_deals_in_range" in matches["error"]
    assert f"deal {did}" in matches["error"]
    assert run.written["gclid"] == []
    sources = result["datasets"]["hubspot/source_classification"]
    assert sources["status"] == "success", sources
    assert [row["deal_id"] for row in run.written["attributed"][0]] == hubspot.WON
    assert result["status"] == "partial"


# ═════════════════════════════════════════════════════════════════════════════
# 10 — a 404 is an answer: raised, never retried
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("where", ["association", "contact"])
def test_37_a_404_on_a_won_deals_contact_lookup_raises_at_once_without_a_retry(
        where, hubspot):
    """HubSpot answered: the object is not there. Asking twice more, after 2 s
    and 4 s, gets the same answer. It still raises — ``gclid=None`` would claim
    the contact was read and carries no click evidence."""
    did, cid, position = _won_victim(hubspot)
    key = ("assoc", "deals", did) if where == "association" else ("contacts.get", cid)
    hubspot.faults[key] = Always(404)
    err = _must_raise_incomplete(_won_pull, pull="pull_closed_won_deals_in_range",
                                 records_read=position)
    assert f"deal {did}" in str(err) and _names_status(err, 404)
    assert hubspot.attempts[key] == 1
    assert hubspot.sleeps == []


def test_38_a_404_on_a_gclid_contacts_associations_raises_at_once_without_a_retry(
        hubspot):
    victim, before = _victim(hubspot)
    hubspot.faults[("assoc", "contacts", victim)] = Always(404)
    err = _must_raise_incomplete(_gclid_pull(hubspot), pull="pull_deals_with_gclid",
                                 records_read=before)
    assert victim in str(err) and _names_status(err, 404)
    assert hubspot.attempts[("assoc", "contacts", victim)] == 1
    assert hubspot.sleeps == []


@pytest.mark.parametrize("where", ["association", "source"])
def test_39_a_404_in_a_source_lookup_marks_the_deal_unread_without_a_retry(
        where, hubspot):
    did, cid, _position = _won_victim(hubspot)
    key = (("assoc", "deals", did) if where == "association"
           else ("contacts.source", cid))
    hubspot.faults[key] = Always(404)
    rows = {row["deal_id"]: row for row in _sources_pull()}
    assert rows[did]["lookup_failed"] is True and rows[did]["contacts"] == []
    assert sum(1 for row in rows.values() if row["lookup_failed"]) == 1
    assert hubspot.attempts[key] == 1
    assert hubspot.sleeps == []


# ═════════════════════════════════════════════════════════════════════════════
# 11 — a 200 that is not an association list is not "nothing associated"
# ═════════════════════════════════════════════════════════════════════════════
def _unusable_payloads(first_id):
    return {
        "no results key": {},
        "a reference with no id": {"results": [
            {"toObjectId": int(first_id), "associationTypes": []},
            {"associationTypes": []}]},
        "results is not a list": {"results": "none"},
        "not an object": [],
    }


UNUSABLE = sorted(_unusable_payloads(0))


@pytest.mark.parametrize("shape", UNUSABLE)
def test_40_an_unusable_association_response_for_a_gclid_contact_raises(
        shape, hubspot):
    """``payload.get("results", [])`` read a 200 with no ``results`` key as "no
    deals", and a reference with no id was skipped: the pull returned the list
    without that contact's deals and without an error."""
    victim, before = _victim(hubspot)
    first_deal = hubspot.contact_deals[victim][0]
    hubspot.assoc_payload[("contacts", victim)] = _unusable_payloads(first_deal)[shape]
    err = _must_raise_incomplete(_gclid_pull(hubspot), pull="pull_deals_with_gclid",
                                 records_read=before)
    assert victim in str(err) and "association" in str(err)
    assert hubspot.attempts[("assoc", "contacts", victim)] == 1
    assert hubspot.sleeps == []


@pytest.mark.parametrize("shape", UNUSABLE)
def test_41_an_unusable_association_response_for_a_won_deal_raises(shape, hubspot):
    """Here the dropped record was the deal's click evidence: the deal came
    back with ``gclid=None``."""
    did, cid, position = _won_victim(hubspot)
    hubspot.assoc_payload[("deals", did)] = _unusable_payloads(cid)[shape]

    def what_came_back(rows):
        row = next(r for r in rows if r["deal_id"] == did)
        return f"deal {did} came back with gclid={row['gclid']!r}"
    err = _must_raise_incomplete(_won_pull, pull="pull_closed_won_deals_in_range",
                                 records_read=position, describe=what_came_back)
    assert f"deal {did}" in str(err) and "association" in str(err)
    assert hubspot.attempts[("assoc", "deals", did)] == 1


def test_42_an_explicitly_empty_results_list_is_still_a_valid_empty_answer(hubspot):
    """The control for 40 and 41: ``{"results": []}`` is HubSpot saying
    nothing is associated, and stays a finding."""
    victim, _before = _victim(hubspot)
    hubspot.assoc_payload[("contacts", victim)] = {"results": []}
    result = _gclid_pull(hubspot)()
    assert result == [d for d in hubspot.expected_gclid_deals()
                      if d["contact_id"] != victim]

    did, _cid, _position = _won_victim(hubspot)
    hubspot.assoc_payload[("deals", did)] = {"results": []}
    row = next(r for r in _won_pull() if r["deal_id"] == did)
    assert row["contact_id"] is None and row["gclid"] is None
    source = next(r for r in _sources_pull() if r["deal_id"] == did)
    assert source["lookup_failed"] is False and source["contacts"] == []


@pytest.mark.parametrize("shape", UNUSABLE + ["a second page"])
def test_43_an_unusable_association_response_marks_the_source_lookup_failed(
        shape, hubspot):
    """For the sources pull the silent version was the worst one: the deal
    came back ``lookup_failed: False`` with no contacts — a SUCCESSFUL empty
    association, which the caller writes as Unclassified."""
    did, cid, _position = _won_victim(hubspot)
    if shape == "a second page":
        hubspot.assoc_has_next_page.add(("deals", did))
    else:
        hubspot.assoc_payload[("deals", did)] = _unusable_payloads(cid)[shape]
    rows = {row["deal_id"]: row for row in _sources_pull()}
    assert rows[did]["lookup_failed"] is True, rows[did]
    assert rows[did]["contacts"] == []
    assert sum(1 for row in rows.values() if row["lookup_failed"]) == 1


# ═════════════════════════════════════════════════════════════════════════════
# 12 — transport errors, and a timeout on every SDK read
# ═════════════════════════════════════════════════════════════════════════════
def _refused():
    """What the SDK lets through when no response arrives. Its REST layer
    converts only a urllib3 SSLError; this one reaches the connector raw."""
    return urllib3.exceptions.MaxRetryError(
        None, "/crm/v3/objects/search", "connection refused")


def _timed_out():
    return urllib3.exceptions.ReadTimeoutError(
        None, "/crm/v3/objects", "Read timed out. (read timeout=30)")


@each_search_pull
def test_44_a_transport_error_on_a_search_page_raises_the_named_error(pull, hubspot):
    """It escaped as a raw urllib3 exception: loud, but not the error the
    callers and the run record name, and without the record count."""
    family, call, _expected = SEARCH_PULLS[pull]
    refused = _refused()
    hubspot.faults[(f"{family}.search", 1)] = Always(refused)
    err = _must_raise_incomplete(call, pull=pull, records_read=100)
    assert err.cause is refused and err.__cause__ is refused
    assert "MaxRetryError" in str(err)
    assert hubspot.attempts[(f"{family}.search", 1)] == 1


def test_45_a_transport_error_on_a_deal_read_raises_the_named_error(hubspot):
    victim, before = _victim(hubspot)
    second_deal = hubspot.contact_deals[victim][1]
    timed_out = _timed_out()
    hubspot.faults[("deals.get", second_deal)] = Always(timed_out)
    err = _must_raise_incomplete(_gclid_pull(hubspot), pull="pull_deals_with_gclid",
                                 records_read=before)
    assert err.cause is timed_out and victim in str(err)


def test_46_a_transport_error_on_a_won_deals_contact_read_is_retried_then_raised(
        hubspot):
    did, cid, position = _won_victim(hubspot)
    hubspot.faults[("contacts.get", cid)] = [_timed_out()]
    assert _won_pull() == hubspot.expected_won_rows()       # one timeout: retried

    hubspot.faults[("contacts.get", cid)] = Always(_refused())
    hubspot.attempts.clear()
    err = _must_raise_incomplete(_won_pull, pull="pull_closed_won_deals_in_range",
                                 records_read=position)
    assert f"deal {did}" in str(err) and "MaxRetryError" in str(err)
    assert hubspot.attempts[("contacts.get", cid)] == hp.MAX_RETRIES


def test_47_a_transport_error_on_a_source_read_marks_the_deal_unread(hubspot):
    did, cid, _position = _won_victim(hubspot)
    hubspot.faults[("contacts.source", cid)] = Always(_refused())
    rows = {row["deal_id"]: row for row in _sources_pull()}
    assert rows[did]["lookup_failed"] is True and rows[did]["contacts"] == []
    assert hubspot.attempts[("contacts.source", cid)] == hp.MAX_RETRIES


def test_48_every_sdk_read_these_pulls_make_carries_a_request_timeout(hubspot):
    """The SDK applies no timeout unless it is given one, so a HubSpot that
    stopped answering held a pull — and the job around it — open without
    limit. The REST calls already passed ``timeout=30``."""
    contacts = hp.pull_paid_search_contacts(days_back=30)
    hp.pull_paid_search_contacts_in_range(WINDOW_FROM.isoformat(), TODAY.isoformat())
    hp.pull_all_contacts_in_range(WINDOW_FROM.isoformat(), TODAY.isoformat())
    hp.pull_deals_with_gclid(contacts)
    _won_pull()
    _sources_pull()

    assert {api for api, _timeout in hubspot.timeouts} == {
        "contacts.search", "deals.search", "contacts.get", "deals.get"}
    without = sorted({api for api, timeout in hubspot.timeouts if not timeout})
    assert without == [], f"SDK calls made with no timeout: {without}"
    assert {timeout for _api, timeout in hubspot.timeouts} == {
        getattr(hp, "HUBSPOT_REQUEST_TIMEOUT_SECONDS", None)} == {30}


@each_search_pull
def test_49_a_search_page_with_no_results_list_raises_the_named_error(pull, hubspot):
    """A 200 whose body has no `results` key reaches the pull as
    ``results=None``. It is not an empty page: the pull must not return the
    100 records it had, and must not die on a bare ``TypeError``."""
    family, call, _expected = SEARCH_PULLS[pull]
    hubspot.faults[(f"{family}.search", 1)] = Always(NO_RESULTS_LIST)
    err = _must_raise_incomplete(call, pull=pull, records_read=100)
    assert "no `results` list" in str(err)
    assert hubspot.attempts[(f"{family}.search", 1)] == 1     # not retried
