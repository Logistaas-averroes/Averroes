"""
GCLID Match Engine
Joins Windsor.ai click data with HubSpot contacts via hs_google_click_id.
Produces a unified matched dataset mapping Google Ads clicks to HubSpot outcomes.

Reads:
  - data/ads_search_terms.json  (Windsor output)
  - data/crm_contacts.json      (HubSpot output)
  - data/crm_deals.json         (HubSpot deals with GCLID)
  - config/thresholds.yaml (coverage threshold)

Writes:
  - data/matched_gclid.json     (matched click → contact → deal records)
  - data/gclid_coverage.json    (coverage statistics)
"""

import json
import logging
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml
from connectors.hubspot_pull import DEAL_STAGE_MAP
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DATA_DIR = "data"
CONFIG_PATH = "config/thresholds.yaml"


def _load_json(filename: str) -> list:
    """Load a JSON file from the data directory. Returns [] on missing/invalid file."""
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        logger.warning("Data file not found: %s", path)
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to load %s: %s", path, exc)
        return []


def _load_config() -> dict:
    """Load the master config file."""
    try:
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        logger.error("Failed to load config: %s", exc)
        return {}


def _extract_gclid_from_url(url: str):
    """Extract GCLID from a URL's query parameters."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        gclid_values = params.get("gclid", [])
        return gclid_values[0] if gclid_values else None
    except Exception:
        return None


def _build_contact_gclid_index(contacts: list) -> dict:
    """
    Build a lookup: GCLID → contact data.
    Uses hs_google_click_id as the primary GCLID source.
    Falls back to extracting GCLID from hs_analytics_first_url.
    """
    index = {}
    for contact in contacts:
        props = contact.get("properties", {})
        contact_id = contact.get("id")

        gclid = props.get("hs_google_click_id")

        # Fallback: try extracting from the first URL
        if not gclid:
            first_url = props.get("hs_analytics_first_url", "")
            gclid = _extract_gclid_from_url(first_url)

        if not gclid:
            continue

        index[gclid] = {
            "contact_id": contact_id,
            "company": props.get("company"),
            "country": (props.get("ip_country") or props.get("country") or "").lower(),
            "keyword": props.get("hs_analytics_source_data_2"),
            "campaign": props.get("hs_analytics_source_data_1"),
            "mql_status": props.get("mql_status"),
            "email": props.get("email"),
            "createdate": props.get("createdate"),
        }

    return index


def _build_deal_index_by_contact(deals: list) -> dict:
    """Build a lookup: contact_id → best deal info (most advanced stage)."""

    # Stage progression order (higher index = further along)
    stage_order = [
        "qualifiedtobuy",
        "334269159",
        "326093513",
        "326093515",
        "326093516",
    ]

    index = {}
    for deal in deals:
        contact_id = deal.get("contact_id")
        if not contact_id:
            continue

        deal_props = deal.get("properties", {})
        stage_id = deal_props.get("dealstage", "")
        amount = deal_props.get("amount")

        # If we already have a deal for this contact, keep the most advanced one
        if contact_id in index:
            existing_stage = index[contact_id]["deal_stage"]
            existing_rank = (
                stage_order.index(existing_stage) if existing_stage in stage_order else -1
            )
            new_rank = stage_order.index(stage_id) if stage_id in stage_order else -1
            if new_rank <= existing_rank:
                continue

        index[contact_id] = {
            "deal_stage": stage_id,
            "deal_stage_label": DEAL_STAGE_MAP.get(stage_id, "Unknown"),
            "deal_amount": float(amount) if amount else None,
        }

    return index


def _build_windsor_gclid_index(search_terms: list) -> dict:
    """
    Build a lookup of unique (campaign, keyword, match_type) from Windsor data.
    Windsor search term rows don't contain per-row GCLIDs but do contain
    campaign/keyword/match_type which we can use to enrich matched records.
    """
    index = {}
    for row in search_terms:
        keyword = (row.get("keyword") or "").lower()
        campaign = row.get("campaign", "")
        match_type = row.get("match_type", "")
        if keyword:
            index[keyword] = {
                "campaign": campaign,
                "match_type": match_type,
                "spend": float(row.get("spend", 0) or 0),
            }
    return index


def run_gclid_match() -> dict:
    """
    Main entry point: join Windsor data with HubSpot contacts via GCLID.

    Returns:
        dict with keys "matched", "coverage"
    """
    # Load data files
    search_terms = _load_json("ads_search_terms.json")
    contacts = _load_json("crm_contacts.json")
    deals = _load_json("crm_deals.json")

    # Load config for coverage threshold
    config = _load_config()
    min_coverage_pct = (
        config.get("data_quality", {}).get("gclid_coverage_warning_pct", 70)
    )

    # Build indexes
    contact_gclid_index = _build_contact_gclid_index(contacts)
    deal_index = _build_deal_index_by_contact(deals)
    windsor_keyword_index = _build_windsor_gclid_index(search_terms)

    # Count total paid search contacts (those with source = PAID_SEARCH)
    total_paid_contacts = len(contacts)
    contacts_with_gclid = len(contact_gclid_index)

    # Build matched records
    matched_records = []
    for gclid, contact_info in contact_gclid_index.items():
        contact_id = contact_info["contact_id"]
        keyword = (contact_info.get("keyword") or "").lower()

        # Enrich with Windsor keyword data if available
        windsor_data = windsor_keyword_index.get(keyword, {})

        # Get deal info if available
        deal_info = deal_index.get(contact_id, {})

        record = {
            "gclid": gclid,
            "contact_id": contact_id,
            "company": contact_info.get("company"),
            "country": contact_info.get("country"),
            "keyword": contact_info.get("keyword"),
            "campaign": contact_info.get("campaign") or windsor_data.get("campaign"),
            "match_type": windsor_data.get("match_type"),
            "mql_status": contact_info.get("mql_status"),
            "deal_stage": deal_info.get("deal_stage"),
            "deal_stage_label": deal_info.get("deal_stage_label"),
            "deal_amount": deal_info.get("deal_amount"),
            "matched": True,
        }
        matched_records.append(record)

    # Calculate coverage
    coverage_pct = round(
        contacts_with_gclid / max(total_paid_contacts, 1) * 100, 1
    )
    coverage_alert = coverage_pct < min_coverage_pct

    coverage_stats = {
        "total_paid_contacts": total_paid_contacts,
        "matched_to_windsor": contacts_with_gclid,
        "gclid_coverage_pct": coverage_pct,
        "alert": coverage_alert,
        "alert_reason": (
            f"GCLID coverage below {min_coverage_pct}% threshold"
            if coverage_alert
            else None
        ),
        "contacts_without_gclid": total_paid_contacts - contacts_with_gclid,
    }

    logger.info(
        "GCLID match complete: %d/%d contacts matched (%.1f%%)",
        contacts_with_gclid,
        total_paid_contacts,
        coverage_pct,
    )
    if coverage_alert:
        logger.warning(
            "GCLID coverage BELOW threshold: %.1f%% < %d%%",
            coverage_pct, min_coverage_pct,
        )

    return {"matched": matched_records, "coverage": coverage_stats}


def save_output(result: dict):
    """Save matched records and coverage stats to data/."""
    os.makedirs(DATA_DIR, exist_ok=True)

    matched_path = os.path.join(DATA_DIR, "matched_gclid.json")
    with open(matched_path, "w") as f:
        json.dump(result["matched"], f, indent=2, default=str)

    coverage_path = os.path.join(DATA_DIR, "gclid_coverage.json")
    with open(coverage_path, "w") as f:
        json.dump(result["coverage"], f, indent=2)

    logger.info(
        "Saved %d matched records to %s", len(result["matched"]), matched_path
    )
    logger.info("Saved coverage stats to %s", coverage_path)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Running GCLID match engine...")

    result = run_gclid_match()
    save_output(result)

    # Print coverage summary
    cov = result["coverage"]
    logger.info("Coverage: %d/%d (%.1f%%)",
                cov["matched_to_windsor"],
                cov["total_paid_contacts"],
                cov["gclid_coverage_pct"])

    if cov["alert"]:
        logger.warning("ALERT: %s", cov["alert_reason"])

    # Print sample matches
    for rec in result["matched"][:3]:
        logger.info(
            "  Match: %s | %s | %s | %s",
            rec.get("company", "N/A"),
            rec.get("country", "N/A"),
            rec.get("keyword", "N/A"),
            rec.get("mql_status", "N/A"),
        )
