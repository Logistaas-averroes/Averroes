"""
HubSpot Won Deals Connector
Fetches HubSpot won deals and saves normalized deal revenue data to data/.

Responsibility:
  - Fetch deals with dealstage = 326093516 (Deal Won / Payment Received).
  - Resolve associated contact fields where possible.
  - Save output to data/hubspot_won_deals.json.
  - Connector only fetches and saves. No ROAS math here.

What is NOT in this file:
  - No attribution decisions.
  - No ROAS calculation.
  - No verdict logic.
  - No Google Ads calls.
  - No HubSpot writes.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY")
HUBSPOT_API_BASE_URL = "https://api.hubapi.com"
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 2
MAX_SEARCH_PAGES = 100

# Deal Won / Payment Received — confirmed HubSpot stage ID
WON_DEAL_STAGE_ID = "326093516"

DEAL_PROPERTIES = [
    "dealname",
    "amount",
    "amount_in_home_currency",
    "deal_currency_code",
    "hs_mrr",
    "hs_arr",
    "hs_acv",
    "closedate",
    "createdate",
    "dealstage",
    "pipeline",
    "hs_analytics_source",
    "hs_analytics_source_data_1",
    "hs_analytics_source_data_2",
    "hubspot_owner_id",
    "gclid",
]

DATA_DIR = Path(__file__).parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "hubspot_won_deals.json"


def _get_headers():
    """Return authorization headers for HubSpot API."""
    if not HUBSPOT_API_KEY:
        raise RuntimeError("HUBSPOT_API_KEY is not set")
    bearer_scheme = "Bearer"
    bearer_token = f"{bearer_scheme} {HUBSPOT_API_KEY}"
    return {
        "Authorization": bearer_token,
        "Content-Type": "application/json",
    }


def _retry_request(method, url, **kwargs):
    """Execute HTTP request with retry on 429 rate limit."""
    for attempt in range(1, MAX_RETRIES + 1):
        resp = requests.request(method, url, **kwargs)
        if resp.status_code == 429 and attempt < MAX_RETRIES:
            wait = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "HubSpot rate limited (429) — retry %d/%d in %ds",
                attempt, MAX_RETRIES, wait,
            )
            time.sleep(wait)
            continue
        return resp
    return resp


def _get_associated_contact(deal_id: str) -> dict:
    """Resolve associated contact fields for a deal."""
    contact_info = {
        "associated_contact_id": None,
        "associated_contact_country": None,
        "associated_contact_ip_country": None,
        "associated_contact_gclid": None,
    }

    try:
        # Get associations
        url = f"{HUBSPOT_API_BASE_URL}/crm/v3/objects/deals/{deal_id}/associations/contacts"
        resp = _retry_request("GET", url, headers=_get_headers())
        if resp.status_code != 200:
            return contact_info

        data = resp.json()
        results = data.get("results", [])
        if not results:
            return contact_info

        contact_id = results[0].get("id")
        if not contact_id:
            return contact_info

        contact_info["associated_contact_id"] = contact_id

        # Fetch contact details
        contact_url = (
            f"{HUBSPOT_API_BASE_URL}/crm/v3/objects/contacts/{contact_id}"
            f"?properties=country,ip_country,hs_google_click_id"
        )
        contact_resp = _retry_request("GET", contact_url, headers=_get_headers())
        if contact_resp.status_code != 200:
            return contact_info

        props = contact_resp.json().get("properties", {})
        contact_info["associated_contact_country"] = props.get("country")
        contact_info["associated_contact_ip_country"] = props.get("ip_country")
        contact_info["associated_contact_gclid"] = props.get("hs_google_click_id")

    except Exception as exc:
        logger.warning("Failed to resolve contact for deal %s: %s", deal_id, exc)

    return contact_info


def _normalize_deal(deal: dict, contact_info: dict) -> dict:
    """Normalize a deal record with safe defaults."""
    props = deal.get("properties", {})
    warnings = []

    # Normalize revenue fields — missing becomes 0 with warning
    mrr = props.get("hs_mrr")
    arr = props.get("hs_arr")
    acv = props.get("hs_acv")
    amount = props.get("amount")

    def _safe_float(val, field_name):
        if val is None or val == "":
            warnings.append(f"{field_name} missing, defaulted to 0")
            return 0.0
        try:
            return float(val)
        except (ValueError, TypeError):
            warnings.append(f"{field_name} invalid ({val}), defaulted to 0")
            return 0.0

    return {
        "deal_id": deal.get("id"),
        "dealname": props.get("dealname"),
        "amount": _safe_float(amount, "amount"),
        "amount_in_home_currency": _safe_float(
            props.get("amount_in_home_currency"), "amount_in_home_currency"
        ),
        "deal_currency_code": props.get("deal_currency_code"),
        "hs_mrr": _safe_float(mrr, "hs_mrr"),
        "hs_arr": _safe_float(arr, "hs_arr"),
        "hs_acv": _safe_float(acv, "hs_acv"),
        "closedate": props.get("closedate"),
        "createdate": props.get("createdate"),
        "dealstage": props.get("dealstage"),
        "pipeline": props.get("pipeline"),
        "hs_analytics_source": props.get("hs_analytics_source"),
        "hs_analytics_source_data_1": props.get("hs_analytics_source_data_1"),
        "hs_analytics_source_data_2": props.get("hs_analytics_source_data_2"),
        "hubspot_owner_id": props.get("hubspot_owner_id"),
        "gclid": props.get("gclid"),  # None is acceptable
        # Associated contact fields
        "associated_contact_id": contact_info.get("associated_contact_id"),
        "associated_contact_country": contact_info.get("associated_contact_country"),
        "associated_contact_ip_country": contact_info.get("associated_contact_ip_country"),
        "associated_contact_gclid": contact_info.get("associated_contact_gclid"),
        # Metadata
        "warnings": warnings,
    }


def pull_won_deals(days_back: int = 365) -> list:
    """
    Fetch HubSpot won deals using CRM search API with pagination.

    Filters on dealstage = 326093516 (Deal Won / Payment Received).
    Resolves associated contact fields where possible.
    Known limitation: contact enrichment is currently per-deal (N+1 API pattern).
    Pagination safeguards cap pages and stop if cursor does not advance.
    Saves normalized output to data/hubspot_won_deals.json.

    Returns list of normalized deal dicts.
    """
    headers = _get_headers()
    cutoff = datetime.utcnow() - timedelta(days=days_back)
    cutoff_ts = str(int(cutoff.timestamp() * 1000))

    deals = []
    after = None
    pages_fetched = 0

    search_url = f"{HUBSPOT_API_BASE_URL}/crm/v3/objects/deals/search"

    while True:
        pages_fetched += 1
        if pages_fetched > MAX_SEARCH_PAGES:
            logger.warning(
                "Stopped HubSpot pagination after %d pages (MAX_SEARCH_PAGES=%d)",
                pages_fetched - 1,
                MAX_SEARCH_PAGES,
            )
            break

        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "dealstage",
                            "operator": "EQ",
                            "value": WON_DEAL_STAGE_ID,
                        },
                        {
                            "propertyName": "closedate",
                            "operator": "GTE",
                            "value": cutoff_ts,
                        },
                    ]
                }
            ],
            "properties": DEAL_PROPERTIES,
            "limit": 100,
        }
        if after:
            payload["after"] = after

        resp = _retry_request("POST", search_url, headers=headers, json=payload)

        if resp.status_code != 200:
            logger.error(
                "HubSpot deals search failed: %d %s", resp.status_code, resp.text
            )
            break

        data = resp.json()
        results = data.get("results", [])

        for deal in results:
            deal_id = deal.get("id", "")
            contact_info = _get_associated_contact(deal_id)
            normalized = _normalize_deal(deal, contact_info)
            deals.append(normalized)

        # Pagination
        paging = data.get("paging")
        if paging and paging.get("next"):
            next_after = paging["next"].get("after")
            if not next_after:
                break
            if str(next_after) == str(after):
                logger.warning(
                    "Stopping HubSpot pagination: cursor did not advance (after=%s)", after
                )
                break
            after = next_after
        else:
            break

    # Save to data/
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(deals, f, indent=2, default=str)

    logger.info("Pulled %d won deals (last %d days) → %s", len(deals), days_back, OUTPUT_FILE)
    return deals
