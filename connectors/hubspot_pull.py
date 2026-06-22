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
        "deal_stage_label": DEAL_STAGE_MAP.get(stage, "Deal Won / Payment Received"),
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
