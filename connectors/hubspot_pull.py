"""
HubSpot CRM Connector
Pulls contacts (paid search source), deals, and pipeline data.
Confirmed field names from live account audit — April 2026.
"""

import logging
import os
import json
import math
import time
from datetime import datetime, timedelta
import requests
import urllib3
import hubspot
from hubspot.crm.contacts import ApiException
from hubspot.crm.deals import ApiException as DealsApiException
from hubspot.crm.companies import ApiException as CompaniesApiException
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY")

HUBSPOT_API_BASE_URL = "https://api.hubapi.com"
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 2


class HubSpotRetryableError(Exception):
    """A transient/HTTP HubSpot failure that must be retried, never silently
    treated as an empty/Unclassified result (PR-ADS-117)."""


class HubSpotSearchStalledError(Exception):
    """The contact scan cannot prove it reached the end of the result set.

    Raised when more than one full page of contacts shares an identical
    ``lastmodifieddate`` at HubSpot's 10,000-result paging boundary, so
    re-anchoring the watermark cannot advance. Returning normally here would let
    a caller mark a historical bootstrap COMPLETE while an unknown number of
    contacts were never read (PR-ADS-153B truth-safety §4).
    """


class HubSpotSearchIncompleteError(Exception):
    """A contact scan reached its end with fewer contacts than HubSpot counts.

    PR-ADS-161I. HubSpot reports ``total`` (the size of the result set) on every
    search response. A scan that ends having yielded materially fewer unique
    contacts than the FIRST query's ``total`` skipped some, whatever the reason,
    and must never be reported as complete. Raised instead of the completion
    sentinel, so a bootstrap stays partial and an incremental run fails.
    """


class HubSpotSearchPageUnreadableError(Exception):
    """A search page came back as a 200 that cannot be read (no ``results``
    list).

    PR-ADS-166B (review round 2). Deliberately NOT a
    ``HubSpotSearchIncompleteError``. That one proves a GAP somewhere in what
    was read, so callers rewind (contacts) or hold the watermark (deals). An
    unreadable page proves nothing about the pages before it: under keyset
    paging every record not yet read still sorts at or after the newest one
    read, so the prefix keeps its checkpoint and the run is retried from it.
    """


#: PR-ADS-163A. The SDK generates one ``ApiException`` PER API package, with no
#: common base: ``hubspot.crm.deals`` raises its own class, which
#: ``except ApiException`` (the contacts class imported above) does not catch.
#: The legacy pulls call both APIs, so they catch both.
#:
#: PR-ADS-166A (review round 2). ``hubspot.crm.companies`` is a third package
#: with a third class. The company name read caught only the contacts class,
#: so a 403 from the companies API was retried three times and never stopped
#: the run, and a 404 naming the missing ids was never read as an answer.
HUBSPOT_API_EXCEPTIONS = (ApiException, DealsApiException,
                          CompaniesApiException)

#: What the SDK lets through from the transport: a refused connection, a reset
#: or a timeout (``MaxRetryError``, ``ReadTimeoutError`` …) escapes as the raw
#: urllib3 exception. A TLS failure is NOT covered: SDK 13.0.0 turns a urllib3
#: ``SSLError`` into ``ApiException(status=0)`` and then fails inside its own
#: error handling with ``AttributeError: 'NoneType' object has no attribute
#: 'decode'``. That is loud — the pull does not return — but it is not the
#: named error, and it is deliberately not caught here: catching
#: ``AttributeError`` would hide real defects.
HUBSPOT_TRANSPORT_EXCEPTIONS = (urllib3.exceptions.HTTPError,)

#: Every way an SDK read can fail: the API answered with an error, or the
#: request never got an answer.
HUBSPOT_SDK_READ_ERRORS = HUBSPOT_API_EXCEPTIONS + HUBSPOT_TRANSPORT_EXCEPTIONS

#: Passed as ``_request_timeout`` on every SDK call the legacy pulls make. The
#: SDK applies no timeout of its own, so a HubSpot that stopped answering held
#: the pull open with no limit. Matches the ``timeout=30`` of the REST calls.
HUBSPOT_REQUEST_TIMEOUT_SECONDS = 30


def _is_not_found(exc) -> bool:
    """A 404, from the SDK or from ``requests``. HubSpot answered: the object
    is not there. Asking again gets the same answer, so it is never retried."""
    if getattr(exc, "status", None) == 404:
        return True
    return getattr(getattr(exc, "response", None), "status_code", None) == 404


class _UnusableAssociationResponse(Exception):
    """A 200 from the v4 associations API that cannot be read as the whole
    answer: not an association list, or one that continues on another page.
    Internal: each caller converts it to its own named error."""


def _association_object_ids(payload, *, reject_next_page: bool) -> list:
    """The associated object ids in a v4 association list response.

    ``{"results": []}`` is a valid answer: nothing is associated. A response
    with no ``results`` key, or a reference with no object id, is NOT an empty
    answer — reading it as one drops records without an error — and raises.
    ``reject_next_page``: these readers request one page, so a response that
    announces another is a list they would return short.
    """
    if not isinstance(payload, dict) or "results" not in payload:
        raise _UnusableAssociationResponse(
            "the response has no `results` key")
    results = payload["results"]
    if not isinstance(results, list):
        raise _UnusableAssociationResponse("`results` is not a list")
    paging = payload.get("paging")
    if reject_next_page and isinstance(paging, dict) and paging.get("next"):
        raise _UnusableAssociationResponse(
            "more than one page of associations (only the first is read)")
    ids = []
    for ref in results:
        object_id = (ref.get("toObjectId") or ref.get("id")) \
            if isinstance(ref, dict) else None
        if not object_id:
            raise _UnusableAssociationResponse(
                "an association reference has no object id")
        ids.append(str(object_id))
    return ids


def _describe_hubspot_failure(exc) -> str:
    """A short, log-safe description of why a HubSpot read failed."""
    if exc is None:
        return "unknown failure"
    status = getattr(exc, "status", None)
    if status is not None:          # an SDK ApiException
        reason = getattr(exc, "reason", None)
        return f"HubSpot API error HTTP {status}" + (f" ({reason})" if reason else "")
    return f"{type(exc).__name__}: {exc}"[:300]


class HubSpotPullIncompleteError(Exception):
    """A legacy pull could not read its whole result set.

    PR-ADS-163A. The three contact pulls (``pull_paid_search_contacts``,
    ``pull_paid_search_contacts_in_range``, ``pull_all_contacts_in_range``)
    used to log a HubSpot error, stop, and RETURN what they had: ``[]`` when
    the first page failed, the first pages when a later one did. A caller
    cannot tell that list from a complete one, so with an expired token the
    monthly job recorded ``success``, stamped ``hubspot/contacts`` fresh and
    overwrote ``data/crm_contacts.json`` with ``[]``. ``pull_deals_with_gclid``
    skipped a contact whose associations it could not read, and
    ``pull_closed_won_deals_in_range`` returned a won deal whose contact it
    could not read as a deal with no click evidence.

    These five pulls now raise this on an API or transport error, an exhausted
    retry, or a malformed response, and the records already read are discarded.
    That is not a proof of completeness: they page by offset and never read
    HubSpot's ``total``, so a record deleted or re-sorted while the scan runs
    can still shift a page (the PR-ADS-161I failure). Not addressed here.

    ``pull``          the function that could not finish
    ``records_read``  how many records it had read before the failure
    ``cause``         the underlying exception, or None
    """

    def __init__(self, pull: str, records_read: int, cause=None,
                 detail: str | None = None):
        self.pull = pull
        self.records_read = int(records_read)
        self.cause = cause
        self.detail = detail or _describe_hubspot_failure(cause)
        super().__init__(
            f"{pull} could not return a complete result: {self.detail}. "
            f"{self.records_read} record(s) read before the failure were "
            "discarded, not returned")


# Fields confirmed live from Logistaas HubSpot account
CONTACT_PROPERTIES = [
    "firstname",
    "lastname",
    "email",
    "company",
    "hs_google_click_id",          # GCLID — confirmed populated
    "mql_status",                  # OPEN-Connecting, CLOSED-JobSeeker etc.
    "hs_lead_status",
    "lifecyclestage",
    "hs_analytics_source",
    "hs_analytics_source_data_1",  # Campaign name (UTM)
    "hs_analytics_source_data_2",  # Keyword (UTM)
    "hs_latest_source",
    "hs_latest_source_data_1",
    "hs_latest_source_data_2",
    "hs_analytics_first_url",
    "ip_country",
    "country",
    "createdate",
    "hubspot_owner_id",
    "mql___mdr_comments",
    "search_terms",
]

# HubSpot deal stage IDs from live account
DEAL_STAGE_MAP = {
    "qualifiedtobuy":  "Proposal / Implementation Plan",
    "334269159":       "In Trials",
    "326093513":       "Pricing Acceptance",
    "326093515":       "Invoice Agreement Sent",
    "379260140":       "Unresponsive",
    "326093516":       "Deal Won / Payment Received",
    "379124201":       "Lost Deal",
    "379124202":       "Downgrade Deal",
    "379124203":       "Churn Deal",
}

ACTIVE_DEAL_STAGES = ["qualifiedtobuy", "334269159", "326093513", "326093515"]
WON_DEAL_STAGES = ["326093516"]
LOST_DEAL_STAGES = ["379124201", "379124202", "379124203", "379260140"]


# ---------------------------------------------------------------------------
# PR-ADS-153B — canonical CRM funnel contact properties
# ---------------------------------------------------------------------------
# HubSpot Lifecycle Stage owns the funnel. These properties carry the canonical
# stage-entry evidence (`hs_v2_date_entered_*`) that replaces the previous
# "createdate is the event date for every stage" proxy, plus `lastmodifieddate`
# which drives a true modification-watermark sync (a contact created two years
# ago and qualified today MUST be refreshed today).
#
# `mql___mdr_comments` is deliberately ABSENT: PR-ADS-153B §15 removed the
# `mql_status or mql___mdr_comments` fallback, so MDR free text can never again
# be written into the typed status property.
CONTACT_FUNNEL_PROPERTIES = [
    # Identity / lifecycle truth
    "lifecyclestage",
    "hs_lead_status",
    "mql_status",
    "createdate",
    "lastmodifieddate",
    # Canonical stage-entry timestamps
    "hs_v2_date_entered_lead",
    "hs_v2_date_entered_marketingqualifiedlead",
    "hs_v2_date_entered_salesqualifiedlead",
    "hs_v2_date_entered_opportunity",
    "hs_v2_date_entered_customer",
    # Acquisition-source evidence (existing attribution doctrine)
    "hs_analytics_source",
    "hs_analytics_source_data_1",
    "hs_analytics_source_data_2",
    "hs_latest_source",
    "hs_latest_source_data_1",
    "hs_latest_source_data_2",
    "hs_analytics_first_url",
    "hs_google_click_id",
    # Geography / firmographics used by country + source attribution
    "ip_country",
    "country",
    "company",
    "hubspot_owner_id",
    # PR-ADS-166A — the contact's own name, so the Leads page can say who a
    # row is. Read-only, like every property here. `email` stays absent.
    "firstname",
    "lastname",
]

# HubSpot's CRM search API refuses to page beyond 10,000 results for one query.
# The sync re-anchors its watermark instead of paging past this (see
# ``iter_contacts_modified_since``).
HUBSPOT_SEARCH_RESULT_CAP = 10000
_SEARCH_PAGE_SIZE = 100


def get_client():
    if not HUBSPOT_API_KEY:
        raise RuntimeError("HUBSPOT_API_KEY is not set")
    return hubspot.Client.create(access_token=HUBSPOT_API_KEY)


def _legacy_search_page(pull: str, records_read: int, search):
    """One page of a legacy search pull: the whole page, or an exception.

    PR-ADS-163A. ``search`` performs the request. A 429 is retried at most
    ``MAX_RETRIES`` times with exponential backoff — ``MAX_RETRIES + 1``
    attempts per page, the budget ``pull_deals_for_ledger`` uses — and then
    raised: these loops used to wait and retry a 429 with no limit. Any other
    API error, and any transport error the SDK lets through (a refused
    connection, a timeout), is raised at once as the same named error. Nothing
    is returned for a page that was not read.

    A 200 that carries no ``results`` list (the SDK returns ``results=None``
    for a body without that key) is not an empty page: it raises too.
    """
    for attempt in range(MAX_RETRIES + 1):
        try:
            page = search()
            if not isinstance(getattr(page, "results", None), list):
                detail = "the search response carries no `results` list"
                logger.error(
                    "%s failed after reading %d record(s): %s",
                    pull, records_read, detail)
                raise HubSpotPullIncompleteError(pull, records_read, None, detail)
            return page
        except HUBSPOT_SDK_READ_ERRORS as exc:
            status = getattr(exc, "status", None)
            if status == 429 and attempt < MAX_RETRIES:
                wait = INITIAL_BACKOFF_SECONDS * (2 ** attempt)
                logger.warning(
                    "HubSpot rate limited during %s — retry %d/%d in %ds",
                    pull, attempt + 1, MAX_RETRIES, wait,
                )
                time.sleep(wait)
                continue
            detail = _describe_hubspot_failure(exc)
            if status == 429:
                detail = f"still rate limited (HTTP 429) after {attempt + 1} attempts"
            logger.error(
                "%s failed after reading %d record(s): %s", pull, records_read, detail)
            raise HubSpotPullIncompleteError(pull, records_read, exc, detail) from exc
    raise HubSpotPullIncompleteError(      # unreachable: the loop returns or raises
        pull, records_read, None, "retry budget exhausted")


def pull_paid_search_contacts(days_back: int = 90) -> list:
    """
    Pull all contacts with source = PAID_SEARCH from the last N days.
    These are the contacts we can reconcile with Google Ads via GCLID.

    Raises ``HubSpotPullIncompleteError`` (PR-ADS-163A) when a page cannot be
    read: it is never answered with the pages before it, and never with ``[]``.
    Pages by offset without reading ``total``; see that class for the limit.
    """
    client = get_client()
    cutoff = datetime.utcnow() - timedelta(days=days_back)
    cutoff_ts = int(cutoff.timestamp() * 1000)

    contacts = []
    after = None

    while True:
        response = _legacy_search_page(
            "pull_paid_search_contacts", len(contacts),
            lambda: client.crm.contacts.search_api.do_search(
                public_object_search_request={
                    "filterGroups": [
                        {
                            "filters": [
                                {
                                    "propertyName": "hs_analytics_source",
                                    "operator": "EQ",
                                    "value": "PAID_SEARCH"
                                },
                                {
                                    "propertyName": "createdate",
                                    "operator": "GTE",
                                    "value": str(cutoff_ts)
                                }
                            ]
                        }
                    ],
                    "properties": CONTACT_PROPERTIES,
                    "limit": 100,
                    "after": after
                },
                _request_timeout=HUBSPOT_REQUEST_TIMEOUT_SECONDS,
            ),
        )

        contacts.extend([c.to_dict() for c in response.results])

        if response.paging and response.paging.next:
            after = response.paging.next.after
        else:
            break

    logger.info("Pulled %d paid search contacts (last %d days)", len(contacts), days_back)
    return contacts


def pull_paid_search_contacts_in_range(date_from: str, date_to: str) -> list:
    """Pull paid-search contacts created within an explicit date range.

    Used by the historical backfill framework (PR-ADS-071).
    Filters by hs_analytics_source = PAID_SEARCH and createdate GTE/LTE.
    Paginates using the HubSpot CRM search API cursor.
    Raises ``HubSpotPullIncompleteError`` (PR-ADS-163A) when a page cannot be
    read, instead of returning the pages before it.
    NEVER writes to HubSpot.
    """
    client = get_client()

    # HubSpot createdate is millisecond epoch timestamps
    from datetime import timezone, time as _time
    from datetime import date as _date

    def _to_ms(d: str, start: bool) -> int:
        """Convert ISO date to millisecond epoch (start-of-day or end-of-day UTC)."""
        parsed = _date.fromisoformat(d)
        if start:
            dt = datetime.combine(parsed, _time.min, tzinfo=timezone.utc)
        else:
            dt = datetime.combine(parsed, _time.max, tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)

    from_ms = _to_ms(str(date_from), start=True)
    to_ms   = _to_ms(str(date_to),   start=False)

    contacts = []
    after = None

    while True:
        response = _legacy_search_page(
            "pull_paid_search_contacts_in_range", len(contacts),
            lambda: client.crm.contacts.search_api.do_search(
                public_object_search_request={
                    "filterGroups": [
                        {
                            "filters": [
                                {
                                    "propertyName": "hs_analytics_source",
                                    "operator": "EQ",
                                    "value": "PAID_SEARCH",
                                },
                                {
                                    "propertyName": "createdate",
                                    "operator": "GTE",
                                    "value": str(from_ms),
                                },
                                {
                                    "propertyName": "createdate",
                                    "operator": "LTE",
                                    "value": str(to_ms),
                                },
                            ]
                        }
                    ],
                    "properties": CONTACT_PROPERTIES,
                    "limit": 100,
                    "after": after,
                },
                _request_timeout=HUBSPOT_REQUEST_TIMEOUT_SECONDS,
            ),
        )
        contacts.extend([c.to_dict() for c in response.results])

        if response.paging and response.paging.next:
            after = response.paging.next.after
        else:
            break

    logger.info(
        "Pulled %d paid search contacts (range %s → %s)",
        len(contacts), date_from, date_to,
    )
    return contacts


def pull_deals_with_gclid(contacts: list) -> list:
    """
    For contacts that have a GCLID, pull their associated deals.
    This gives us the full ad click → pipeline journey.

    Uses the HubSpot CRM v4 associations REST API directly (version-agnostic).
    Endpoint: GET /crm/v4/objects/contacts/{contact_id}/associations/deals
    This avoids SDK breakage when hubspot-api-client (>=9.0.0) reorganises its
    associations interface between minor versions.

    Returns the deals of EVERY GCLID contact or raises
    ``HubSpotPullIncompleteError`` (PR-ADS-163A). A contact whose associations
    or deals could not be read used to be logged and skipped, so the list
    silently omitted its deals.
    """
    client = get_client()
    gclid_contacts = [
        c for c in contacts
        if c.get("properties", {}).get("hs_google_click_id")
    ]

    deals = []
    for contact in gclid_contacts:
        contact_id = contact["id"]
        gclid = contact["properties"]["hs_google_click_id"]
        deals.extend(
            _deals_for_gclid_contact(client, contact_id, gclid, len(deals)))

    logger.info("Found %d deals linked to GCLID contacts", len(deals))
    return deals


def _deals_for_gclid_contact(client, contact_id, gclid, records_read: int) -> list:
    """Every deal associated with one GCLID contact, or an exception.

    PR-ADS-163A. Retries a 429 and a transient request error up to
    ``MAX_RETRIES`` attempts, as before. What changed is the end of the budget:
    it raises ``HubSpotPullIncompleteError`` instead of logging and moving on to
    the next contact. The deals are collected per attempt and handed back only
    when the contact was read in full, so a retry cannot return a deal twice.

    A 404 is an answer and is not retried. A 200 without a ``results`` key, or
    with a reference that has no deal id, raises: both used to be read as "no
    deals" and the contact's deals dropped without an error.
    """
    pull = "pull_deals_with_gclid"

    def _incomplete(exc, what: str):
        detail = f"{what} for contact {contact_id}"
        if exc is not None:
            detail += f": {_describe_hubspot_failure(exc)}"
        logger.error("%s failed after reading %d deal(s): %s",
                     pull, records_read, detail)
        return HubSpotPullIncompleteError(pull, records_read, exc, detail)

    for attempt in range(1, MAX_RETRIES + 1):
        contact_deals = []
        try:
            # Use REST API directly — version-agnostic, never breaks on SDK upgrades
            assoc_url = (
                f"{HUBSPOT_API_BASE_URL}/crm/v4/objects/contacts"
                f"/{contact_id}/associations/deals"
            )
            headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}"}
            resp = requests.get(assoc_url, headers=headers, timeout=30)

            if resp.status_code == 429 and attempt < MAX_RETRIES:
                wait = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "HubSpot rate limited on associations — retry %d/%d in %ds",
                    attempt, MAX_RETRIES, wait,
                )
                time.sleep(wait)
                continue

            resp.raise_for_status()
            # TODO PR-ADS-028: add associations pagination. Until then a second
            # page is refused, not dropped.
            try:
                deal_ids = _association_object_ids(
                    resp.json(), reject_next_page=True)
            except _UnusableAssociationResponse as bad:
                raise _incomplete(
                    None, f"unusable deal-association response ({bad})") from bad

            for deal_id in deal_ids:
                deal = client.crm.deals.basic_api.get_by_id(
                    deal_id=deal_id,
                    properties=["dealname", "dealstage", "amount",
                               "closedate", "createdate", "pipeline",
                               "hs_deal_stage_probability"],
                    _request_timeout=HUBSPOT_REQUEST_TIMEOUT_SECONDS,
                )
                deal_dict = deal.to_dict()
                deal_dict["gclid"] = gclid
                deal_dict["contact_id"] = contact_id
                deal_dict["stage_label"] = DEAL_STAGE_MAP.get(
                    deal_dict.get("properties", {}).get("dealstage", ""), "Unknown"
                )
                contact_deals.append(deal_dict)
            return contact_deals  # the contact was read in full

        except requests.exceptions.RequestException as exc:
            if attempt < MAX_RETRIES and not _is_not_found(exc):
                wait = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "Transient error fetching associations for contact %s — retry %d/%d in %ds: %s",
                    contact_id, attempt, MAX_RETRIES, wait, exc,
                )
                time.sleep(wait)
            else:
                raise _incomplete(
                    exc, f"deal associations could not be read after "
                         f"{attempt} attempt(s)") from exc
        except HUBSPOT_SDK_READ_ERRORS as exc:
            if getattr(exc, "status", None) == 429 and attempt < MAX_RETRIES:
                wait = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "HubSpot rate limited on deal fetch — retry %d/%d in %ds",
                    attempt, MAX_RETRIES, wait,
                )
                time.sleep(wait)
            else:
                raise _incomplete(exc, "a deal could not be fetched") from exc

    raise HubSpotPullIncompleteError(      # unreachable: the loop returns or raises
        pull, records_read, None,
        f"retry budget exhausted for contact {contact_id}")


def _iter_closed_won_deal_pages(pull: str, date_from: str, date_to: str):
    """Yield closed-won deals by **closedate**, one page of raw deal dicts at a
    time — the deal search and nothing else.

    PR-ADS-163A. The ONE won-deal search, shared by
    ``pull_closed_won_deals_in_range`` (which then resolves each deal's click
    evidence) and ``pull_closed_won_deals_with_sources_in_range`` (which reads
    every associated contact's source and has no use for click evidence). The
    second used to get its deals through the first, so a lookup it discarded
    could fail it.

    ``pull`` names the caller in ``HubSpotPullIncompleteError``. The window is
    the deal's close date and nothing else.
    """
    client = get_client()

    from datetime import timezone, time as _time
    from datetime import date as _date

    def _to_ms(d: str, start: bool) -> int:
        parsed = _date.fromisoformat(d)
        edge = _time.min if start else _time.max
        return int(datetime.combine(parsed, edge, tzinfo=timezone.utc).timestamp() * 1000)

    from_ms = _to_ms(str(date_from), start=True)
    to_ms = _to_ms(str(date_to), start=False)

    read = 0
    after = None
    while True:
        response = _legacy_search_page(
            pull, read,
            lambda: client.crm.deals.search_api.do_search(
                public_object_search_request={
                    "filterGroups": [
                        {
                            "filters": [
                                {"propertyName": "dealstage", "operator": "IN",
                                 "values": WON_DEAL_STAGES},
                                {"propertyName": "closedate", "operator": "GTE",
                                 "value": str(from_ms)},
                                {"propertyName": "closedate", "operator": "LTE",
                                 "value": str(to_ms)},
                            ]
                        }
                    ],
                    "properties": ["dealname", "dealstage", "amount", "closedate",
                                   "createdate", "pipeline"],
                    "limit": 100,
                    "after": after,
                },
                _request_timeout=HUBSPOT_REQUEST_TIMEOUT_SECONDS,
            ),
        )
        page = [d.to_dict() for d in response.results]
        read += len(page)
        yield page
        if response.paging and response.paging.next:
            after = response.paging.next.after
        else:
            break


def pull_closed_won_deals_in_range(date_from: str, date_to: str) -> list:
    """Pull HubSpot closed-won deals by **closedate** (revenue truth) — PR-ADS-114.

    This is the revenue-recovery / daily revenue source. It fetches deals
    DIRECTLY by dealstage = Deal Won / Payment Received and closedate in range —
    it does NOT walk recently-created contacts (which misses historical revenue).

    For each won deal it resolves the associated contact and captures the real
    GCLID / campaign / keyword / country evidence. NO synthetic GCLID is ever
    created: deals without click evidence are returned with gclid=None so the
    caller can report (and never attribute) them honestly.

    Raises ``HubSpotPullIncompleteError`` (PR-ADS-163A) when a page of deals
    cannot be read, and when a deal's contact cannot be read: gclid=None means
    the contact was read and carries no click evidence, never that the lookup
    failed.

    Returns a list of normalised dicts:
        {deal_id, contact_id, gclid, campaign_name, keyword, country, company,
         deal_close_date, deal_amount_usd, deal_stage, deal_stage_label}

    NEVER writes to HubSpot.
    """
    deals: list = []
    for page in _iter_closed_won_deal_pages(
            "pull_closed_won_deals_in_range", date_from, date_to):
        for deal_dict in page:
            try:
                deals.append(_normalise_won_deal(deal_dict))
            except HubSpotRetryableError as exc:
                # The deal was read but its contact — the only place its click
                # evidence lives — was not. Returning it with gclid=None would
                # report "no click evidence" for a lookup that never answered.
                logger.error(
                    "pull_closed_won_deals_in_range failed after reading %d "
                    "deal(s): %s", len(deals), exc)
                raise HubSpotPullIncompleteError(
                    "pull_closed_won_deals_in_range", len(deals), exc,
                    str(exc)) from exc

    logger.info(
        "Pulled %d closed-won deals by closedate (range %s → %s)",
        len(deals), date_from, date_to,
    )
    return deals


def _normalise_won_deal(deal_dict: dict) -> dict:
    """Normalise a raw HubSpot deal into a recovery row, resolving its contact.

    Click evidence is read from the associated contact:
      - direct ``hs_google_click_id``           → match_source "gclid"
      - GCLID extracted from ``hs_analytics_first_url`` → match_source "first_url"
    Missing evidence stays None (gclid=None, match_source=None) — never fabricated.
    """
    from connectors.gclid_match import _extract_gclid_from_url  # noqa: PLC0415

    props = deal_dict.get("properties", {}) or {}
    deal_id = deal_dict.get("id")
    stage = props.get("dealstage", "")

    contact = _fetch_primary_contact_for_deal(deal_id)
    cprops = (contact or {}).get("properties", {}) if contact else {}

    first_url = cprops.get("hs_analytics_first_url") or ""
    direct_gclid = (cprops.get("hs_google_click_id") or "").strip()
    if direct_gclid:
        gclid, match_source = direct_gclid, "gclid"
    else:
        url_gclid = _extract_gclid_from_url(first_url)
        if url_gclid:
            gclid, match_source = url_gclid, "first_url"
        else:
            gclid, match_source = None, None

    return {
        "deal_id": str(deal_id) if deal_id is not None else None,
        "contact_id": (contact or {}).get("id"),
        "gclid": gclid,
        "match_source": match_source,
        "first_url": first_url or None,
        "campaign_name": cprops.get("hs_analytics_source_data_1") or None,
        "keyword": cprops.get("hs_analytics_source_data_2") or None,
        "country": cprops.get("ip_country") or cprops.get("country") or None,
        "company": cprops.get("company") or None,
        "deal_close_date": props.get("closedate") or None,
        "deal_amount_usd": props.get("amount"),
        "deal_stage": stage or None,
        # PR-ADS-153E-A: an UNKNOWN stage id stays explicitly unknown. Labelling
        # it "Deal Won / Payment Received" (the previous default) meant an
        # unrecognised stage that slipped past the filter was read as revenue by
        # the downstream `deal_stage_label ILIKE '%won%'` predicate.
        "deal_stage_label": DEAL_STAGE_MAP.get(stage) or (
            f"Unknown stage ({stage})" if stage else "Unknown stage"),
    }


def _fetch_primary_contact_for_deal(deal_id) -> dict | None:
    """Return the first associated contact (with properties) for a deal, or None.

    Uses the version-agnostic CRM v4 associations REST endpoint, then fetches the
    contact's properties. Read-only.

    None means the lookup ANSWERED ``results: []``: the deal has no associated
    contact. PR-ADS-163A: a lookup that failed used to return None too, so a
    rate-limited won deal came back as a deal with no click evidence. It now
    retries up to ``MAX_RETRIES`` attempts, the way
    ``_fetch_contact_source_props`` does, and then raises
    ``HubSpotRetryableError``. Two things are raised without a retry: a 404
    (HubSpot answered; the answer will not change) and a 200 that is not an
    association list (no ``results`` key, or a reference with no contact id).
    """
    if not deal_id:
        return None
    client = get_client()
    assoc_url = (
        f"{HUBSPOT_API_BASE_URL}/crm/v4/objects/deals/{deal_id}/associations/contacts"
    )
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}"}
            resp = requests.get(assoc_url, headers=headers, timeout=30)
            if resp.status_code == 429 and attempt < MAX_RETRIES:
                time.sleep(INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue
            resp.raise_for_status()
            try:
                contact_ids = _association_object_ids(
                    resp.json(), reject_next_page=False)
            except _UnusableAssociationResponse as bad:
                raise HubSpotRetryableError(
                    f"contact lookup failed for deal {deal_id}: unusable "
                    f"contact-association response ({bad})") from bad
            if not contact_ids:
                return None
            contact = client.crm.contacts.basic_api.get_by_id(
                contact_id=contact_ids[0], properties=CONTACT_PROPERTIES,
                _request_timeout=HUBSPOT_REQUEST_TIMEOUT_SECONDS,
            )
            return contact.to_dict()
        except (requests.exceptions.RequestException,
                *HUBSPOT_SDK_READ_ERRORS) as exc:
            if attempt < MAX_RETRIES and not _is_not_found(exc):
                time.sleep(INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue
            raise HubSpotRetryableError(
                f"contact lookup failed for deal {deal_id} after {attempt} "
                f"attempt(s): {_describe_hubspot_failure(exc)}") from exc
    raise HubSpotRetryableError(
        f"contact lookup exhausted retries for deal {deal_id}")


def pull_contacts_by_ids(contact_ids: list) -> dict:
    """Batch-read HubSpot contacts by ID; return {contact_id: createdate | None}.

    PR-ADS-115 lead event-date reconciliation. Read-only — reads ONLY createdate.
    Not-found contacts are simply absent from the result (the caller treats them
    as excluded_legacy). Raises on API error so the caller can mark the affected
    rows ``unresolved`` (retryable) rather than excluding them.

    NEVER writes to HubSpot.
    """
    if not contact_ids:
        return {}
    client = get_client()
    out: dict = {}
    ids = [str(c) for c in contact_ids if c]
    for i in range(0, len(ids), 100):  # HubSpot batch read: max 100 ids/call
        batch = ids[i:i + 100]
        resp = client.crm.contacts.batch_api.read(
            batch_read_input_simple_public_object_id={
                "properties": ["createdate"],
                "inputs": [{"id": cid} for cid in batch],
            }
        )
        for obj in resp.results:
            d = obj.to_dict()
            out[str(d.get("id"))] = (d.get("properties", {}) or {}).get("createdate")
    return out


def pull_all_contacts_in_range(date_from: str, date_to: str) -> list:
    """Pull ALL HubSpot contacts created within a date range — every source.

    PR-ADS-117 source backfill: unlike pull_paid_search_contacts_in_range this is
    NOT filtered to PAID_SEARCH, so Organic / Offline / Other Paid contacts are
    classified too. Returns raw contact dicts (with Original Source + drill-down
    properties). Read-only — NEVER writes to HubSpot.

    Raises ``HubSpotPullIncompleteError`` (PR-ADS-163A) when a page cannot be
    read, instead of returning the pages before it.
    """
    client = get_client()
    from datetime import timezone, time as _time
    from datetime import date as _date

    def _to_ms(d, start):
        parsed = _date.fromisoformat(d)
        edge = _time.min if start else _time.max
        return int(datetime.combine(parsed, edge, tzinfo=timezone.utc).timestamp() * 1000)

    from_ms, to_ms = _to_ms(str(date_from), True), _to_ms(str(date_to), False)
    contacts, after = [], None
    while True:
        response = _legacy_search_page(
            "pull_all_contacts_in_range", len(contacts),
            lambda: client.crm.contacts.search_api.do_search(
                public_object_search_request={
                    "filterGroups": [{"filters": [
                        {"propertyName": "createdate", "operator": "GTE", "value": str(from_ms)},
                        {"propertyName": "createdate", "operator": "LTE", "value": str(to_ms)},
                    ]}],
                    "properties": CONTACT_PROPERTIES,
                    "limit": 100,
                    "after": after,
                },
                _request_timeout=HUBSPOT_REQUEST_TIMEOUT_SECONDS,
            ),
        )
        contacts.extend([c.to_dict() for c in response.results])
        if response.paging and response.paging.next:
            after = response.paging.next.after
        else:
            break
    logger.info("Pulled %d contacts (all sources, %s → %s)", len(contacts), date_from, date_to)
    return contacts


# ---------------------------------------------------------------------------
# PR-ADS-153B — canonical CRM funnel contact sync (all sources, watermarked)
# ---------------------------------------------------------------------------
def _epoch_ms(value) -> int:
    """Coerce a datetime / ISO string / epoch-ms value to epoch milliseconds."""
    from datetime import timezone as _tz

    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=_tz.utc)
        return int(dt.timestamp() * 1000)
    text = str(value).strip()
    if not text:
        return 0
    if text.isdigit():
        return int(text)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz.utc)
    return int(dt.timestamp() * 1000)


def parse_hubspot_timestamp(value):
    """Parse a HubSpot timestamp (ISO string or epoch ms) to an aware datetime.

    Returns None for null/blank/unparseable input. Absence stays absence — the
    caller must NEVER substitute another date for a missing stage-entry
    timestamp (PR-ADS-153B §3).
    """
    from datetime import timezone as _tz

    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=_tz.utc)
    # PR-ADS-161C review: an out-of-range epoch RAISED here, and inside a
    # history read that turned one contact's bad value into a failed request
    # for its whole 50-contact chunk. Unparseable is None, like any other.
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value) / 1000.0, tz=_tz.utc)
        except (ValueError, OverflowError, OSError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        try:
            return datetime.fromtimestamp(int(text) / 1000.0, tz=_tz.utc)
        except (ValueError, OverflowError, OSError):
            return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=_tz.utc)


# ---------------------------------------------------------------------------
# PR-ADS-155 §4 — recovering stage-entry dates from HubSpot property history
# ---------------------------------------------------------------------------
# The audit that produced this code path
# --------------------------------------
# Some contacts carry a lifecycle stage that proves they reached MQL/SQL/etc.
# while `hs_v2_date_entered_<stage>` is null, so the canonical funnel has a real
# transition with no date. Two recovery routes were checked against the live
# portal before any of this was written:
#
#   1. HubSpot's legacy per-stage date properties (`hs_lifecyclestage_lead_date`
#      and friends). A property search against the connected portal returns NO
#      such properties — the account exposes only the `hs_v2_date_entered_*` /
#      `hs_v2_date_exited_*` / `hs_v2_latest_time_in_*` family. This route does
#      not exist here.
#
#   2. Property HISTORY on `lifecyclestage`. HubSpot retains version history for
#      the property: each historical value with the timestamp it was set and the
#      source that set it, exposed through `propertiesWithHistory` on the CRM
#      read API (confirmed present on the installed SDK's batch-read model). A
#      version whose value IS a funnel stage is HubSpot's own record of the
#      transition into that stage — the same evidence `hs_v2_date_entered_*` is
#      derived from, read from the other end.
#
# So route 2 is real evidence and route 1 is not. What route 2 cannot promise is
# COMPLETENESS: history depth is bounded by HubSpot's retention, and a contact
# set straight to a later stage never had a version for the stages it skipped.
# Where no matching version exists, this returns nothing for that stage. The
# timestamp then stays NULL and the cohort keeps reporting it as a coverage gap.
#
# Read-only. This uses the CRM READ API only. There is no write path to HubSpot
# anywhere in this module's history support.
HUBSPOT_LIFECYCLE_HISTORY_PROPERTY = "lifecyclestage"

# PR-ADS-161C §4 — the direct SQL-entry property, read on the SAME request.
#
# The prospective gap detector re-reads `lifecyclestage` history for every
# undated post-boundary contact on every run, and until this PR it never
# re-read the direct property. A `hs_v2_date_entered_salesqualifiedlead` that
# HubSpot populated after our sync last selected the contact was therefore
# invisible to it forever: the history held no SQL version (a stage jump, say),
# the incident was recorded as "no SQL transition", and the one exact date that
# existed upstream was never fetched. It now rides on the history read as a
# CURRENT property — not history — at no extra request.
HUBSPOT_SQL_ENTRY_PROPERTY = "hs_v2_date_entered_salesqualifiedlead"
HISTORY_READ_CURRENT_PROPERTIES = [HUBSPOT_LIFECYCLE_HISTORY_PROPERTY,
                                   HUBSPOT_SQL_ENTRY_PROPERTY]

# HubSpot caps a batch read that requests property history well below the
# ordinary 100-record batch limit. 50 is the documented ceiling and is used as a
# hard bound rather than a default a caller can raise past it.
HUBSPOT_HISTORY_BATCH_LIMIT = 50

# ── PR-ADS-155-F1 — per-contact EVIDENCE STATES ─────────────────────────────
# The first cut returned `{contact_id: [versions]}` and defaulted every
# requested contact to `[]`. That made four different facts indistinguishable,
# and a production dry run over 50 contacts reported "no history version for
# stage" 36 times without anyone being able to say which of them HubSpot had
# actually answered. These are four different findings with four different
# follow-ups, so they are four different states:
#
#   HISTORY_CONTACT_ABSENT  HubSpot's response did not include this contact at
#                           all (deleted, merged, or not visible to this token).
#                           We asked and got no record — not "no history".
#   HISTORY_PROPERTY_ABSENT the contact came back, but carries no
#                           `propertiesWithHistory` for `lifecyclestage`. Either
#                           history was not returned, or the property has never
#                           been set on this contact.
#   HISTORY_EMPTY           the history key IS present and the list is empty.
#                           HubSpot affirmatively holds no versions.
#   HISTORY_PRESENT         versions were returned; whether any of them MATCHES a
#                           funnel stage is the service's question, not this one.
HISTORY_CONTACT_ABSENT = "history_contact_not_returned"
HISTORY_PROPERTY_ABSENT = "history_payload_missing"
HISTORY_EMPTY = "history_payload_empty"
HISTORY_PRESENT = "history_payload_present"

# ── PR-ADS-159 §1 — why the first production dry run recovered nothing ───────
# 50 contacts examined, 50 `history_payload_missing`, 0 usable histories. The
# request never asked for history.
#
# `client.crm.contacts.batch_api.read(...)` was handed a plain dict as its body.
# The SDK's `ApiClient.sanitize_for_serialization` documents its own behaviour:
# "If obj is dict, return the dict" — `attribute_map` is applied ONLY to model
# instances. So the body went out carrying the snake_case key
# `properties_with_history`, HubSpot's batch-read endpoint does not know that
# field, ignored it, and answered with contacts that carry no history container
# at all. Every contact then parsed as HISTORY_PROPERTY_ABSENT, which reads as
# "HubSpot holds no history" when the truth is "we never asked for any".
#
#     dict body  -> {'inputs', 'properties', 'properties_with_history'}
#     model body -> {'inputs', 'properties', 'propertiesWithHistory'}   ← correct
#
# The previous docstring's claim that "the request model serializes
# properties_with_history to propertiesWithHistory" was true OF THE MODEL and
# irrelevant to the code, which never built one. A parameter that exists on a
# model the call does not use is not a parameter that was sent.
#
# The fix is to construct the real model. `test_pr_ads_159_*` asserts the
# serialized body key so the dict form cannot come back unnoticed.
HISTORY_PARAMETER_UNSUPPORTED = "history_parameter_dropped_or_unsupported"

#: How the history for a contact was obtained, so a report can say which read
#: produced its evidence rather than leaving the reader to assume the batch.
HISTORY_VIA_BATCH = "batch_read"
HISTORY_VIA_INDIVIDUAL = "individual_read"


def _batch_history_body(ids):
    """The batch-read body as an SDK MODEL, so history is actually requested.

    Falls back to a hand-built camelCase dict if the model cannot be imported,
    because the wire contract — ``propertiesWithHistory`` — is what matters, not
    which object produced it. A snake_case dict is never sent either way.
    """
    payload_inputs = [{"id": cid} for cid in ids]
    prop = [HUBSPOT_LIFECYCLE_HISTORY_PROPERTY]
    # PR-ADS-161C: the CURRENT value of the direct SQL-entry property rides on
    # the same read. History is still requested for `lifecyclestage` only.
    current = HISTORY_READ_CURRENT_PROPERTIES
    try:
        from hubspot.crm.contacts import (  # noqa: PLC0415
            BatchReadInputSimplePublicObjectId,
        )
    except Exception:  # noqa: BLE001 — pragma: no cover
        return {"inputs": payload_inputs, "properties": current,
                "propertiesWithHistory": prop}
    return BatchReadInputSimplePublicObjectId(
        inputs=payload_inputs, properties=current, properties_with_history=prop)


def _history_from_record(record: dict) -> dict:
    """One contact's history payload → its evidence state and versions. Pure.

    ``to_dict()`` emits snake_case; a raw JSON dict emits camelCase. Both are
    accepted so parsing never depends on which form the caller passed.
    """
    history = record.get("properties_with_history")
    if history is None:
        history = record.get("propertiesWithHistory")
    direct = _direct_sql_entry(record)
    if not isinstance(history, dict) \
            or HUBSPOT_LIFECYCLE_HISTORY_PROPERTY not in history:
        return {"state": HISTORY_PROPERTY_ABSENT, "versions": [],
                "direct_sql_entry": direct}
    versions = history.get(HUBSPOT_LIFECYCLE_HISTORY_PROPERTY) or []
    if not versions:
        return {"state": HISTORY_EMPTY, "versions": [],
                "direct_sql_entry": direct}
    return {"state": HISTORY_PRESENT,
            "versions": [_normalise_history_version(v) for v in versions],
            "direct_sql_entry": direct}


def _direct_sql_entry(record: dict) -> dict:
    """The direct SQL-entry property as this read returned it. Pure.

    PR-ADS-161C §4. Returns ``{"state", "value", "raw_present"}``:

    * ``present``     — a timestamp, parsed;
    * ``absent``      — the property came back empty;
    * ``unparseable`` — a non-empty value we could not parse. OURS to fix, and
      never folded into "absent", which is HubSpot's answer;
    * ``not_read``    — the record carries no properties container at all, so
      this read says nothing about the property either way.
    """
    props = record.get("properties")
    if not isinstance(props, dict):
        return {"state": "not_read", "value": None, "raw_present": False}
    raw = props.get(HUBSPOT_SQL_ENTRY_PROPERTY)
    raw_present = raw is not None and str(raw).strip() != ""
    parsed = parse_hubspot_timestamp(raw) if raw_present else None
    if not raw_present:
        state = "absent"
    else:
        state = "present" if parsed is not None else "unparseable"
    return {"state": state, "value": parsed, "raw_present": raw_present}


def _as_record(result) -> dict:
    return result.to_dict() if hasattr(result, "to_dict") else dict(result)


def fetch_lifecycle_stage_history(contact_ids, *, client=None) -> dict:
    """Read `lifecyclestage` version history for up to 50 contacts. READ-ONLY.

    Returns ``{contact_id: {"state": <evidence state>, "versions": [...]}}``.
    Each version is ``{"value", "timestamp", "source_type", "source_id",
    "source_label", "updated_by_user_id"}`` with ``timestamp`` an aware
    datetime, ordered exactly as HubSpot returned them.

    The state is reported per contact and is never inferred from an empty list:
    "HubSpot did not return this contact", "it returned it with no history
    payload" and "it returned an empty history" are three different facts, and
    only the third is evidence that no transition was ever recorded.

    The body is built as ``BatchReadInputSimplePublicObjectId``, NOT as a plain
    dict. The SDK applies ``attribute_map`` only to model instances, so a dict
    body would send ``properties_with_history`` — a field HubSpot's batch-read
    endpoint does not know — and history would never be requested. See the
    PR-ADS-159 §1 note above; that was the live defect.

    Raises ``HubSpotRetryableError`` on a non-rate-limit API failure. A partial
    read is never returned as a complete one.
    """
    ids = [str(c).strip() for c in (contact_ids or []) if str(c or "").strip()]
    if not ids:
        return {}
    if len(ids) > HUBSPOT_HISTORY_BATCH_LIMIT:
        raise ValueError(
            f"at most {HUBSPOT_HISTORY_BATCH_LIMIT} contacts per history read; "
            f"got {len(ids)}")

    client = client or get_client()
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.crm.contacts.batch_api.read(
                batch_read_input_simple_public_object_id=_batch_history_body(ids))
            break
        except ApiException as exc:
            if exc.status == 429 and attempt < MAX_RETRIES:
                time.sleep(INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue
            raise HubSpotRetryableError(
                f"HubSpot contact history read failed (status={exc.status}): {exc}"
            ) from exc
    else:  # pragma: no cover — the loop always breaks or raises
        raise HubSpotRetryableError("HubSpot contact history read exhausted retries")

    # Default: HubSpot did not return this contact. Overwritten below for every
    # contact it did return, so the default can never be mistaken for evidence.
    out = {cid: {"state": HISTORY_CONTACT_ABSENT, "versions": [],
                 "via": HISTORY_VIA_BATCH} for cid in ids}
    for result in (getattr(response, "results", None) or []):
        record = _as_record(result)
        contact_id = str(record.get("id") or "").strip()
        if not contact_id:
            continue
        out[contact_id] = {**_history_from_record(record),
                           "via": HISTORY_VIA_BATCH}
    return out


def fetch_lifecycle_stage_history_single(contact_id, *, client=None) -> dict:
    """One contact's ``lifecyclestage`` history, via the individual read.

    READ-ONLY. ``GET /crm/v3/objects/contacts/{id}`` with
    ``propertiesWithHistory`` as a QUERY parameter — a path the SDK maps
    correctly on its own (``basic_api.get_by_id`` appends
    ``("propertiesWithHistory", ...)`` explicitly), unlike the batch body.

    This exists as a bounded fallback for the case where the batch endpoint
    returns no history container even when asked properly. It costs one request
    per contact, so it is never used for an unbounded scan: the recovery service
    applies it only to contacts a batch read has already failed to answer, and
    only within an explicit request budget.

    Returns the same ``{"state", "versions", "via"}`` shape as the batch read.
    A 404 is ``HISTORY_CONTACT_ABSENT`` — HubSpot answered, and its answer is
    that the contact is not there. Authentication and permission failures raise,
    because a token that cannot read contacts must stop the run, not be counted
    as fifty contacts without history.
    """
    cid = str(contact_id or "").strip()
    if not cid:
        return {"state": HISTORY_CONTACT_ABSENT, "versions": [],
                "via": HISTORY_VIA_INDIVIDUAL}

    client = client or get_client()
    prop = [HUBSPOT_LIFECYCLE_HISTORY_PROPERTY]
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            record = client.crm.contacts.basic_api.get_by_id(
                cid, properties=HISTORY_READ_CURRENT_PROPERTIES,
                properties_with_history=prop)
            break
        except ApiException as exc:
            if exc.status == 404:
                return {"state": HISTORY_CONTACT_ABSENT, "versions": [],
                        "via": HISTORY_VIA_INDIVIDUAL}
            if exc.status == 429 and attempt < MAX_RETRIES:
                time.sleep(INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue
            raise HubSpotRetryableError(
                f"HubSpot individual history read failed for contact "
                f"(status={exc.status})") from exc
    else:  # pragma: no cover — the loop always breaks, returns or raises
        raise HubSpotRetryableError(
            "HubSpot individual history read exhausted retries")

    return {**_history_from_record(_as_record(record)),
            "via": HISTORY_VIA_INDIVIDUAL}


#: PR-ADS-161C §4.3 — what the forensic comparison asks HubSpot for. Current
#: values plus the HISTORY of both the stage and the direct property, so the
#: audit can say not only whether HubSpot holds an SQL date but WHEN HubSpot
#: set it — which is what separates "we never re-read it" from "we read it and
#: dropped it". No name, email, company or other personal property is asked for
#: BY THIS READ (the contact sync reads names since PR-ADS-166A).
COMPARISON_CURRENT_PROPERTIES = [HUBSPOT_LIFECYCLE_HISTORY_PROPERTY,
                                 HUBSPOT_SQL_ENTRY_PROPERTY,
                                 "lastmodifieddate"]
COMPARISON_HISTORY_PROPERTIES = [HUBSPOT_LIFECYCLE_HISTORY_PROPERTY,
                                 HUBSPOT_SQL_ENTRY_PROPERTY]


def _comparison_body(ids):
    """The comparison batch body as an SDK model (see `_batch_history_body`)."""
    payload_inputs = [{"id": cid} for cid in ids]
    try:
        from hubspot.crm.contacts import (  # noqa: PLC0415
            BatchReadInputSimplePublicObjectId,
        )
    except Exception:  # noqa: BLE001 — pragma: no cover
        return {"inputs": payload_inputs,
                "properties": COMPARISON_CURRENT_PROPERTIES,
                "propertiesWithHistory": COMPARISON_HISTORY_PROPERTIES}
    return BatchReadInputSimplePublicObjectId(
        inputs=payload_inputs, properties=COMPARISON_CURRENT_PROPERTIES,
        properties_with_history=COMPARISON_HISTORY_PROPERTIES)


def _direct_property_set_at(record: dict):
    """When HubSpot last SET the direct SQL property to a non-empty value.

    Read from the property's own history. ``None`` when the history is not
    present or holds no dated non-empty version — unknown, never guessed.
    """
    history = record.get("properties_with_history")
    if history is None:
        history = record.get("propertiesWithHistory")
    if not isinstance(history, dict):
        return None
    stamps = []
    for version in history.get(HUBSPOT_SQL_ENTRY_PROPERTY) or []:
        v = _normalise_history_version(version)
        if v.get("value") not in (None, "") and v.get("timestamp") is not None:
            stamps.append(v["timestamp"])
    return max(stamps) if stamps else None


def compare_sql_entry_evidence(contact_ids, *, client=None) -> dict:
    """READ-ONLY forensic read of SQL-entry evidence for up to 50 contacts.

    PR-ADS-161C §4.3. Used only by the explicitly invoked
    ``--compare-hubspot`` audit mode — never by a scheduled path.

    Returns ``{contact_id: {...}}`` with, per contact::

        {"returned", "lifecycle_stage", "last_modified_at", "direct_sql_entry",
         "direct_sql_entry_set_at", "history_state", "versions"}

    A contact HubSpot did not return is ``returned: False`` — an identity
    finding (deleted, merged, invisible to this token), never "no evidence".
    Raises ``HubSpotRetryableError`` on a failed request: a failed read must be
    reported as a failed read by the caller, not as fifty empty answers.
    """
    ids = [str(c).strip() for c in (contact_ids or []) if str(c or "").strip()]
    if not ids:
        return {}
    if len(ids) > HUBSPOT_HISTORY_BATCH_LIMIT:
        raise ValueError(
            f"at most {HUBSPOT_HISTORY_BATCH_LIMIT} contacts per comparison "
            f"read; got {len(ids)}")

    client = client or get_client()
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.crm.contacts.batch_api.read(
                batch_read_input_simple_public_object_id=_comparison_body(ids))
            break
        except ApiException as exc:
            if exc.status == 429 and attempt < MAX_RETRIES:
                time.sleep(INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue
            raise HubSpotRetryableError(
                f"HubSpot SQL-evidence comparison read failed "
                f"(status={exc.status}): {exc}") from exc
    else:  # pragma: no cover — the loop always breaks or raises
        raise HubSpotRetryableError("HubSpot comparison read exhausted retries")

    out = {cid: {"returned": False, "lifecycle_stage": None,
                 "last_modified_at": None,
                 "direct_sql_entry": {"state": "not_read", "value": None,
                                      "raw_present": False},
                 "direct_sql_entry_set_at": None,
                 "history_state": HISTORY_CONTACT_ABSENT, "versions": []}
           for cid in ids}
    for result in (getattr(response, "results", None) or []):
        record = _as_record(result)
        contact_id = str(record.get("id") or "").strip()
        if not contact_id:
            continue
        props = record.get("properties") or {}
        history = _history_from_record(record)
        out[contact_id] = {
            "returned": True,
            "lifecycle_stage": props.get(HUBSPOT_LIFECYCLE_HISTORY_PROPERTY),
            "last_modified_at": parse_hubspot_timestamp(
                props.get("lastmodifieddate")),
            "direct_sql_entry": history.get("direct_sql_entry"),
            "direct_sql_entry_set_at": _direct_property_set_at(record),
            "history_state": history.get("state"),
            "versions": history.get("versions") or [],
        }
    return out


def diagnose_lifecycle_history_reads(contact_ids, *, client=None) -> dict:
    """Compare the batch read against the individual read. READ-ONLY, no PII.

    Answers the one question a payload-state count cannot: when the batch says
    "no history", is that HubSpot's answer or ours? Running both reads over the
    same bounded sample separates "this portal does not expose lifecyclestage
    history" from "our batch request is malformed", and those have opposite
    follow-ups.

    Records only structural facts — endpoint category, HTTP outcome, requested
    and returned counts, whether the history container exists, whether a
    ``lifecyclestage`` key exists, how many versions came back, and the parser's
    verdict. Never a token, an email, a name, a company, a full contact payload,
    or any unrelated property value. Contact ids are HubSpot's own opaque
    identifiers and are reported only as counts here.
    """
    ids = [str(c).strip() for c in (contact_ids or []) if str(c or "").strip()]
    sample = ids[:HUBSPOT_HISTORY_BATCH_LIMIT]
    out: dict = {
        "requested_contacts": len(sample),
        "history_property": HUBSPOT_LIFECYCLE_HISTORY_PROPERTY,
        "hubspot_writes_performed": False,
        "batch": {"endpoint": "POST /crm/v3/objects/contacts/batch/read",
                  "request_body_key": _batch_history_request_key()},
        "individual": {"endpoint": "GET /crm/v3/objects/contacts/{id}",
                       "request_parameter": "propertiesWithHistory (query)"},
    }
    if not sample:
        out["batch"]["outcome"] = "not_attempted"
        out["individual"]["outcome"] = "not_attempted"
        return out

    client = client or get_client()

    batch_failed = individual_failed = False
    try:
        batch = fetch_lifecycle_stage_history(sample, client=client)
        out["batch"].update(outcome="ok", returned_contacts=len(batch),
                            states=_state_counts(batch),
                            versions_seen=sum(len(v.get("versions") or [])
                                              for v in batch.values()))
    except Exception as exc:  # noqa: BLE001
        batch, batch_failed = {}, True
        out["batch"].update(outcome="request_failed",
                            error_type=type(exc).__name__)

    individual: dict = {}
    for cid in sample:
        try:
            individual[cid] = fetch_lifecycle_stage_history_single(
                cid, client=client)
        except Exception as exc:  # noqa: BLE001
            individual_failed = True
            out["individual"].update(outcome="request_failed",
                                     error_type=type(exc).__name__)
            break
    else:
        out["individual"].update(outcome="ok",
                                 returned_contacts=len(individual),
                                 states=_state_counts(individual),
                                 versions_seen=sum(
                                     len(v.get("versions") or [])
                                     for v in individual.values()))

    # PR-ADS-159-R4: BOTH outcomes are preserved even when they disagree and
    # even when neither produced usable history. Collapsing them was how a
    # failed request came to read as "the portal holds nothing".
    out["verdict"] = _history_read_verdict(
        batch, individual, batch_failed=batch_failed,
        individual_failed=individual_failed)
    out["paths_agree"] = (out["batch"].get("states")
                          == out["individual"].get("states"))

    # The structural finding, independent of what the portal answered: if the
    # body will not carry `propertiesWithHistory`, history is never requested
    # and no empirical verdict about the portal is admissible.
    if out["batch"]["request_body_key"] != "propertiesWithHistory":
        out["request_defect"] = HISTORY_PARAMETER_UNSUPPORTED
        out["verdict"] = HISTORY_PARAMETER_UNSUPPORTED
    else:
        out["request_defect"] = None
    return out


def _batch_history_request_key() -> str:
    """The JSON key the batch body will actually carry. Structural, not a guess.

    A body that serializes to ``properties_with_history`` is the PR-ADS-159 §1
    defect, and the diagnostic reports the real key rather than the intent.
    """
    body = _batch_history_body(["0"])
    if isinstance(body, dict):
        return next((k for k in body if "istory" in k), "absent")
    return next((json_key for attr, json_key in body.attribute_map.items()
                 if attr == "properties_with_history"), "absent")


def _state_counts(by_contact: dict) -> dict:
    counts: dict = {}
    for entry in by_contact.values():
        state = (entry or {}).get("state") or HISTORY_CONTACT_ABSENT
        counts[state] = counts.get(state, 0) + 1
    return dict(sorted(counts.items()))


#: PR-ADS-159-R4 — the DIAGNOSIS vocabulary. Denominator: one diagnosis run.
#: These are statements about the READS, which no single contact's payload can
#: make. `history_parameter_dropped_or_unsupported` lives here and only here:
#: declaring it as a per-contact evidence state is what made it unreachable.
VERDICT_BOTH = "both_paths_return_history"
VERDICT_INDIVIDUAL_ONLY = "individual_only_batch_returns_no_history"
VERDICT_BATCH_ONLY = "batch_only_individual_returns_no_history"
VERDICT_NEITHER = "neither_path_returns_history"
VERDICT_BATCH_FAILED = "batch_request_failed"
VERDICT_INDIVIDUAL_FAILED = "individual_request_failed"
VERDICT_BOTH_FAILED = "both_requests_failed"

DIAGNOSIS_VERDICTS = (
    VERDICT_BOTH,
    VERDICT_INDIVIDUAL_ONLY,
    VERDICT_BATCH_ONLY,
    VERDICT_NEITHER,
    VERDICT_BATCH_FAILED,
    VERDICT_INDIVIDUAL_FAILED,
    VERDICT_BOTH_FAILED,
    HISTORY_PARAMETER_UNSUPPORTED,
)


def _history_read_verdict(batch: dict, individual: dict, *,
                          batch_failed: bool = False,
                          individual_failed: bool = False) -> str:
    """Which read, if either, produced usable history over the sample.

    PR-ADS-159-R4: a FAILED request is not "this read returns no history". The
    first cut passed an empty dict for a failed batch, which fell through to
    `neither_path_returns_history` — the strongest possible claim about the
    portal, drawn from a request that never got an answer. That is the same
    class of error as the defect being diagnosed.
    """
    def _any_present(d):
        return any((e or {}).get("state") == HISTORY_PRESENT for e in d.values())

    if batch_failed and individual_failed:
        return VERDICT_BOTH_FAILED
    if batch_failed:
        return VERDICT_BATCH_FAILED
    if individual_failed:
        return VERDICT_INDIVIDUAL_FAILED

    batch_ok, single_ok = _any_present(batch), _any_present(individual)
    if batch_ok and single_ok:
        return VERDICT_BOTH
    if single_ok:
        # The portal HAS the history and the batch is not getting it. That is
        # not a retention finding — it is a statement about our request.
        return VERDICT_INDIVIDUAL_ONLY
    if batch_ok:
        return VERDICT_BATCH_ONLY
    return VERDICT_NEITHER


def _normalise_history_version(version) -> dict:
    """One HubSpot property-history version, normalised. Pure.

    The raw value is preserved verbatim — the stage is normalised by
    ``analysis.crm_lifecycle`` at the point of use, not guessed here — and the
    HubSpot source fields are carried through so a recovered date can always be
    traced to the change that produced it.
    """
    raw = version.to_dict() if hasattr(version, "to_dict") else dict(version or {})
    return {
        "value": raw.get("value"),
        "timestamp": parse_hubspot_timestamp(raw.get("timestamp")),
        # PR-ADS-159 §2: the raw field, kept so the caller can tell a version
        # that carried NO timestamp from one whose timestamp would not parse.
        # Both arrive as `timestamp: None`, and they are different defects: the
        # first is HubSpot recording a change without a time, the second is a
        # value we are failing to read.
        "timestamp_raw": raw.get("timestamp"),
        "source_type": raw.get("source_type") or raw.get("sourceType"),
        "source_id": raw.get("source_id") or raw.get("sourceId"),
        # HubSpot's human-readable account of the change (e.g. a workflow name).
        "source_label": raw.get("source_label") or raw.get("sourceLabel"),
        "updated_by_user_id": (raw.get("updated_by_user_id")
                               or raw.get("updatedByUserId")),
    }


def _completion_sentinel(watermark_ms, total_yielded, page_index, reanchored,
                         source_total=None, unique_read=None):
    """The explicit end-of-result-set marker. An empty page, so it can never
    write rows or move a watermark — it only proves the scan finished."""
    return [], {
        "watermark_ms": watermark_ms,
        "watermark_iso": datetime.utcfromtimestamp(
            watermark_ms / 1000.0).isoformat() + "Z",
        "total_yielded": total_yielded,
        "page_index": page_index,
        "reanchored": reanchored,
        "source_total": source_total,
        "unique_read": unique_read,
        "complete": True,
    }


SCAN_SHORTFALL_TOLERANCE = 0.01
SCAN_SHORTFALL_MIN_SLACK = 2


def _assert_scan_complete(source_total, unique_ids, *, noun: str = "contact",
                          tolerant: bool = True) -> None:
    """PR-ADS-161I: a scan may end only when it read (about) every record
    HubSpot counted when it began.

    With keyset paging the only legitimate shortfall is a record merged or
    deleted mid-scan BEFORE it was read (or, with an extra filter, one that
    left the filter); it is gone from the result set too. Records modified
    mid-scan are re-read (de-duplicated by id) and do not lower the count.

    ``tolerant`` (the contact scan): 1% or 2 records, whichever is larger;
    zero records read against a non-empty result set never passes.
    ``tolerant=False`` (the deal ledger, PR-ADS-166B): every counted record
    must have been read. A deal deleted mid-scan then costs one ``partial``
    run, which the next run repairs; a tolerance would let a missed won deal
    pass as complete.
    """
    if source_total is None:
        raise HubSpotSearchIncompleteError(
            "the scan never read HubSpot's total; completeness is unproven")
    seen = len({i for i in unique_ids if i})
    slack = (max(SCAN_SHORTFALL_MIN_SLACK,
                 math.ceil(source_total * SCAN_SHORTFALL_TOLERANCE))
             if tolerant else 0)
    if (source_total > 0 and seen == 0) or seen < source_total - slack:
        raise HubSpotSearchIncompleteError(
            f"{noun} scan ended after {seen} unique {noun}s but HubSpot "
            f"counted {source_total} at its start; the scan is INCOMPLETE "
            "and must not be reported as complete")


def _object_id_number(cid: str):
    """HubSpot object ids are numeric strings; the id keyset needs the number.

    Digits are extracted so a test fake's ``c123`` keys like ``123``; a
    production id is all digits, so this is the id itself. None when there is
    no digit at all — the keyset cannot walk it, and the caller stalls.
    """
    digits = "".join(ch for ch in str(cid or "") if ch.isdigit())
    return int(digits) if digits else None


_contact_id_number = _object_id_number


class HubSpotSearchRequestError(Exception):
    """One search request failed and its retry budget, if any, is spent.

    PR-ADS-166B. ``transient`` is True for a 429, a 5xx or a transport error
    (retried ``MAX_RETRIES`` times before this is raised); False for any other
    API error — a refusal, raised at once. ``status`` is the HTTP status, or
    None for a transport error. ``detail`` names the status and the attempts.
    """

    def __init__(self, what: str, status, detail: str, transient: bool):
        self.what = what
        self.status = status
        self.detail = detail
        self.transient = transient
        super().__init__(f"HubSpot {what} search failed: {detail}")


def _bounded_search(call, what: str):
    """Run one SDK search request with a bounded retry. ``call()`` performs it.

    Catches ``HUBSPOT_SDK_READ_ERRORS`` — every package's ``ApiException`` and
    the urllib3 transport errors the SDK lets through — never one package's
    class. A 429, a 5xx or a transport error is retried ``MAX_RETRIES`` times
    (``MAX_RETRIES + 1`` attempts, 2 / 4 / 8 s); anything else is raised at
    once. Both raise ``HubSpotSearchRequestError``.
    """
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            return call()
        except HUBSPOT_SDK_READ_ERRORS as exc:
            status = getattr(exc, "status", None)
            transient = (status is None or status == 429
                         or (isinstance(status, int) and status >= 500))
            if transient and attempt <= MAX_RETRIES:
                wait = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning("HubSpot %s search: %s — retry %d/%d in %ds",
                               what, _describe_hubspot_failure(exc), attempt,
                               MAX_RETRIES, wait)
                time.sleep(wait)
                continue
            detail = _describe_hubspot_failure(exc)
            if transient:
                detail = f"{detail} after {attempt} attempts"
            raise HubSpotSearchRequestError(what, status, detail,
                                            transient) from exc
    raise HubSpotSearchRequestError(        # unreachable: returns or raises
        what, None, "retry budget exhausted", True)


def _keyset_scan(search, *, modified_prop: str, since_ms: int,
                 base_filters=(), page_size: int = _SEARCH_PAGE_SIZE,
                 max_pages: int | None = None, noun: str = "record",
                 tolerant: bool = True):
    """Keyset-page one HubSpot search over records modified at/after a time.

    PR-ADS-166B: the loop PR-ADS-161I built for contacts, shared so the deal
    ledger reads the same way. ``search(filters, sort_property)`` performs one
    request at offset 0 and returns the SDK response; ``base_filters`` (e.g. a
    stage ``IN``) are added to every query.

    * Every page is a NEW query ``<modified_prop> >= watermark``, never an
      ``after`` offset, so HubSpot's 10,000-result paging ceiling never
      applies and a record already read that is modified, merged or deleted
      mid-scan cannot shift an unread one out of the next page: every unread
      record still matches ``>= watermark``.
    * Rows re-returned at the watermark timestamp are de-duplicated by id. A
      record modified mid-scan to a LATER time is read again later, as its
      newer version.
    * A full page that never leaves its starting timestamp is a tie group,
      walked by id: ``<modified_prop> EQ T AND hs_object_id GT last`` sorted by
      id, then the scan resumes with ``GT T``. HubSpot's order among tied rows
      is arbitrary between queries, so they are never paged by offset.
    * A response with no ``results`` list is an error, never an empty page.
    * The scan ends on a short range page, and only after
      ``_assert_scan_complete`` against the FIRST query's ``total``.
    * PR-ADS-175A: HubSpot filters and sorts on its search INDEX but returns
      each record's CURRENT properties. A record modified shortly before (or
      during) the scan is returned at its OLD sort position carrying its NEW,
      later timestamp. So no single row may move the watermark, and no row
      is discarded because its timestamp is below a watermark another row
      set: every returned row is read unless this scan already read that id
      at that timestamp. After a range page the next watermark is the OLDER
      of the page's last two parseable timestamps — one out-of-place row
      cannot carry it past the unread rows behind it. Two at the very end of
      one page can; ``_assert_scan_complete`` refuses that only when the
      shortfall exceeds its slack (``tolerant``: 1% or 2 records) and is not
      masked by records that entered the result set mid-scan, which count
      toward the unique total too. A tie walk never moves the watermark.
      ``meta["watermark_ms"]`` is that position, the only value a caller may
      checkpoint.

    Yields ``(fresh_records, meta)``; the last yield is the completion
    sentinel ``([], {"complete": True, …})``. A ``max_pages`` cap ends with no
    sentinel. Raises ``HubSpotSearchStalledError``,
    ``HubSpotSearchIncompleteError``, or whatever ``search`` raises.
    """
    watermark_ms = int(since_ms or 0)
    total_yielded = 0
    page_index = 0
    reanchored = 0          # queries after the first
    # PR-ADS-175A: id -> the timestamp this scan last read it at. A row is
    # re-read only when it comes back carrying a different timestamp (a newer
    # version). Replaces a set keyed to the current watermark, which treated
    # every row below the watermark as already read.
    read_at: dict[str, int] = {}
    # HubSpot's ``total`` for the scan's FIRST query: the bar the scan must meet.
    source_total = None
    unique_ids: set[str] = set()
    # True right after a whole tie group at ``watermark_ms`` has been walked.
    strictly_after = False
    tie_last_id = None      # not None while walking a tie group by id
    base_filters = list(base_filters or [])

    while True:
        if max_pages is not None and page_index >= max_pages:
            # Capped run: deliberately NO completion sentinel.
            return

        query_from_ms = watermark_ms
        query_was_strict = strictly_after and tie_last_id is None
        if tie_last_id is not None:
            response = search(base_filters + [
                {"propertyName": modified_prop, "operator": "EQ",
                 "value": str(watermark_ms)},
                {"propertyName": "hs_object_id", "operator": "GT",
                 "value": str(tie_last_id)},
            ], "hs_object_id")
        else:
            response = search(base_filters + [
                {"propertyName": modified_prop,
                 "operator": "GT" if strictly_after else "GTE",
                 "value": str(watermark_ms)},
            ], modified_prop)

        if source_total is None:
            total = getattr(response, "total", None)
            source_total = (int(total) if isinstance(total, int)
                            and not isinstance(total, bool) else None)
            if source_total is None:
                raise HubSpotSearchIncompleteError(
                    f"HubSpot {noun} search response carried no `total`; the "
                    "scan cannot prove completeness")
        elif page_index:
            reanchored += 1

        raw_results = getattr(response, "results", None)
        if not isinstance(raw_results, list):
            raise HubSpotSearchPageUnreadableError(
                f"HubSpot {noun} search response carries no `results` list "
                f"(page {page_index + 1}); an unreadable page is not an empty "
                "one")
        results = [r.to_dict() if hasattr(r, "to_dict") else dict(r)
                   for r in raw_results]
        fresh = []
        page_ids = []
        stamps = []             # parseable timestamps, in result order
        for record in results:
            rid = str(record.get("id") or "").strip()
            page_ids.append(_object_id_number(rid))
            modified = _epoch_ms(
                (record.get("properties") or {}).get(modified_prop))
            if modified > 0:
                stamps.append(modified)
            if not rid:
                # No identity to de-duplicate on: read once, when first past
                # the query's bound (it is rejected downstream regardless).
                if modified > query_from_ms:
                    fresh.append(record)
                continue
            # Unparseable timestamps (<= 0) are still read once: the record
            # is in HubSpot's total. They never move the watermark.
            stamp = modified if modified > 0 else 0
            if rid in read_at and (read_at[rid] == stamp or stamp == 0):
                continue
            read_at[rid] = stamp
            fresh.append(record)

        if tie_last_id is None and stamps:
            # PR-ADS-175A: the next query starts from the OLDER of the page's
            # last two timestamps, never from the newest row. A row returned
            # at a stale index position carries a later timestamp than its
            # place in the sort; trusting it skipped every row between
            # (production 2026-11-23: 152 contacts). Never moves backwards.
            watermark_ms = max(watermark_ms, min(stamps[-2:]))

        page_index += 1
        if fresh:
            total_yielded += len(fresh)
            unique_ids.update(str(r.get("id") or "") for r in fresh)
            yield fresh, {
                "watermark_ms": watermark_ms,
                "watermark_iso": datetime.utcfromtimestamp(
                    watermark_ms / 1000.0).isoformat() + "Z",
                "total_yielded": total_yielded,
                "page_index": page_index,
                "reanchored": reanchored,
                "complete": False,
            }

        full = len(results) >= page_size
        if tie_last_id is not None:
            if tie_last_id == -1 and not results:
                # Positive control: a full page at this timestamp was just
                # read, so the walk's first page cannot be empty.
                raise HubSpotSearchStalledError(
                    f"HubSpot returned no {noun}s for {modified_prop} EQ "
                    f"{watermark_ms} right after a full page at it; the tie "
                    "walk cannot be trusted. The scan is INCOMPLETE.")
            if not full:
                tie_last_id = None
                strictly_after = True
                continue
            if None in page_ids or max(page_ids) <= tie_last_id:
                raise HubSpotSearchStalledError(
                    f"HubSpot {noun} scan stalled walking the {noun}s that "
                    f"share {modified_prop}={watermark_ms}: the id keyset "
                    "did not advance. The scan is INCOMPLETE and must not be "
                    "reported as complete.")
            tie_last_id = max(page_ids)
            continue

        if not full:
            # A short page from a range query is the end of the result set.
            _assert_scan_complete(source_total, unique_ids, noun=noun,
                                  tolerant=tolerant)
            yield _completion_sentinel(
                watermark_ms, total_yielded, page_index, reanchored,
                source_total, len(unique_ids))
            return

        if query_was_strict and not fresh and watermark_ms == query_from_ms:
            raise HubSpotSearchStalledError(
                f"HubSpot {noun} scan made no progress: a full page after "
                f"{modified_prop}={watermark_ms} moved neither the watermark "
                f"nor read a new {noun}. The scan is INCOMPLETE.")
        strictly_after = False
        if watermark_ms == query_from_ms:
            # The page ended at its starting timestamp: walk that timestamp
            # by id, then resume after it.
            tie_last_id = -1


def iter_contacts_modified_since(
    since,
    *,
    page_size: int = _SEARCH_PAGE_SIZE,
    max_pages: int | None = None,
    client=None,
):
    """Yield pages of ALL-SOURCE HubSpot contacts modified at/after ``since``.

    PR-ADS-153B §7. This is the canonical contact ingestion read. Unlike
    ``pull_paid_search_contacts*`` it applies NO source filter, and unlike
    ``pull_all_contacts_in_range`` it windows on ``lastmodifieddate`` rather than
    ``createdate`` — so a contact created two years ago whose lifecycle changed
    today IS returned today.

    Results are sorted ASCENDING by ``lastmodifieddate`` so the caller can
    durably checkpoint after every page: the last row's modification timestamp is
    a safe resume watermark.

    KEYSET PAGING (PR-ADS-161I). Every page is a NEW query ``lastmodifieddate >=
    watermark`` from offset 0; rows already read at the watermark timestamp are
    de-duplicated. An offset over HubSpot's live result set shifts whenever a
    contact already read is modified, merged or deleted mid-scan — the row moves
    out of its slot and the next offset silently skips an unread contact. A
    keyset query cannot skip: every unread contact still has ``lastmodifieddate
    >= watermark``. A group of contacts sharing ONE timestamp that fills a page
    is walked by keyset on the contact id (``lastmodifieddate EQ T AND
    hs_object_id GT last``, sorted by id; verified against live HubSpot), never
    by offset: HubSpot's order among tied rows is arbitrary between queries and
    a tied member modified mid-scan would shift an offset. After the group the
    scan resumes with ``lastmodifieddate GT T``. No offset is ever sent.

    Yields ``(contacts, page_meta)`` where ``page_meta`` carries
    ``{watermark_ms, watermark_iso, total_yielded, page_index, reanchored,
    complete}``.

    COMPLETION PROOF (PR-ADS-153B truth-safety §4): reaching the true end of the
    result set is signalled EXPLICITLY by a final sentinel page
    ``([], {"complete": True, ...})``. A caller may only treat a historical
    bootstrap as complete when it observed that sentinel. Simply running out of
    pages (e.g. because ``max_pages`` capped the run) proves nothing and yields
    no sentinel.

    Raises ``HubSpotRetryableError`` when a search request fails: a 429, a
    5xx or a transport error after ``MAX_RETRIES`` retries, any other API
    error at once (PR-ADS-166B: this used to retry a 429 without limit and
    caught only the contacts ``ApiException``, so a timeout escaped raw).
    ``HubSpotSearchStalledError`` when a query makes no progress (HubSpot did
    not honour the keyset filter, or a contact id is not numeric), and
    ``HubSpotSearchIncompleteError`` when the scan ends short of HubSpot's own
    ``total`` — a partial read is NEVER reported as a complete one — and
    ``HubSpotSearchPageUnreadableError`` when a page carries no ``results``
    list (not a proven gap: the pages before it keep their checkpoints). The loop itself is ``_keyset_scan``, shared
    with the deal ledger since PR-ADS-166B.
    NEVER writes to HubSpot.
    """
    client = client or get_client()

    def _search(filters, sort_prop):
        # No `_request_timeout` here: unchanged by PR-ADS-166B (see docs/09).
        return _bounded_search(
            lambda: client.crm.contacts.search_api.do_search(
                public_object_search_request={
                    "filterGroups": [{"filters": filters}],
                    "properties": CONTACT_FUNNEL_PROPERTIES,
                    "sorts": [{"propertyName": sort_prop,
                               "direction": "ASCENDING"}],
                    "limit": page_size,
                    "after": None,
                }
            ),
            "contact")

    try:
        yield from _keyset_scan(
            _search, modified_prop="lastmodifieddate",
            since_ms=_epoch_ms(since), page_size=page_size,
            max_pages=max_pages, noun="contact", tolerant=True)
    except HubSpotSearchRequestError as exc:
        # `status=` stays in the text: the lifecycle recovery reads it to stop
        # on 401/403 (`_is_permanent_auth_failure`).
        raise HubSpotRetryableError(
            f"HubSpot contact search failed (status={exc.status}): "
            f"{exc.detail}") from exc


def normalize_contact_funnel_row(contact: dict) -> dict | None:
    """Normalise one raw HubSpot contact into a canonical funnel row.

    PR-ADS-153B §4. Pure — no I/O, so it is directly unit-testable.

    Doctrine enforced here:
      * ``contact_id`` is the durable HubSpot identity; a contact without one is
        rejected (returns None) rather than given a synthetic key.
      * Missing stage-entry timestamps stay NULL. ``createdate`` is NEVER
        substituted for a missing funnel event date.
      * ``mql_status`` carries ONLY the HubSpot property — there is no MDR
        free-text fallback.
      * Unknown lifecycle stages are preserved verbatim, never guessed.
      * No email address is read or returned.
      * ``first_name`` / ``last_name`` are the HubSpot ``firstname`` /
        ``lastname`` properties verbatim (PR-ADS-166A). A blank one is None —
        nothing else (company, email, id) is ever put in its place.
    """
    from analysis.crm_lifecycle import (  # noqa: PLC0415
        LIFECYCLE_RULE_VERSION, normalize_lifecycle_stage,
    )
    from analysis.mql_status_taxonomy import (  # noqa: PLC0415
        MQL_STATUS_RULE_VERSION, classify_mql_status, normalize_mql_status,
    )

    if not contact:
        return None
    contact_id = str(contact.get("id") or "").strip()
    if not contact_id:
        return None

    props = contact.get("properties") or {}

    def _prop(name):
        value = props.get(name)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    gclid = _prop("hs_google_click_id")
    mql_status = normalize_mql_status(props.get("mql_status"))

    entered_lead = parse_hubspot_timestamp(props.get("hs_v2_date_entered_lead"))
    entered_mql = parse_hubspot_timestamp(
        props.get("hs_v2_date_entered_marketingqualifiedlead"))
    entered_sql = parse_hubspot_timestamp(
        props.get("hs_v2_date_entered_salesqualifiedlead"))
    entered_opp = parse_hubspot_timestamp(props.get("hs_v2_date_entered_opportunity"))
    entered_customer = parse_hubspot_timestamp(props.get("hs_v2_date_entered_customer"))
    stage_dates = [d for d in (entered_lead, entered_mql, entered_sql,
                               entered_opp, entered_customer) if d is not None]

    return {
        "contact_id": contact_id,
        "created_at": parse_hubspot_timestamp(props.get("createdate")),
        "last_modified_at": parse_hubspot_timestamp(props.get("lastmodifieddate")),

        "lifecycle_stage": normalize_lifecycle_stage(props.get("lifecyclestage")),
        "lead_status": _prop("hs_lead_status"),
        "mql_status": mql_status,
        "mql_status_category": classify_mql_status(mql_status),

        # Canonical stage-entry evidence — absence stays absence.
        "date_entered_lead": entered_lead,
        "date_entered_mql": entered_mql,
        "date_entered_sql": entered_sql,
        "date_entered_opportunity": entered_opp,
        "date_entered_customer": entered_customer,
        "latest_stage_entry_at": max(stage_dates) if stage_dates else None,

        "hs_analytics_source": _prop("hs_analytics_source"),
        "hs_analytics_source_data_1": _prop("hs_analytics_source_data_1"),
        "hs_analytics_source_data_2": _prop("hs_analytics_source_data_2"),
        "hs_latest_source": _prop("hs_latest_source"),
        "hs_latest_source_data_1": _prop("hs_latest_source_data_1"),
        "hs_latest_source_data_2": _prop("hs_latest_source_data_2"),
        "hs_analytics_first_url": _prop("hs_analytics_first_url"),

        "ip_country": _prop("ip_country"),
        "country": _prop("country"),
        "company": _prop("company"),
        "owner_id": _prop("hubspot_owner_id"),
        "first_name": _prop("firstname"),
        "last_name": _prop("lastname"),

        "gclid": gclid,
        "has_gclid": bool(gclid),

        "source_system": "hubspot_api",
        "lifecycle_rule_version": LIFECYCLE_RULE_VERSION,
        "mql_rule_version": MQL_STATUS_RULE_VERSION,
    }


# ---------------------------------------------------------------------------
# PR-ADS-166A — names by id (read-only batch reads, 100 objects per call)
# ---------------------------------------------------------------------------
#: HubSpot's batch read accepts at most 100 inputs when no history is asked.
NAME_BATCH_LIMIT = 100
#: Pause between name batch reads, so ~30 calls do not burst the rate limit.
NAME_READ_PACING_SECONDS = 0.2
CONTACT_NAME_PROPERTIES = ["firstname", "lastname"]
COMPANY_NAME_PROPERTIES = ["name"]


def _name_batch_body(module: str, ids, properties):
    inputs = [{"id": i} for i in ids]
    try:
        import importlib  # noqa: PLC0415
        model = importlib.import_module(module).BatchReadInputSimplePublicObjectId
    except Exception:  # noqa: BLE001 — pragma: no cover
        return {"inputs": inputs, "properties": list(properties)}
    return model(inputs=inputs, properties=list(properties))


class HubSpotNameReadRefused(HubSpotRetryableError):
    """A name read HubSpot REFUSED (a 4xx other than 429): a missing scope, a
    bad token, a malformed request. It will not change on retry, and the next
    batch would be refused the same way. ``status`` is the HTTP status."""

    def __init__(self, message: str, status):
        super().__init__(message)
        self.status = status


def _not_found_ids(payload, asked: set) -> set:
    """Ids a batch-read answer NAMES as not found, restricted to those asked.

    HubSpot reports objects it cannot return in ``errors`` (category
    ``OBJECT_NOT_FOUND``, ``context.ids``) as the body of a 404 when it
    returned none of them.
    """
    if not isinstance(payload, dict):
        return set()
    blocks = list(payload.get("errors") or [])
    if payload.get("category"):
        blocks.append(payload)
    found: set = set()
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if str(block.get("category") or "").upper() != "OBJECT_NOT_FOUND":
            continue
        ids = (block.get("context") or {}).get("ids") or []
        found.update(str(i).strip() for i in ids)
    return found & asked


def _name_backoff(attempt: int) -> None:
    time.sleep(INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)))


def _read_names_batch(batch_api, body, ids, what: str) -> dict:
    """One batch read. ``{id: properties}`` for the ASKED ids HubSpot returned.

    An id HubSpot did not return (deleted, merged, invisible) is absent from
    the result — it is not an object with no name. That includes an answer of
    404 that NAMES requested ids as not found: the batch was answered, and
    those ids were not returned. (A 207 is not an error to the SDK: it is
    RETURNED, as a result list beside its errors, and handled like any other
    answer — ids it does not list were not returned.)

    ``except HUBSPOT_API_EXCEPTIONS``, not ``except ApiException``: the SDK
    has one exception class PER package with no common base, and this
    function reads contacts AND companies. Classifying by ``status`` only
    works if the exception is caught at all.

    Retried with bounded backoff, as the association read is: 429, any 5xx, a
    timeout or a transport error. Not retried: any other 4xx, which raises
    ``HubSpotNameReadRefused`` with the status. Every failure raises
    ``HubSpotRetryableError`` — a failed read is never an empty answer.
    """
    import json as _json  # noqa: PLC0415

    asked = set(ids)
    last = "unknown failure"
    response = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = batch_api.read(
                batch_read_input_simple_public_object_id=body)
            break
        except HUBSPOT_API_EXCEPTIONS as exc:
            status = getattr(exc, "status", None)
            last = _describe_hubspot_failure(exc)
            if status == 404:
                try:
                    payload = _json.loads(getattr(exc, "body", None) or "null")
                except (TypeError, ValueError):
                    payload = None
                if _not_found_ids(payload, asked):
                    # Answered: these ids do not exist for us. Whatever the
                    # body did return is kept.
                    response = type("Answered", (), {
                        "results": list((payload or {}).get("results") or [])})()
                    break
            if status == 429 or (isinstance(status, int) and status >= 500):
                if attempt < MAX_RETRIES:
                    _name_backoff(attempt)
                    continue
                break
            raise HubSpotNameReadRefused(
                f"HubSpot {what} name read refused ({last})", status) from exc
        except Exception as exc:  # noqa: BLE001 — timeout, transport
            last = _describe_hubspot_failure(exc)
            if attempt < MAX_RETRIES:
                _name_backoff(attempt)
                continue
            break
    if response is None:
        raise HubSpotRetryableError(
            f"HubSpot {what} name read failed after {MAX_RETRIES} attempts "
            f"({last})")
    results = getattr(response, "results", None)
    if not isinstance(results, (list, tuple)):
        raise HubSpotRetryableError(
            f"HubSpot {what} name read returned no result list")
    out: dict = {}
    for result in results:
        record = _as_record(result)
        object_id = str(record.get("id") or "").strip()
        if object_id in asked:
            out[object_id] = record.get("properties") or {}
    return out


def _clean_name(value):
    text = " ".join(str(value).split()) if value is not None else ""
    return text or None


def _checked_ids(ids, what: str) -> list:
    ids = list(dict.fromkeys(str(i).strip() for i in ids or []
                             if str(i or "").strip()))
    if len(ids) > NAME_BATCH_LIMIT:
        raise ValueError(f"at most {NAME_BATCH_LIMIT} {what} per name read; "
                         f"got {len(ids)}")
    return ids


def read_contact_names(contact_ids, *, client=None) -> dict:
    """READ-ONLY: ``{contact_id: {"first_name", "last_name"}}`` for the asked
    contacts HubSpot returned (a blank name is None). At most 100 ids."""
    ids = _checked_ids(contact_ids, "contacts")
    if not ids:
        return {}
    client = client or get_client()
    found = _read_names_batch(
        client.crm.contacts.batch_api,
        _name_batch_body("hubspot.crm.contacts", ids, CONTACT_NAME_PROPERTIES),
        ids, "contact")
    return {cid: {"first_name": _clean_name(props.get("firstname")),
                  "last_name": _clean_name(props.get("lastname"))}
            for cid, props in found.items()}


def read_company_names(company_ids, *, client=None) -> dict:
    """READ-ONLY: ``{company_id: name-or-None}`` for the asked companies
    HubSpot returned. At most 100 ids."""
    ids = _checked_ids(company_ids, "companies")
    if not ids:
        return {}
    client = client or get_client()
    found = _read_names_batch(
        client.crm.companies.batch_api,
        _name_batch_body("hubspot.crm.companies", ids, COMPANY_NAME_PROPERTIES),
        ids, "company")
    return {cid: _clean_name(props.get("name")) for cid, props in found.items()}


# ---------------------------------------------------------------------------
# PR-ADS-163C — which stored contacts still exist in HubSpot
# ---------------------------------------------------------------------------
# The modified-since scan above can only see a contact that CHANGED. A contact
# that was deleted, or merged into another, does not change: it stops being
# returned, and nothing tells the sync so. This is the explicit question the
# sync never asks — "do these ids still exist?" — and nothing else. Read-only:
# the CRM search endpoint (one filter, one property) for every stored id, and
# a direct read by id of the few contacts a run is about to remove.

#: Ids asked for in one search. Equal to the page limit, so one batch is always
#: one page and there is nothing to page through.
CONTACT_EXISTENCE_BATCH_SIZE = 100

#: Seconds slept between two searches. HubSpot's search endpoint allows about
#: five requests a second per token; this keeps the check under four.
CONTACT_EXISTENCE_PACING_SECONDS = 0.25

#: After this many batches IN A ROW with no usable answer the check stops
#: calling HubSpot and marks every remaining batch unknown. A dead token or a
#: sustained rate limit would otherwise be retried for every one of ~220
#: batches (a 429 alone is 14 seconds of backoff per batch).
CONTACT_EXISTENCE_MAX_CONSECUTIVE_UNKNOWN = 3


def _canonical_object_id(value) -> str | None:
    """A HubSpot object id in its canonical decimal text, or None when the
    value is not one. ``"0042"`` and ``42`` are both ``"42"``."""
    text = str(value if value is not None else "").strip()
    if not (text and text.isascii() and text.isdigit()):
        return None
    return str(int(text))


def _existence_batch(client, batch: list, records_read: int) -> set:
    """The ids of ``batch`` that HubSpot returned, or an exception.

    The answer is used only when it can be read as the WHOLE answer: a
    ``results`` list (``_legacy_search_page`` raises without one, and bounds
    the 429 retries), HubSpot's own ``total`` equal to the number of results
    and no larger than the batch, no further page, and every result an id that
    was asked for, once. Anything else raises ``HubSpotPullIncompleteError``:
    the caller marks the batch UNKNOWN. An unreadable answer is never an
    answer of "none of these exist".
    """
    pull = "check_contacts_exist"

    def _unusable(detail: str):
        logger.error("%s: unusable answer for a batch of %d id(s): %s",
                     pull, len(batch), detail)
        return HubSpotPullIncompleteError(pull, records_read, None, detail)

    page = _legacy_search_page(
        pull, records_read,
        lambda: client.crm.contacts.search_api.do_search(
            public_object_search_request={
                "filterGroups": [{"filters": [
                    {"propertyName": "hs_object_id", "operator": "IN",
                     "values": list(batch)},
                ]}],
                "properties": ["hs_object_id"],
                "limit": CONTACT_EXISTENCE_BATCH_SIZE,
            },
            _request_timeout=HUBSPOT_REQUEST_TIMEOUT_SECONDS,
        ),
    )
    results = page.results
    total = getattr(page, "total", None)
    if not isinstance(total, int) or isinstance(total, bool):
        raise _unusable("the search response carries no `total`")
    if total != len(results):
        raise _unusable(
            f"HubSpot counted {total} match(es) but returned {len(results)}")
    if total > len(batch):
        raise _unusable(
            f"HubSpot counted {total} match(es) for {len(batch)} id(s) asked")
    paging = getattr(page, "paging", None)
    if paging is not None and getattr(paging, "next", None) is not None:
        raise _unusable("the response announces another page")

    # Ids are compared as the NUMBERS they are. HubSpot returns an object id
    # in its canonical decimal form; comparing text would call a contact
    # "not asked for" whenever the two spellings of one number differ, and
    # one such id would make its whole batch unknown on every run.
    asked = {_canonical_object_id(cid) for cid in batch}
    returned: set = set()
    for result in results:
        record = result.to_dict() if hasattr(result, "to_dict") else result
        rid = _canonical_object_id(record.get("id")) \
            if isinstance(record, dict) else None
        if rid is None:
            raise _unusable("a result carries no contact id")
        if rid not in asked:
            # HubSpot answered a different question than the one asked. What
            # that says about the ids that WERE asked is unknown.
            raise _unusable("a result is a contact that was not asked for")
        if rid in returned:
            raise _unusable("the same contact was returned twice")
        returned.add(rid)
    return returned


def check_contacts_exist(contact_ids, *, client=None, sleep=time.sleep) -> dict:
    """Ask HubSpot which of ``contact_ids`` still exist. Read-only.

    PR-ADS-163C. One CRM search per batch of at most
    ``CONTACT_EXISTENCE_BATCH_SIZE`` ids (``hs_object_id IN <batch>``,
    requesting ``hs_object_id`` only), paced by
    ``CONTACT_EXISTENCE_PACING_SECONDS``.

    Every id ends in exactly one of three lists::

        existing   HubSpot returned it
        missing    it was in a batch HubSpot answered, and was not returned
        unknown    its batch had no usable answer, was never sent, or the id
                   is not stored as a canonical HubSpot object id (not all
                   digits, or digits with leading zeros) and cannot be asked
                   about under the text it is stored as

    ``missing`` is a statement about HubSpot. ``unknown`` is a statement about
    this check, and a caller must never read it as ``missing``.

    Also returned: ``batches`` / ``batches_answered`` / ``batches_unknown``,
    ``unverifiable`` (the non-numeric ids, a subset of ``unknown``; when there
    are any they count as one unknown batch), ``search_calls`` (batches sent,
    not counting 429 retries), ``stopped_early`` and ``errors`` (at most five).

    Raises only when no client can be built (``HUBSPOT_API_KEY`` unset).
    NEVER writes to HubSpot.
    """
    ids, seen = [], set()
    for raw in contact_ids or []:
        cid = str(raw or "").strip()
        if cid and cid not in seen:
            seen.add(cid)
            ids.append(cid)
    # Only an id stored in its canonical form is asked about. `0042` is the
    # number 42 to HubSpot, which would answer with `42`: an answer about a
    # contact this table does not store under that text. It is reported
    # unknown on its own — never missing, and without making the other 99 ids
    # of its batch unknown with it.
    askable = [cid for cid in ids if _canonical_object_id(cid) == cid]
    unverifiable = [cid for cid in ids if _canonical_object_id(cid) != cid]
    batches = [askable[i:i + CONTACT_EXISTENCE_BATCH_SIZE]
               for i in range(0, len(askable), CONTACT_EXISTENCE_BATCH_SIZE)]

    existing: list = []
    missing: list = []
    unknown: list = list(unverifiable)
    errors: list = []
    answered = 0
    unknown_batches = 1 if unverifiable else 0
    search_calls = 0
    consecutive_unknown = 0
    stopped_early = False
    if unverifiable:
        errors.append(f"{len(unverifiable)} stored contact id(s) are not "
                      "canonical HubSpot object ids and could not be checked")

    if batches:
        client = client or get_client()
    for index, batch in enumerate(batches):
        if consecutive_unknown >= CONTACT_EXISTENCE_MAX_CONSECUTIVE_UNKNOWN:
            # Not asked. Unknown, never missing.
            stopped_early = True
            unknown.extend(batch)
            unknown_batches += 1
            continue
        if index and CONTACT_EXISTENCE_PACING_SECONDS > 0:
            sleep(CONTACT_EXISTENCE_PACING_SECONDS)
        search_calls += 1
        try:
            returned = _existence_batch(client, batch, len(existing))
        except HubSpotPullIncompleteError as exc:
            consecutive_unknown += 1
            unknown.extend(batch)
            unknown_batches += 1
            if len(errors) < 5:
                errors.append(exc.detail)
            continue
        consecutive_unknown = 0
        answered += 1
        existing.extend(cid for cid in batch if cid in returned)
        missing.extend(cid for cid in batch if cid not in returned)

    if stopped_early:
        errors.append(
            f"stopped after {CONTACT_EXISTENCE_MAX_CONSECUTIVE_UNKNOWN} "
            "consecutive batches without a usable answer; the remaining "
            "batches were not sent")
    logger.info(
        "check_contacts_exist: %d id(s), %d existing, %d missing, %d unknown "
        "(%d of %d batch(es) answered)", len(ids), len(existing), len(missing),
        len(unknown), answered, len(batches))
    return {
        "existing": existing,
        "missing": missing,
        "unknown": unknown,
        "unverifiable": unverifiable,
        "batches": len(batches) + (1 if unverifiable else 0),
        "batches_answered": answered,
        "batches_unknown": unknown_batches,
        "search_calls": search_calls,
        "stopped_early": stopped_early,
        "errors": errors,
    }


#: The properties the direct read asks for. ``hs_merged_object_ids`` is the
#: only evidence, in the response itself, that a different record returned
#: for an id is that id's merge survivor.
CONTACT_DIRECT_READ_PROPERTIES = ["hs_object_id", "hs_merged_object_ids"]

DIRECT_READ_ABSENT = "absent"            # 404: HubSpot has no such contact
DIRECT_READ_MERGED = "merged_away"       # returned its survivor, which names it
DIRECT_READ_EXISTS = "exists"            # returned the contact itself
DIRECT_READ_UNKNOWN = "unknown"          # anything else


def _direct_read_contact(client, contact_id: str) -> tuple:
    """Read ONE contact by id and say what that proves. ``(verdict, detail)``.

    The second, independent read behind a removal (PR-ADS-163C review N1). The
    search index can leave a live contact out of a well-formed answer; a read
    of the object itself does not go through that index.

    * a 404 → ``absent``. HubSpot answered: there is no such contact;
    * a 200 carrying the SAME id → ``exists``;
    * a 200 carrying a DIFFERENT id → ``merged_away`` only when that record's
      ``hs_merged_object_ids`` names the id asked for. A different record that
      does not say so proves nothing about the id, and is ``unknown``;
    * a 429 is retried ``MAX_RETRIES`` times with backoff, then ``unknown``;
      any other API or transport error, or a response with no id, is
      ``unknown`` at once.

    Read-only: ``basic_api.get_by_id``.
    """
    for attempt in range(MAX_RETRIES + 1):
        try:
            record = client.crm.contacts.basic_api.get_by_id(
                contact_id=contact_id,
                properties=CONTACT_DIRECT_READ_PROPERTIES,
                _request_timeout=HUBSPOT_REQUEST_TIMEOUT_SECONDS,
            )
        except HUBSPOT_SDK_READ_ERRORS as exc:
            if _is_not_found(exc):
                return DIRECT_READ_ABSENT, None
            if getattr(exc, "status", None) == 429 and attempt < MAX_RETRIES:
                time.sleep(INITIAL_BACKOFF_SECONDS * (2 ** attempt))
                continue
            return DIRECT_READ_UNKNOWN, _describe_hubspot_failure(exc)
        payload = record.to_dict() if hasattr(record, "to_dict") else record
        if not isinstance(payload, dict):
            return DIRECT_READ_UNKNOWN, "the response is not a contact record"
        returned = _canonical_object_id(payload.get("id"))
        if returned is None:
            return DIRECT_READ_UNKNOWN, "the response carries no contact id"
        if returned == _canonical_object_id(contact_id):
            return DIRECT_READ_EXISTS, None
        merged = str((payload.get("properties") or {})
                     .get("hs_merged_object_ids") or "")
        merged_ids = {_canonical_object_id(part)
                      for part in merged.replace(",", ";").split(";")}
        if _canonical_object_id(contact_id) in merged_ids - {None}:
            return DIRECT_READ_MERGED, f"merged into contact {returned}"
        return DIRECT_READ_UNKNOWN, (
            f"HubSpot returned contact {returned}, which does not name "
            f"{contact_id} among its merged ids")
    return DIRECT_READ_UNKNOWN, "retry budget exhausted"   # unreachable


def confirm_contacts_absent(contact_ids, *, client=None,
                            sleep=time.sleep) -> dict:
    """Read each of ``contact_ids`` directly and report which are really gone.

    PR-ADS-163C review N1. Called only for the contacts a run is about to
    remove (at most the per-run limit), after the search has omitted each of
    them twice. One ``GET /crm/v3/objects/contacts/{id}`` per contact, paced
    like the search.

    Every id ends in exactly one list::

        absent        404 — confirmed gone
        merged_away   its merge survivor was returned and names it — confirmed
        existing      the contact itself was returned: the SEARCH was wrong
        unknown       no usable answer; not confirmed, and not "existing"

    After ``CONTACT_EXISTENCE_MAX_CONSECUTIVE_UNKNOWN`` unknown answers in a
    row the remaining ids are not read and are unknown. Also returned:
    ``direct_reads`` (ids actually read) and ``errors`` (at most five).

    Raises only when no client can be built. NEVER writes to HubSpot.
    """
    ids, seen = [], set()
    for raw in contact_ids or []:
        cid = str(raw or "").strip()
        if cid and cid not in seen:
            seen.add(cid)
            ids.append(cid)
    out = {DIRECT_READ_ABSENT: [], DIRECT_READ_MERGED: [],
           DIRECT_READ_EXISTS: [], DIRECT_READ_UNKNOWN: []}
    errors: list = []
    reads = 0
    consecutive_unknown = 0
    if ids:
        client = client or get_client()
    for cid in ids:
        if (_canonical_object_id(cid) != cid
                or consecutive_unknown
                >= CONTACT_EXISTENCE_MAX_CONSECUTIVE_UNKNOWN):
            out[DIRECT_READ_UNKNOWN].append(cid)
            continue
        if reads and CONTACT_EXISTENCE_PACING_SECONDS > 0:
            sleep(CONTACT_EXISTENCE_PACING_SECONDS)
        reads += 1
        verdict, detail = _direct_read_contact(client, cid)
        out[verdict].append(cid)
        if verdict == DIRECT_READ_UNKNOWN:
            consecutive_unknown += 1
            if detail and len(errors) < 5:
                errors.append(f"contact {cid}: {detail}")
        else:
            consecutive_unknown = 0
    logger.info(
        "confirm_contacts_absent: %d id(s): %d absent, %d merged away, "
        "%d existing, %d unknown", len(ids), len(out[DIRECT_READ_ABSENT]),
        len(out[DIRECT_READ_MERGED]), len(out[DIRECT_READ_EXISTS]),
        len(out[DIRECT_READ_UNKNOWN]))
    return {"absent": out[DIRECT_READ_ABSENT],
            "merged_away": out[DIRECT_READ_MERGED],
            "existing": out[DIRECT_READ_EXISTS],
            "unknown": out[DIRECT_READ_UNKNOWN],
            "direct_reads": reads, "errors": errors}


def pull_closed_won_deals_with_sources_in_range(date_from: str, date_to: str) -> list:
    """Pull closed-won deals by closedate with ALL associated contacts' sources.

    PR-ADS-117 source attribution: returns, per deal, every associated contact's
    Original Source + drill-down so the caller can detect ambiguous (conflicting)
    attribution without splitting revenue. Read-only — NEVER writes to HubSpot.

    A deal whose association/source lookup fails (after bounded retries) is
    returned with ``lookup_failed: True`` and NO contacts. That entry is NOT an
    answer: the caller must not write it. ``source_attribution_service`` skips
    it and keeps the chunk incomplete; the incremental sync fails the step
    (PR-ADS-163A — it used to upsert the deal as Unclassified). A deal with a
    *successful* empty association is genuinely Unclassified
    (contacts == [], lookup_failed False).

    PR-ADS-163A: the deals come from the deal search alone
    (``_iter_closed_won_deal_pages``). This used to call
    ``pull_closed_won_deals_in_range``, which resolves every deal's primary
    contact for click evidence this function never read — so a failure in a
    lookup it discarded failed the source step. A page of deals that cannot be
    read raises ``HubSpotPullIncompleteError``.

    Returns [{deal_id, deal_close_date, deal_amount_usd, lookup_failed,
              contacts:[{contact_id, source_primary, source_detail}]}].
    """
    out = []
    for page in _iter_closed_won_deal_pages(
            "pull_closed_won_deals_with_sources_in_range", date_from, date_to):
        for deal_dict in page:
            props = deal_dict.get("properties", {}) or {}
            raw_id = deal_dict.get("id")
            deal_id = str(raw_id) if raw_id is not None else None
            entry = {
                "deal_id": deal_id,
                "deal_close_date": props.get("closedate") or None,
                "deal_amount_usd": props.get("amount"),
                "lookup_failed": False,
                "contacts": [],
            }
            try:
                contact_ids = _fetch_associated_contact_ids(deal_id)
                for cid in contact_ids:
                    source = _fetch_contact_source_props(cid)
                    entry["contacts"].append({
                        "contact_id": cid,
                        "source_primary": source.get("hs_analytics_source"),
                        "source_detail": source.get("hs_analytics_source_data_1"),
                    })
            except HubSpotRetryableError as exc:
                # A failed lookup — do NOT classify as Unclassified; mark the
                # deal so the caller withholds it.
                logger.warning("Source lookup failed for deal %s (retryable): %s", deal_id, exc)
                entry["lookup_failed"] = True
                entry["contacts"] = []
            out.append(entry)
    return out


def _fetch_associated_contact_ids(deal_id) -> list:
    """All associated contact IDs for a deal (v4 associations). Read-only.

    Retries on rate-limit / transient request errors; raises HubSpotRetryableError
    on persistent failure so the caller never mistakes an API failure for a deal
    that genuinely has no associated contacts. An empty list means a SUCCESSFUL
    lookup that answered ``results: []``.

    PR-ADS-163A: a 404 is raised without a retry, and a 200 without a
    ``results`` key, with a reference that has no contact id, or announcing a
    second page raises instead of being read as "no contacts".
    """
    if not deal_id:
        return []
    url = f"{HUBSPOT_API_BASE_URL}/crm/v4/objects/deals/{deal_id}/associations/contacts"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url, headers={"Authorization": f"Bearer {HUBSPOT_API_KEY}"}, timeout=30)
            if resp.status_code == 429 and attempt < MAX_RETRIES:
                time.sleep(INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue
            resp.raise_for_status()
            try:
                return _association_object_ids(resp.json(), reject_next_page=True)
            except _UnusableAssociationResponse as bad:
                raise HubSpotRetryableError(
                    f"association lookup failed for deal {deal_id}: unusable "
                    f"contact-association response ({bad})") from bad
        except (requests.exceptions.RequestException, ApiException) as exc:
            if attempt < MAX_RETRIES and not _is_not_found(exc):
                time.sleep(INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue
            raise HubSpotRetryableError(
                f"association lookup failed for deal {deal_id}: {exc}") from exc
    raise HubSpotRetryableError(f"association lookup exhausted retries for deal {deal_id}")


def _fetch_contact_source_props(contact_id) -> dict:
    """Read a contact's Original Source + drill-down properties. Read-only.

    Retries on rate-limit / transient errors; raises HubSpotRetryableError on
    persistent failure (never silently returns {} for an API failure).

    PR-ADS-163A: a 404 is raised without a retry, a transport error the SDK
    lets through is handled like an API error, and the read has a timeout.
    """
    if not contact_id:
        return {}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            contact = get_client().crm.contacts.basic_api.get_by_id(
                contact_id=str(contact_id),
                properties=["hs_analytics_source", "hs_analytics_source_data_1"],
                _request_timeout=HUBSPOT_REQUEST_TIMEOUT_SECONDS,
            )
            return contact.to_dict().get("properties", {}) or {}
        except (requests.exceptions.RequestException,
                *HUBSPOT_SDK_READ_ERRORS) as exc:
            if attempt < MAX_RETRIES and not _is_not_found(exc):
                time.sleep(INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue
            raise HubSpotRetryableError(
                f"contact source lookup failed for {contact_id}: {exc}") from exc
    raise HubSpotRetryableError(f"contact source lookup exhausted retries for {contact_id}")


def get_lead_quality_summary(contacts: list) -> dict:
    """
    Aggregate MQL status breakdown from contacts.
    Uses real HubSpot field: mql_status
    """
    summary = {
        "total": len(contacts),
        "with_gclid": 0,
        "without_gclid": 0,
        "mql_status_breakdown": {},
        "by_country": {},
        "junk_indicators": []
    }

    for c in contacts:
        props = c.get("properties", {})

        # GCLID coverage
        if props.get("hs_google_click_id"):
            summary["with_gclid"] += 1
        else:
            summary["without_gclid"] += 1

        # MQL status
        status = props.get("mql_status", "UNKNOWN")
        summary["mql_status_breakdown"][status] = \
            summary["mql_status_breakdown"].get(status, 0) + 1

        # Geography
        country = props.get("ip_country", "unknown").lower()
        summary["by_country"][country] = \
            summary["by_country"].get(country, 0) + 1

        # Junk signals in MDR comments
        comments = (props.get("mql___mdr_comments") or "").lower()
        if any(w in comments for w in ["job", "student", "wrong", "spam", "junk"]):
            summary["junk_indicators"].append({
                "contact_id": c.get("id"),
                "company": props.get("company"),
                "country": country,
                "comment": comments[:100],
                "keyword": props.get("hs_analytics_source_data_2")
            })

    summary["gclid_coverage_pct"] = round(
        summary["with_gclid"] / max(summary["total"], 1) * 100, 1
    )

    return summary


def save_output(contacts: list, deals: list, summary: dict):
    """Save pulled data to data/ directory for downstream modules."""
    os.makedirs("data", exist_ok=True)

    with open("data/crm_contacts.json", "w") as f:
        json.dump(contacts, f, indent=2, default=str)

    with open("data/crm_deals.json", "w") as f:
        json.dump(deals, f, indent=2, default=str)

    with open("data/crm_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    logger.info("Saved %d contacts, %d deals to data/", len(contacts), len(deals))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Pulling HubSpot CRM data...")
    contacts = pull_paid_search_contacts(days_back=90)
    deals = pull_deals_with_gclid(contacts)
    summary = get_lead_quality_summary(contacts)

    logger.info("GCLID coverage: %s%%", summary["gclid_coverage_pct"])
    logger.info("MQL status breakdown: %s", summary["mql_status_breakdown"])
    logger.info("Junk indicators found: %d", len(summary["junk_indicators"]))

    save_output(contacts, deals, summary)


# ═══════════════════════════════════════════════════════════════════════════
# PR-ADS-153E-A — CANONICAL DEAL SYNC (read-only)
# ═══════════════════════════════════════════════════════════════════════════
# The previous deal pulls fetched ONLY the won stage and only a handful of
# properties, so open pipeline was invisible, churn could never reverse a
# customer, and revenue carried no currency provenance (PR-ADS-153A §9.1/§9.3).
#
# This contract fetches EVERY relevant pipeline stage with the authoritative
# won boolean, the full currency trio, and all associated contacts with their
# association labels.
#
# Read-only, always: no HubSpot write, no Google Ads call of any kind, and no
# fabricated GCLID or source value.

# The stages the map can LABEL — open, won, lost, downgrade and churn. This is a
# display vocabulary, NOT a population filter: `pull_deals_for_ledger` reads
# every deal by default and labels an unrecognised stage `Unknown stage (<id>)`.
# Gating ingestion on this list would make the ledger silently incomplete the
# moment someone adds a pipeline stage in HubSpot — the canonical ledger would
# then be missing deals nobody could see were missing.
ALL_TRACKED_DEAL_STAGES = list(DEAL_STAGE_MAP.keys())

# Properties the canonical ledger needs. `hs_is_closed_won` is THE won
# predicate; the currency trio is what makes a USD claim provable.
DEAL_LEDGER_PROPERTIES = [
    "dealname",
    "pipeline",
    "dealstage",
    "hs_is_closed",
    "hs_is_closed_won",
    "createdate",
    "closedate",
    "hs_lastmodifieddate",
    "amount",
    "deal_currency_code",
    "amount_in_home_currency",
]


# Contact properties the canonical ledger's attribution evidence is built from.
# This is a DEDICATED list, not a reuse of CONTACT_PROPERTIES: the ledger must
# never read a contact's name or email address (PR-ADS-153E-A §13 — audit output
# goes into CI logs), and the properties below are exactly the ones
# ``services.hubspot_deal_sync_service._contact_evidence`` consumes.
DEAL_ATTRIBUTION_CONTACT_PROPERTIES = [
    "hs_google_click_id",          # GCLID, when HubSpot captured it directly
    "hs_analytics_first_url",      # GCLID fallback, parsed from the first URL
    "hs_analytics_source",         # Original Source
    "hs_analytics_source_data_1",  # Campaign
    "hs_analytics_source_data_2",  # Keyword
    "ip_country",                  # Country, in the product's usual precedence
    "country",
]


class ContactBatchReadError(RuntimeError):
    """A batch contact read FAILED.

    Raised rather than returning a partial mapping, so the caller can record a
    lookup failure instead of writing an empty attribution that looks like a
    successful "this contact has no source" observation.
    """


def pull_contact_attribution_properties(contact_ids: list) -> dict:
    """Batch-read the attribution evidence for a set of contacts.

    Returns ``{contact_id: {"id": contact_id, "properties": {...}}}`` — one
    documented shape, with ``properties`` holding
    ``DEAL_ATTRIBUTION_CONTACT_PROPERTIES``.

    Deliberately SEPARATE from ``pull_contacts_by_ids``, which is the PR-ADS-115
    lead-date reader and returns ``{contact_id: createdate}``. The two contracts
    are not interchangeable, and treating one as the other is what produced an
    attribute error on every deal that actually had a contact.

    Contacts HubSpot does not return are simply absent from the mapping; the
    caller compares requested against returned and treats a shortfall as a
    lookup failure rather than as an empty successful attribution.

    Raises ``ContactBatchReadError`` on API failure. Read-only — NEVER writes to
    HubSpot.
    """
    if not contact_ids:
        return {}
    ids = [str(c) for c in contact_ids if c not in (None, "")]
    if not ids:
        return {}

    client = get_client()
    out: dict = {}
    for i in range(0, len(ids), 100):   # HubSpot batch read: max 100 ids/call
        batch = ids[i:i + 100]
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = client.crm.contacts.batch_api.read(
                    batch_read_input_simple_public_object_id={
                        "properties": DEAL_ATTRIBUTION_CONTACT_PROPERTIES,
                        "inputs": [{"id": cid} for cid in batch],
                    },
                    _request_timeout=HUBSPOT_REQUEST_TIMEOUT_SECONDS,
                )
                break
            # PR-ADS-166B: the SDK never raises `requests` errors; its
            # transport failures are urllib3 errors, which this used to let
            # escape unretried. `requests` is kept as it was.
            except (*HUBSPOT_SDK_READ_ERRORS,
                    requests.exceptions.RequestException) as exc:
                if attempt < MAX_RETRIES:
                    time.sleep(INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                    continue
                raise ContactBatchReadError(
                    f"contact attribution batch read failed: {exc}") from exc
        for obj in (getattr(resp, "results", None) or []):
            d = obj.to_dict() if hasattr(obj, "to_dict") else dict(obj)
            cid = str(d.get("id"))
            out[cid] = {"id": cid, "properties": d.get("properties") or {}}
    return out


class DealAssociationLookupError(RuntimeError):
    """A deal's association lookup FAILED.

    Raised so the caller can tell a failure apart from a successful lookup that
    legitimately found no contacts. Collapsing the two would let a transient API
    error silently erase a deal's attribution (PR-ADS-153E-A §6 rule 4).
    """


def fetch_deal_associations(deal_id) -> dict:
    """Every contact associated with a deal, with association labels.

    Returns ``{deal_id, contacts: [{contact_id, association_type_id,
    association_label}], complete: True}``.

    Raises ``DealAssociationLookupError`` when the lookup could not be
    completed. An empty ``contacts`` list therefore ALWAYS means "HubSpot says
    this deal has no associated contacts", never "we could not find out".
    """
    if not deal_id:
        raise DealAssociationLookupError("deal_id is required")

    url = (f"{HUBSPOT_API_BASE_URL}/crm/v4/objects/deals/{deal_id}"
           f"/associations/contacts")
    headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}"}
    contacts: list = []
    after = None
    last_exc = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            contacts = []
            after = None
            while True:
                params = {"limit": 100}
                if after:
                    params["after"] = after
                resp = requests.get(url, headers=headers, params=params, timeout=30)
                if resp.status_code == 429 and attempt < MAX_RETRIES:
                    raise requests.exceptions.RequestException("rate limited")
                resp.raise_for_status()
                payload = resp.json() or {}
                for row in (payload.get("results") or []):
                    contact_id = row.get("toObjectId") or row.get("id")
                    if contact_id is None:
                        continue
                    types = row.get("associationTypes") or []
                    first = types[0] if types else {}
                    contacts.append({
                        "contact_id": str(contact_id),
                        "association_type_id": (
                            str(first.get("typeId")) if first.get("typeId") is not None
                            else None),
                        "association_label": first.get("label"),
                    })
                paging = (payload.get("paging") or {}).get("next") or {}
                after = paging.get("after")
                if not after:
                    break
            return {"deal_id": str(deal_id), "contacts": contacts, "complete": True}
        except (requests.exceptions.RequestException, ValueError) as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                time.sleep(INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue

    raise DealAssociationLookupError(
        f"association lookup failed for deal {deal_id}: {last_exc}")


# ---------------------------------------------------------------------------
# PR-ADS-161D2 — deal→company associations (customer identity evidence)
# ---------------------------------------------------------------------------

#: Associations per page. HubSpot's v4 association read accepts up to 500.
COMPANY_ASSOCIATION_PAGE_SIZE = 500
#: Hard bound on pages for ONE deal. A deal with more than 100,000 associated
#: companies is not a real deal; a cursor that never ends is a broken read.
COMPANY_ASSOCIATION_MAX_PAGES = 200
#: Ceiling on a server-requested Retry-After wait, so one header cannot park
#: the scheduler.
COMPANY_ASSOCIATION_MAX_RETRY_AFTER_SECONDS = 60


class DealCompanyAssociationLookupError(RuntimeError):
    """A deal→company association lookup did not complete.

    Raised — never returned as an empty list — so a caller can tell "HubSpot
    says this deal has no company" from "we could not find out". Collapsing the
    two would turn every timeout into proof that a won deal has no customer.

    ``reason`` is a short machine code with no URL, token or record content in
    it, safe to persist and to print in operator logs.
    """

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def company_association_path_key(category, type_id) -> str:
    """The stable identity of one association PATH: HubSpot's own association
    category and type id. Never a company name or a label, both of which an
    admin can rename."""
    return f"{str(category).strip().upper()}:{str(type_id).strip()}"


def _retry_after_seconds(resp, attempt: int) -> float:
    raw = (getattr(resp, "headers", None) or {}).get("Retry-After")
    try:
        wait = float(raw)
        if wait >= 0:
            return min(wait, COMPANY_ASSOCIATION_MAX_RETRY_AFTER_SECONDS)
    except (TypeError, ValueError):
        pass
    return INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))


def _company_association_page(url, params, *, sleep):
    """One page, with retries. Returns the decoded payload or raises."""
    headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}"}
    last_reason, last_detail = "lookup_failed", ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
        except requests.exceptions.Timeout as exc:
            last_reason, last_detail = "timeout", type(exc).__name__
            resp = None
        except requests.exceptions.RequestException as exc:
            last_reason, last_detail = "connection_error", type(exc).__name__
            resp = None
        if resp is not None:
            status = resp.status_code
            if status == 429 or status >= 500:
                last_reason = "rate_limited" if status == 429 else f"http_{status}"
                last_detail = ""
                if attempt < MAX_RETRIES:
                    sleep(_retry_after_seconds(resp, attempt))
                    continue
                break
            if status != 200:
                # A 4xx other than 429 will not change on retry (a deleted deal,
                # a missing scope). It is a failed lookup, never "no company".
                raise DealCompanyAssociationLookupError(f"http_{status}")
            try:
                payload = resp.json()
            except ValueError:
                last_reason, last_detail = "malformed_payload", "not JSON"
                if attempt < MAX_RETRIES:
                    sleep(INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                    continue
                break
            if not isinstance(payload, dict):
                raise DealCompanyAssociationLookupError(
                    "malformed_payload", "payload is not an object")
            return payload
        if attempt < MAX_RETRIES:
            sleep(INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)))
    raise DealCompanyAssociationLookupError(
        f"{last_reason}_after_{MAX_RETRIES}_attempts", last_detail)


def fetch_deal_company_associations(deal_id, *, sleep=time.sleep) -> dict:
    """Every company associated with a deal, by EVERY association path.

    Returns::

        {"deal_id": "...", "complete": True, "pages": n,
         "companies": [{"company_id", "association_category",
                        "association_type_id", "association_label",
                        "association_path_key"}, ...]}

    One entry per (company, path). A company reached by two association types
    appears twice, with two path keys — two paths to one company is still one
    company, and dropping the second path would lose evidence the identity
    audit reconciles against.

    Reads every page. Raises :class:`DealCompanyAssociationLookupError` on any
    failure — a non-200 response, retries exhausted, a malformed row, a cursor
    that repeats or never ends. An empty ``companies`` list is returned ONLY
    after a complete, successful read, so it always means "HubSpot reports no
    company for this deal".

    Read-only: one GET per page against the v4 associations API. NEVER writes
    to HubSpot.
    """
    if deal_id in (None, ""):
        raise DealCompanyAssociationLookupError("missing_deal_id")
    if not HUBSPOT_API_KEY:
        raise DealCompanyAssociationLookupError("hubspot_api_key_missing")

    url = (f"{HUBSPOT_API_BASE_URL}/crm/v4/objects/deals/{deal_id}"
           f"/associations/companies")
    companies: list = []
    seen: set = set()
    seen_cursors: set = set()
    after = None
    pages = 0
    while True:
        if pages >= COMPANY_ASSOCIATION_MAX_PAGES:
            raise DealCompanyAssociationLookupError(
                "pagination_page_limit", f"{pages} pages")
        params = {"limit": COMPANY_ASSOCIATION_PAGE_SIZE}
        if after:
            params["after"] = after
        payload = _company_association_page(url, params, sleep=sleep)
        pages += 1
        results = payload.get("results")
        if not isinstance(results, list):
            raise DealCompanyAssociationLookupError(
                "malformed_payload", "results is not a list")
        for row in results:
            company_id = row.get("toObjectId") if isinstance(row, dict) else None
            if company_id in (None, ""):
                raise DealCompanyAssociationLookupError(
                    "malformed_payload", "association without toObjectId")
            types = row.get("associationTypes")
            if not isinstance(types, list) or not types:
                # A company with no association type is not a path we can
                # identify; reading it as "associated somehow" would invent one.
                raise DealCompanyAssociationLookupError(
                    "malformed_payload", "association without a type")
            for t in types:
                category = (t or {}).get("category")
                type_id = (t or {}).get("typeId")
                if category in (None, "") or type_id in (None, ""):
                    raise DealCompanyAssociationLookupError(
                        "malformed_payload", "association type without id")
                key = company_association_path_key(category, type_id)
                if (str(company_id), key) in seen:
                    continue
                seen.add((str(company_id), key))
                companies.append({
                    "company_id": str(company_id),
                    "association_category": str(category).strip().upper(),
                    "association_type_id": str(type_id).strip(),
                    "association_label": (t or {}).get("label"),
                    "association_path_key": key,
                })
        nxt = (payload.get("paging") or {}).get("next") or {}
        after = nxt.get("after") if isinstance(nxt, dict) else None
        if not after:
            break
        if after in seen_cursors:
            raise DealCompanyAssociationLookupError(
                "pagination_cursor_repeated")
        seen_cursors.add(after)

    return {"deal_id": str(deal_id), "companies": companies,
            "complete": True, "pages": pages}


def pull_deals_for_ledger(
    *,
    modified_since_ms: int | None = None,
    stages: list | None = None,
    page_limit: int | None = None,
) -> dict:
    """Read EVERY deal, optionally only those modified since a watermark.

    No stage filter is applied by default. A deal whose stage id is absent from
    ``DEAL_STAGE_MAP`` is still ingested and labelled ``Unknown stage (<id>)``
    downstream: the map is a display vocabulary, and filtering the population on
    it would drop revenue for any stage added in HubSpot after this code was
    written. Pass ``stages`` explicitly to narrow the read (tests, targeted
    backfills).

    Incremental sync is driven by ``hs_lastmodifieddate``, NOT by creation
    recency: a deal created two years ago and closed today must be re-read today.

    Returns ``{available, complete, deals, pages, error, gap_proven,
    scan_watermark_ms}``: ``deals`` holds one record per deal id — its newest
    version — in the order HubSpot's index sorted them (NOT necessarily
    ascending by the returned ``hs_lastmodifieddate``: PR-ADS-175A, a deal
    modified just before the read comes back at its old position carrying its
    new time); ``pages`` counts the search responses read;
    ``scan_watermark_ms`` is the keyset position the read last reached
    (``meta["watermark_ms"]``), None when no page was read.

    KEYSET PAGING (PR-ADS-166B). This used to page by HubSpot's ``after``
    offset over ``hs_lastmodifieddate >= since``, end on the first response
    without ``paging.next``, and never read ``total``. A deal already read and
    then edited mid-scan moved to the end of the result set and shifted every
    later row up one slot, so the next offset skipped an unread deal — and the
    read still said ``complete``. Tied timestamps (a bulk edit) arrive in an
    order HubSpot does not keep between queries, so offsets over them skipped
    some and repeated others. It now reads through ``_keyset_scan``, the
    PR-ADS-161I contact loop: every page a fresh query from offset 0, ties
    walked by id, and the end accepted only when the unique deals read are at
    least HubSpot's ``total`` for the first query (``tolerant=False``).

    Outcomes:

    * ``complete`` True only after that proof.
    * PARTIAL (``available`` True, ``complete`` False, the deals read so far):
      a 429, a 5xx or a transport error that outlasted ``MAX_RETRIES``
      retries; a page with no ``results`` list; a stalled tie walk;
      ``page_limit`` reached. The error says which.
    * PARTIAL WITH A PROVEN GAP (the same, plus ``gap_proven`` True): fewer
      unique deals than HubSpot's ``total``, or no ``total`` to check. The
      read is known to have missed a deal but not which one, so no prefix is
      provably clean: the caller must not move its watermark.
    * UNAVAILABLE (``available`` False, no deals): any other API error (401,
      403, 400 …) — a refusal that asking again will not change — with the
      HTTP status in ``error``. Catches ``HUBSPOT_SDK_READ_ERRORS``: before
      PR-ADS-166B this caught the CONTACTS ``ApiException``, which the deals
      package never raises.

    WHERE A PARTIAL READ MAY CHECKPOINT — never when ``gap_proven``. Each
    query asks for ``hs_lastmodifieddate >= scan_watermark_ms``, the keyset
    position, so every deal the read did not reach sits at or after that
    position in HubSpot's index. The newest timestamp READ is not such a
    bound (PR-ADS-175A): HubSpot returns current properties at the index's
    (possibly stale) sort position, so one deal can carry a timestamp hours
    past unread deals. ``sync_deals`` therefore checkpoints at the smallest
    of its clean prefix's newest timestamp, every returned-but-unprocessed
    deal's timestamp, and — when this read stopped early —
    ``scan_watermark_ms``; it resumes from there minus an overlap with
    ``>=``. With offset paging no checkpoint was safe.
    That argument assumes HubSpot honoured every query. A shortfall against
    ``total`` is evidence it did not (e.g. a tie walk whose id sort was
    ignored, PR-ADS-166B review S4), or that an unread deal was deleted; the
    missing deal can sit anywhere, behind the prefix included.
    """
    client = get_client()

    base_filters = []
    if stages:
        base_filters.append({"propertyName": "dealstage", "operator": "IN",
                             "values": list(stages)})
    pages = 0

    def _search(filters, sort_prop):
        nonlocal pages
        response = _bounded_search(
            lambda: client.crm.deals.search_api.do_search(
                public_object_search_request={
                    "filterGroups": [{"filters": filters}],
                    "properties": DEAL_LEDGER_PROPERTIES,
                    "sorts": [{"propertyName": sort_prop,
                               "direction": "ASCENDING"}],
                    "limit": 100,
                    # Always offset 0: keyset paging never sends an offset.
                    "after": None,
                },
                _request_timeout=HUBSPOT_REQUEST_TIMEOUT_SECONDS,
            ),
            "deal")
        if not isinstance(getattr(response, "results", None), list):
            # Checked here as well as in `_keyset_scan` so `pages` counts only
            # pages that were actually read.
            raise HubSpotSearchPageUnreadableError(
                f"HubSpot deal search response carries no `results` list "
                f"(page {pages + 1}); an unreadable page is not an empty one")
        pages += 1
        return response

    # One entry per deal id. A deal read again (edited mid-scan) is re-inserted
    # at its new, later position, so the newest version is kept. The list is
    # in index order, which is NOT always ascending by the returned timestamp
    # (PR-ADS-175A); no caller may treat its newest entry as a resume point.
    by_id: dict = {}
    scan_position = {"ms": None}

    def _result(available, complete, error, gap_proven=False):
        deals = list(by_id.values()) if available else []
        return {"available": available, "complete": complete, "deals": deals,
                "pages": pages, "error": error, "gap_proven": gap_proven,
                "scan_watermark_ms": scan_position["ms"]}

    complete = False
    try:
        for fresh, meta in _keyset_scan(
                _search, modified_prop="hs_lastmodifieddate",
                since_ms=int(modified_since_ms or 0), base_filters=base_filters,
                page_size=100, max_pages=page_limit, noun="deal",
                tolerant=False):
            if meta.get("watermark_ms") is not None:
                scan_position["ms"] = int(meta["watermark_ms"])
            if meta.get("complete"):
                complete = True
                continue
            for deal in fresh:
                deal_id = str(deal.get("id") or "")
                by_id.pop(deal_id, None)
                by_id[deal_id] = deal
    except HubSpotSearchRequestError as exc:
        if not exc.transient:
            logger.error("HubSpot deal ledger pull refused on page %d: %s",
                         pages + 1, exc.detail)
            # Refused, not cut short: nothing this run read is usable.
            return _result(False, False, f"deal read refused: {exc.detail}")
        detail = f"{exc.detail} on page {pages + 1}"
        logger.error("HubSpot deal ledger pull failed: %s", detail)
        # Partial, and SAID to be partial.
        return _result(True, False, detail)
    except HubSpotSearchIncompleteError as exc:
        # A shortfall against `total` (or no `total` to check against) proves
        # that a gap exists, not where it is. No prefix of this read is
        # provably clean, so the caller must not move its watermark at all.
        logger.error("HubSpot deal ledger pull incomplete: %s", exc)
        return _result(True, False, str(exc), gap_proven=True)
    except (HubSpotSearchStalledError, HubSpotSearchPageUnreadableError) as exc:
        # Stopped before the end; what was read is a keyset-safe prefix.
        logger.error("HubSpot deal ledger pull stopped: %s", exc)
        return _result(True, False, str(exc))

    if not complete:
        return _result(True, False, "page_limit_reached")
    logger.info("Pulled %d deals for the canonical ledger across %d page(s)",
                len(by_id), pages)
    return _result(True, True, None)


def fetch_portal_home_currency() -> dict:
    """The HubSpot portal's home currency, so `amount_in_home_currency` can be
    trusted as USD only when it provably is.

    Returns ``{available, home_currency_code, verified}``. ``verified`` is True
    only when HubSpot positively reported the account's currency — this is the
    check whose absence made every revenue figure an unverified USD claim
    (PR-ADS-153A §9.3).
    """
    url = f"{HUBSPOT_API_BASE_URL}/account-info/v3/details"
    headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}"}
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        code = (resp.json() or {}).get("companyCurrency")
        if not code:
            return {"available": True, "home_currency_code": None, "verified": False}
        return {"available": True, "home_currency_code": str(code).upper(),
                "verified": True}
    except (requests.exceptions.RequestException, ValueError) as exc:
        logger.warning("Could not verify HubSpot home currency: %s", exc)
        # Unknown, and never assumed. Downstream currency resolution fails
        # closed rather than reading home amounts as USD.
        return {"available": False, "home_currency_code": None, "verified": False}
