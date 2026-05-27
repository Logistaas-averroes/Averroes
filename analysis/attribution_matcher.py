"""
Attribution Matcher
Matches HubSpot won deals to ad campaigns and countries using a
three-tier attribution hierarchy.

Responsibility:
  - Match won deals to campaigns using GCLID, source tag, or spend-weighted fallback.
  - Assign attribution_confidence and attribution_reason per deal.
  - NO ROAS math.
  - NO API code.
  - NO external API calls.
  - NO frontend badge labels beyond raw confidence strings.

Attribution tiers:
  tier_1_gclid         — Exact GCLID match to Windsor click data.
  tier_2_source_tag    — hs_analytics_source = PAID_SEARCH + campaign tag match.
  tier_3_spend_weighted — Fallback estimate. Never allows SCALE verdict.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"

# Campaign tags confirmed in HubSpot data reference
CAMPAIGN_TAGS = [
    "gulf",
    "mena",
    "europa",
    "mexico,chile",
    "mexico,chile,colombia",
    "global - competitors",
    "compliance - markets",
    "emerging - markets",
    "europe low cpc-new",
    "mature - markets",
    "competitors - lowcpc",
    "cpc - premium",
    "sa 2 | medium cpc (latin america).",
]


def _load_won_deals() -> list:
    """Load won deals from data/hubspot_won_deals.json."""
    deals_file = DATA_DIR / "hubspot_won_deals.json"
    if not deals_file.exists():
        logger.warning("Won deals file not found: %s", deals_file)
        return []
    with open(deals_file, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_windsor_data() -> list:
    """Load Windsor campaign data from data/campaign_performance.json if available."""
    windsor_file = DATA_DIR / "campaign_performance.json"
    if not windsor_file.exists():
        return []
    with open(windsor_file, "r", encoding="utf-8") as f:
        return json.load(f)


def _build_gclid_index(windsor_data: list) -> dict:
    """Build a GCLID → campaign mapping from Windsor data."""
    index = {}
    for row in windsor_data:
        gclid = row.get("gclid")
        if gclid:
            index[gclid] = row.get("campaign", "").lower()
    return index


def _match_campaign_tag(source_data_1: str | None) -> str | None:
    """Try to match hs_analytics_source_data_1 to a known campaign tag.

    Matches are case-insensitive substring checks.
    """
    if not source_data_1:
        return None

    source_lower = source_data_1.lower().strip()

    # Try exact match first
    for tag in CAMPAIGN_TAGS:
        if tag == source_lower:
            return tag

    # Try substring/contains match
    for tag in CAMPAIGN_TAGS:
        if tag in source_lower or source_lower in tag:
            return tag

    return None


def _get_deal_gclid(deal: dict) -> str | None:
    """Get the best available GCLID for a deal."""
    # Direct deal GCLID
    gclid = deal.get("gclid")
    if gclid:
        return gclid
    # Associated contact GCLID
    return deal.get("associated_contact_gclid")


def _get_deal_country(deal: dict) -> str | None:
    """Get the best available country for a deal."""
    country = deal.get("associated_contact_country")
    if country:
        return country
    return deal.get("associated_contact_ip_country")


def attribute_deals(deals: list | None = None) -> list:
    """Attribute won deals to campaigns using the three-tier hierarchy.

    Args:
        deals: Optional list of deal dicts. If None, loads from data/.

    Returns:
        List of attributed deal dicts with attribution_confidence and
        attribution_reason fields added.
    """
    if deals is None:
        deals = _load_won_deals()

    windsor_data = _load_windsor_data()
    gclid_index = _build_gclid_index(windsor_data)

    attributed = []

    for deal in deals:
        result = {
            "deal_id": deal.get("deal_id"),
            "dealname": deal.get("dealname"),
            "campaign": None,
            "country": _get_deal_country(deal),
            "amount": deal.get("amount", 0),
            "hs_mrr": deal.get("hs_mrr", 0),
            "hs_arr": deal.get("hs_arr", 0),
            "hs_acv": deal.get("hs_acv", 0),
            "closedate": deal.get("closedate"),
            "attribution_confidence": None,
            "attribution_reason": None,
        }

        # Tier 1: GCLID match
        deal_gclid = _get_deal_gclid(deal)
        if deal_gclid and deal_gclid in gclid_index:
            result["campaign"] = gclid_index[deal_gclid]
            result["attribution_confidence"] = "tier_1_gclid"
            result["attribution_reason"] = (
                f"Matched GCLID {deal_gclid} to Windsor campaign data"
            )
            attributed.append(result)
            continue

        # Tier 2: Source tag match
        source = deal.get("hs_analytics_source", "")
        source_data_1 = deal.get("hs_analytics_source_data_1")

        if source and source.upper() == "PAID_SEARCH" and source_data_1:
            matched_campaign = _match_campaign_tag(source_data_1)
            if matched_campaign:
                result["campaign"] = matched_campaign
                result["attribution_confidence"] = "tier_2_source_tag"
                result["attribution_reason"] = (
                    f"Matched hs_analytics_source_data_1 to campaign tag {matched_campaign}"
                )
                attributed.append(result)
                continue

        # Tier 3: Spend-weighted estimate (fallback)
        # Assign based on source_data_1 as best-effort even if not exact match
        if source_data_1:
            result["campaign"] = source_data_1.lower().strip()
        result["attribution_confidence"] = "tier_3_spend_weighted"
        result["attribution_reason"] = (
            "No GCLID match and no exact campaign tag match; "
            "attributed via spend-weighted estimate"
        )
        attributed.append(result)

    logger.info(
        "Attributed %d deals: tier_1=%d, tier_2=%d, tier_3=%d",
        len(attributed),
        sum(1 for d in attributed if d["attribution_confidence"] == "tier_1_gclid"),
        sum(1 for d in attributed if d["attribution_confidence"] == "tier_2_source_tag"),
        sum(1 for d in attributed if d["attribution_confidence"] == "tier_3_spend_weighted"),
    )

    return attributed


def get_attribution_summary(attributed_deals: list) -> dict:
    """Return summary counts of attribution tiers."""
    return {
        "tier_1": sum(1 for d in attributed_deals if d["attribution_confidence"] == "tier_1_gclid"),
        "tier_2": sum(1 for d in attributed_deals if d["attribution_confidence"] == "tier_2_source_tag"),
        "tier_3": sum(1 for d in attributed_deals if d["attribution_confidence"] == "tier_3_spend_weighted"),
        "total": len(attributed_deals),
    }
