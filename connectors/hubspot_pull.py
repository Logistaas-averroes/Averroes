"""
HubSpot CRM Connector
Pulls contacts (paid search source), deals, and pipeline data.
Confirmed field names from live account audit — April 2026.
"""

import logging
import os
import json
import time
import functools
from datetime import datetime, timedelta
import requests
import hubspot
from hubspot.crm.contacts import ApiException
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


def _retry_on_rate_limit(func):
    """Decorator: retry with exponential backoff on HubSpot 429 errors."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return func(*args, **kwargs)
            except ApiException as exc:
                if exc.status == 429 and attempt < MAX_RETRIES:
                    wait = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    logger.warning(
                        "HubSpot rate limited (429) — retry %d/%d in %ds",
                        attempt, MAX_RETRIES, wait,
                    )
                    time.sleep(wait)
                else:
                    raise
    return wrapper


@_retry_on_rate_limit
def pull_paid_search_contacts(days_back: int = 90) -> list:
    """
    Pull all contacts with source = PAID_SEARCH from the last N days.
    These are the contacts we can reconcile with Google Ads via GCLID.
    """
    client = get_client()
    cutoff = datetime.utcnow() - timedelta(days=days_back)
    cutoff_ts = int(cutoff.timestamp() * 1000)

    contacts = []
    after = None

    while True:
        try:
            response = client.crm.contacts.search_api.do_search(
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
                }
            )

            contacts.extend([c.to_dict() for c in response.results])

            if response.paging and response.paging.next:
                after = response.paging.next.after
            else:
                break

        except ApiException as exc:
            if exc.status == 429:
                wait = INITIAL_BACKOFF_SECONDS * 2
                logger.warning(
                    "HubSpot rate limited during pagination — waiting %ds", wait
                )
                time.sleep(wait)
                continue
            logger.error("HubSpot API error: %s", exc)
            break

    logger.info("Pulled %d paid search contacts (last %d days)", len(contacts), days_back)
    return contacts


def pull_paid_search_contacts_in_range(date_from: str, date_to: str) -> list:
    """Pull paid-search contacts created within an explicit date range.

    Used by the historical backfill framework (PR-ADS-071).
    Filters by hs_analytics_source = PAID_SEARCH and createdate GTE/LTE.
    Paginates using the HubSpot CRM search API cursor.
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
        try:
            response = client.crm.contacts.search_api.do_search(
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
                }
            )
            contacts.extend([c.to_dict() for c in response.results])

            if response.paging and response.paging.next:
                after = response.paging.next.after
            else:
                break

        except ApiException as exc:
            if exc.status == 429:
                wait = INITIAL_BACKOFF_SECONDS * 2
                logger.warning(
                    "HubSpot rate limited during backfill pagination — waiting %ds", wait
                )
                time.sleep(wait)
                continue
            logger.error("HubSpot API error during backfill: %s", exc)
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

        for attempt in range(1, MAX_RETRIES + 1):
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
                assoc_results = resp.json().get("results", [])  # TODO PR-ADS-028: add associations pagination

                for deal_ref in assoc_results:
                    deal_id = deal_ref.get("toObjectId") or deal_ref.get("id")
                    if not deal_id:
                        continue
                    deal = client.crm.deals.basic_api.get_by_id(
                        deal_id=str(deal_id),
                        properties=["dealname", "dealstage", "amount",
                                   "closedate", "createdate", "pipeline",
                                   "hs_deal_stage_probability"]
                    )
                    deal_dict = deal.to_dict()
                    deal_dict["gclid"] = gclid
                    deal_dict["contact_id"] = contact_id
                    deal_dict["stage_label"] = DEAL_STAGE_MAP.get(
                        deal_dict.get("properties", {}).get("dealstage", ""), "Unknown"
                    )
                    deals.append(deal_dict)
                break  # success — exit retry loop

            except requests.exceptions.RequestException as exc:
                if attempt < MAX_RETRIES:
                    wait = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    logger.warning(
                        "Transient error fetching associations for contact %s — retry %d/%d in %ds: %s",
                        contact_id, attempt, MAX_RETRIES, wait, exc,
                    )
                    time.sleep(wait)
                else:
                    logger.warning(
                        "Failed to fetch associations for contact %s after %d retries: %s",
                        contact_id, MAX_RETRIES, exc,
                    )
                    break
            except ApiException as exc:
                if exc.status == 429 and attempt < MAX_RETRIES:
                    wait = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    logger.warning(
                        "HubSpot rate limited on deal fetch — retry %d/%d in %ds",
                        attempt, MAX_RETRIES, wait,
                    )
                    time.sleep(wait)
                else:
                    logger.warning(
                        "Failed to fetch deals for contact %s: %s",
                        contact_id, exc,
                    )
                    break

    logger.info("Found %d deals linked to GCLID contacts", len(deals))
    return deals


def pull_closed_won_deals_in_range(date_from: str, date_to: str) -> list:
    """Pull HubSpot closed-won deals by **closedate** (revenue truth) — PR-ADS-114.

    This is the revenue-recovery / daily revenue source. It fetches deals
    DIRECTLY by dealstage = Deal Won / Payment Received and closedate in range —
    it does NOT walk recently-created contacts (which misses historical revenue).

    For each won deal it resolves the associated contact and captures the real
    GCLID / campaign / keyword / country evidence. NO synthetic GCLID is ever
    created: deals without click evidence are returned with gclid=None so the
    caller can report (and never attribute) them honestly.

    Returns a list of normalised dicts:
        {deal_id, contact_id, gclid, campaign_name, keyword, country, company,
         deal_close_date, deal_amount_usd, deal_stage, deal_stage_label}

    NEVER writes to HubSpot.
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

    deals: list = []
    after = None
    while True:
        try:
            response = client.crm.deals.search_api.do_search(
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
                }
            )
            for d in response.results:
                deals.append(_normalise_won_deal(d.to_dict()))
            if response.paging and response.paging.next:
                after = response.paging.next.after
            else:
                break
        except ApiException as exc:
            if exc.status == 429:
                wait = INITIAL_BACKOFF_SECONDS * 2
                logger.warning("HubSpot rate limited pulling closed-won deals — waiting %ds", wait)
                time.sleep(wait)
                continue
            logger.error("HubSpot API error pulling closed-won deals: %s", exc)
            break

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
    contact's properties. Read-only; tolerant of transient errors.
    """
    if not deal_id:
        return None
    client = get_client()
    try:
        assoc_url = (
            f"{HUBSPOT_API_BASE_URL}/crm/v4/objects/deals/{deal_id}/associations/contacts"
        )
        headers = {"Authorization": f"Bearer {HUBSPOT_API_KEY}"}
        resp = requests.get(assoc_url, headers=headers, timeout=30)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return None
        contact_id = results[0].get("toObjectId") or results[0].get("id")
        if not contact_id:
            return None
        contact = client.crm.contacts.basic_api.get_by_id(
            contact_id=str(contact_id), properties=CONTACT_PROPERTIES,
        )
        return contact.to_dict()
    except (requests.exceptions.RequestException, ApiException) as exc:
        logger.warning("Failed to resolve contact for deal %s: %s", deal_id, exc)
        return None


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
        try:
            response = client.crm.contacts.search_api.do_search(
                public_object_search_request={
                    "filterGroups": [{"filters": [
                        {"propertyName": "createdate", "operator": "GTE", "value": str(from_ms)},
                        {"propertyName": "createdate", "operator": "LTE", "value": str(to_ms)},
                    ]}],
                    "properties": CONTACT_PROPERTIES,
                    "limit": 100,
                    "after": after,
                }
            )
            contacts.extend([c.to_dict() for c in response.results])
            if response.paging and response.paging.next:
                after = response.paging.next.after
            else:
                break
        except ApiException as exc:
            if exc.status == 429:
                time.sleep(INITIAL_BACKOFF_SECONDS * 2)
                continue
            logger.error("HubSpot API error pulling all contacts: %s", exc)
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
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000.0, tz=_tz.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return datetime.fromtimestamp(int(text) / 1000.0, tz=_tz.utc)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=_tz.utc)


def _completion_sentinel(watermark_ms, total_yielded, page_index, reanchored):
    """The explicit end-of-result-set marker. An empty page, so it can never
    write rows or move a watermark — it only proves the scan finished."""
    return [], {
        "watermark_ms": watermark_ms,
        "watermark_iso": datetime.utcfromtimestamp(
            watermark_ms / 1000.0).isoformat() + "Z",
        "total_yielded": total_yielded,
        "page_index": page_index,
        "reanchored": reanchored,
        "complete": True,
    }


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
    a safe resume watermark. When a single query reaches HubSpot's 10,000-result
    paging cap the search is re-anchored at the newest timestamp seen instead of
    paging past it (which HubSpot refuses).

    Yields ``(contacts, page_meta)`` where ``page_meta`` carries
    ``{watermark_ms, watermark_iso, total_yielded, page_index, reanchored,
    complete}``.

    COMPLETION PROOF (PR-ADS-153B truth-safety §4): reaching the true end of the
    result set is signalled EXPLICITLY by a final sentinel page
    ``([], {"complete": True, ...})``. A caller may only treat a historical
    bootstrap as complete when it observed that sentinel. Simply running out of
    pages (e.g. because ``max_pages`` capped the run) proves nothing and yields
    no sentinel.

    Raises ``HubSpotRetryableError`` on a non-rate-limit API failure, and
    ``HubSpotSearchStalledError`` when the watermark cannot advance at the
    10,000-result boundary — a partial read is NEVER reported as a complete one.
    NEVER writes to HubSpot.
    """
    client = client or get_client()
    watermark_ms = _epoch_ms(since)
    total_yielded = 0
    page_index = 0
    reanchored = 0
    # Contact ids already emitted at the CURRENT watermark value. Bounded: it is
    # reset every time the watermark advances. Prevents re-emitting the boundary
    # rows after a re-anchor, and detects a stalled watermark.
    seen_at_watermark: set[str] = set()

    while True:
        after = None
        page_count_this_query = 0
        progressed = False

        while True:
            if max_pages is not None and page_index >= max_pages:
                # Capped run: deliberately NO completion sentinel. The caller
                # must treat this as truncated, never as a finished scan.
                return

            try:
                response = client.crm.contacts.search_api.do_search(
                    public_object_search_request={
                        "filterGroups": [{"filters": [{
                            "propertyName": "lastmodifieddate",
                            "operator": "GTE",
                            "value": str(watermark_ms),
                        }]}],
                        "properties": CONTACT_FUNNEL_PROPERTIES,
                        "sorts": [{
                            "propertyName": "lastmodifieddate",
                            "direction": "ASCENDING",
                        }],
                        "limit": page_size,
                        "after": after,
                    }
                )
            except ApiException as exc:
                if exc.status == 429:
                    wait = INITIAL_BACKOFF_SECONDS * 2
                    logger.warning(
                        "HubSpot rate limited during contact-funnel sync — waiting %ds",
                        wait,
                    )
                    time.sleep(wait)
                    continue
                raise HubSpotRetryableError(
                    f"HubSpot contact search failed (status={exc.status}): {exc}"
                ) from exc

            results = [c.to_dict() for c in (response.results or [])]
            if not results:
                yield _completion_sentinel(
                    watermark_ms, total_yielded, page_index, reanchored)
                return

            fresh = []
            for contact in results:
                cid = str(contact.get("id") or "").strip()
                modified = _epoch_ms(
                    (contact.get("properties") or {}).get("lastmodifieddate")
                )
                if modified > watermark_ms:
                    # The watermark advanced — boundary bookkeeping resets.
                    watermark_ms = modified
                    seen_at_watermark = {cid} if cid else set()
                    fresh.append(contact)
                    progressed = True
                elif cid and cid not in seen_at_watermark:
                    seen_at_watermark.add(cid)
                    fresh.append(contact)
                    progressed = True

            page_index += 1
            page_count_this_query += 1

            if fresh:
                total_yielded += len(fresh)
                yield fresh, {
                    "watermark_ms": watermark_ms,
                    "watermark_iso": datetime.utcfromtimestamp(
                        watermark_ms / 1000.0
                    ).isoformat() + "Z",
                    "total_yielded": total_yielded,
                    "page_index": page_index,
                    "reanchored": reanchored,
                    "complete": False,
                }

            paging_next = getattr(response, "paging", None)
            after = getattr(getattr(paging_next, "next", None), "after", None)

            if not after:
                yield _completion_sentinel(
                    watermark_ms, total_yielded, page_index, reanchored)
                return
            if page_count_this_query * page_size >= HUBSPOT_SEARCH_RESULT_CAP:
                # HubSpot refuses to page further; re-anchor on the newest
                # timestamp seen and start a fresh query.
                break

        if not progressed:
            # More than one full search page shares an identical modification
            # timestamp, so re-anchoring cannot advance and an unknown number of
            # contacts is unreachable. Raise rather than return: returning here
            # would let the caller mark a bootstrap COMPLETE on an incomplete
            # scan (PR-ADS-153B truth-safety §4).
            raise HubSpotSearchStalledError(
                "HubSpot contact scan stalled at the 10,000-result boundary: "
                f"more than one page shares lastmodifieddate={watermark_ms}. "
                "The scan is INCOMPLETE and must not be reported as complete."
            )
        reanchored += 1


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

        "gclid": gclid,
        "has_gclid": bool(gclid),

        "source_system": "hubspot_api",
        "lifecycle_rule_version": LIFECYCLE_RULE_VERSION,
        "mql_rule_version": MQL_STATUS_RULE_VERSION,
    }


def pull_closed_won_deals_with_sources_in_range(date_from: str, date_to: str) -> list:
    """Pull closed-won deals by closedate with ALL associated contacts' sources.

    PR-ADS-117 source attribution: returns, per deal, every associated contact's
    Original Source + drill-down so the caller can detect ambiguous (conflicting)
    attribution without splitting revenue. Read-only — NEVER writes to HubSpot.

    A deal whose association/source lookup fails (after bounded retries) is
    returned with ``lookup_failed: True`` and NO contacts, so the caller can keep
    that deal's chunk incomplete (retryable) rather than fabricating an
    Unclassified row. A deal with a *successful* empty association is genuinely
    Unclassified (contacts == [], lookup_failed False).

    Returns [{deal_id, deal_close_date, deal_amount_usd, lookup_failed,
              contacts:[{contact_id, source_primary, source_detail}]}].
    """
    raw = pull_closed_won_deals_in_range(date_from, date_to)
    out = []
    for d in raw:
        deal_id = d.get("deal_id")
        entry = {
            "deal_id": deal_id,
            "deal_close_date": d.get("deal_close_date"),
            "deal_amount_usd": d.get("deal_amount_usd"),
            "lookup_failed": False,
            "contacts": [],
        }
        try:
            contact_ids = _fetch_associated_contact_ids(deal_id)
            for cid in contact_ids:
                props = _fetch_contact_source_props(cid)
                entry["contacts"].append({
                    "contact_id": cid,
                    "source_primary": props.get("hs_analytics_source"),
                    "source_detail": props.get("hs_analytics_source_data_1"),
                })
        except HubSpotRetryableError as exc:
            # Transient/HTTP failure — do NOT classify as Unclassified; leave the
            # deal's chunk incomplete so it is retried on resume.
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
    lookup with no associations.
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
            ids = []
            for r in resp.json().get("results", []):
                cid = r.get("toObjectId") or r.get("id")
                if cid:
                    ids.append(str(cid))
            return ids
        except (requests.exceptions.RequestException, ApiException) as exc:
            if attempt < MAX_RETRIES:
                time.sleep(INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue
            raise HubSpotRetryableError(
                f"association lookup failed for deal {deal_id}: {exc}") from exc
    raise HubSpotRetryableError(f"association lookup exhausted retries for deal {deal_id}")


def _fetch_contact_source_props(contact_id) -> dict:
    """Read a contact's Original Source + drill-down properties. Read-only.

    Retries on rate-limit / transient errors; raises HubSpotRetryableError on
    persistent failure (never silently returns {} for an API failure).
    """
    if not contact_id:
        return {}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            contact = get_client().crm.contacts.basic_api.get_by_id(
                contact_id=str(contact_id),
                properties=["hs_analytics_source", "hs_analytics_source_data_1"],
            )
            return contact.to_dict().get("properties", {}) or {}
        except ApiException as exc:
            if getattr(exc, "status", None) == 429 and attempt < MAX_RETRIES:
                time.sleep(INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue
            if attempt < MAX_RETRIES:
                time.sleep(INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue
            raise HubSpotRetryableError(
                f"contact source lookup failed for {contact_id}: {exc}") from exc
        except requests.exceptions.RequestException as exc:
            if attempt < MAX_RETRIES:
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

# All stages the ledger tracks — open, won, lost, downgrade and churn. Derived
# from the live stage map so a stage can never be tracked here and unlabelled
# there.
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


def pull_deals_for_ledger(
    *,
    modified_since_ms: int | None = None,
    stages: list | None = None,
    page_limit: int | None = None,
) -> dict:
    """Read every tracked deal, optionally only those modified since a watermark.

    Incremental sync is driven by ``hs_lastmodifieddate``, NOT by creation
    recency: a deal created two years ago and closed today must be re-read today.

    Returns ``{available, complete, deals, pages, error}``.
    ``complete`` is False when pagination was cut short by an error — the caller
    must then record a PARTIAL sync rather than a successful zero-row result.
    """
    client = get_client()
    wanted_stages = stages if stages is not None else ALL_TRACKED_DEAL_STAGES

    filters = [{"propertyName": "dealstage", "operator": "IN",
                "values": wanted_stages}]
    if modified_since_ms is not None:
        filters.append({"propertyName": "hs_lastmodifieddate",
                        "operator": "GTE", "value": str(int(modified_since_ms))})

    deals: list = []
    pages = 0
    after = None
    consecutive_failures = 0

    while True:
        try:
            response = client.crm.deals.search_api.do_search(
                public_object_search_request={
                    "filterGroups": [{"filters": filters}],
                    "properties": DEAL_LEDGER_PROPERTIES,
                    # Stable ordering so a resumed/retried page cannot silently
                    # skip records.
                    "sorts": [{"propertyName": "hs_lastmodifieddate",
                               "direction": "ASCENDING"}],
                    "limit": 100,
                    "after": after,
                }
            )
        except ApiException as exc:
            if exc.status == 429 and consecutive_failures < MAX_RETRIES:
                consecutive_failures += 1
                time.sleep(INITIAL_BACKOFF_SECONDS * (2 ** (consecutive_failures - 1)))
                continue
            logger.error("HubSpot deal ledger pull failed on page %d: %s", pages, exc)
            # Partial, and SAID to be partial.
            return {"available": True, "complete": False, "deals": deals,
                    "pages": pages, "error": str(exc)}

        consecutive_failures = 0
        pages += 1
        for d in (response.results or []):
            deals.append(d.to_dict())

        if page_limit is not None and pages >= page_limit:
            return {"available": True, "complete": False, "deals": deals,
                    "pages": pages, "error": "page_limit_reached"}

        if response.paging and response.paging.next:
            after = response.paging.next.after
        else:
            break

    logger.info("Pulled %d deals for the canonical ledger across %d page(s)",
                len(deals), pages)
    return {"available": True, "complete": True, "deals": deals,
            "pages": pages, "error": None}


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
