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
INITIAL_BACKOFF_SECONDS = 2

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
                assoc_results = resp.json().get("results", [])

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
                logger.warning(
                    "Failed to fetch associations for contact %s: %s",
                    contact_id, exc,
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
