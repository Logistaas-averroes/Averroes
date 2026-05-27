"""
Attribution Confidence Layer (PR-ADS-082)

Responsibility:
  - Create confidence labels, descriptions, and severity levels for ROAS rows.
  - Summarize attribution confidence across campaign and country ROAS data.
  - Decorate ROAS rows with confidence metadata.
  - NO ROAS math.
  - NO API code.
  - NO external API calls.
  - NO writes to Google Ads, HubSpot, or any external system.

Confidence tiers:
  tier_1_gclid          — Exact GCLID match. Revenue matched to ad interaction.
  tier_2_source_tag     — HubSpot paid-search source/campaign tag match.
  tier_3_spend_weighted — Fallback estimate. Use directionally, not as final truth.
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

CONFIDENCE_DEFINITIONS = {
    "tier_1_gclid": {
        "label": "Exact GCLID",
        "severity": "high",
        "trust_level": "exact",
        "description": "Revenue is matched to ad interaction using GCLID-level evidence.",
    },
    "tier_2_source_tag": {
        "label": "Source Tag",
        "severity": "medium",
        "trust_level": "directional",
        "description": "Revenue is matched using HubSpot paid-search source/campaign tags.",
    },
    "tier_3_spend_weighted": {
        "label": "Estimate",
        "severity": "low",
        "trust_level": "estimated",
        "description": (
            "Revenue is estimated using fallback allocation. "
            "Use directionally, not as final truth."
        ),
    },
}


def get_confidence_label(confidence: str) -> str:
    """Return human-readable label for a confidence tier."""
    defn = CONFIDENCE_DEFINITIONS.get(confidence)
    if defn:
        return defn["label"]
    return "Unknown"


def get_confidence_description(confidence: str) -> str:
    """Return description for a confidence tier."""
    defn = CONFIDENCE_DEFINITIONS.get(confidence)
    if defn:
        return defn["description"]
    return "Attribution method is unknown."


def get_confidence_severity(confidence: str) -> str:
    """Return severity level for a confidence tier."""
    defn = CONFIDENCE_DEFINITIONS.get(confidence)
    if defn:
        return defn["severity"]
    return "low"


def _determine_overall_confidence(
    tier_1_share: float, tier_2_share: float, tier_3_share: float, total_rows: int
) -> str:
    """Determine overall confidence level.

    Rules:
      HIGH    — tier_1_share >= 70%
      MEDIUM  — tier_1_share + tier_2_share >= 70%
      LOW     — tier_3_share > 50%
      UNKNOWN — no rows available
    """
    if total_rows == 0:
        return "UNKNOWN"
    if tier_1_share >= 0.70:
        return "HIGH"
    if (tier_1_share + tier_2_share) >= 0.70:
        return "MEDIUM"
    if tier_3_share > 0.50:
        return "LOW"
    return "MEDIUM"


def _generate_confidence_message(overall: str) -> str:
    """Generate a human-readable confidence message."""
    messages = {
        "HIGH": (
            "Most ROAS rows have exact GCLID attribution. "
            "Revenue data is high-confidence."
        ),
        "MEDIUM": (
            "ROAS rows are mostly source-tag matched. "
            "Revenue data is directional but not fully exact."
        ),
        "LOW": (
            "Most ROAS rows are estimated. "
            "Use revenue pages directionally until GCLID matching is wired."
        ),
        "UNKNOWN": (
            "No ROAS rows available to assess confidence."
        ),
    }
    return messages.get(overall, messages["UNKNOWN"])


def summarize_roas_confidence(campaigns: list, countries: list) -> dict:
    """Summarize attribution confidence across ROAS data.

    Args:
        campaigns: List of campaign ROAS dicts (each with attribution_confidence).
        countries: List of country ROAS dicts (each with attribution_confidence).

    Returns:
        Summary dict with tier counts, shares, overall confidence, and message.
    """
    all_rows = (campaigns or []) + (countries or [])

    campaign_rows = len(campaigns or [])
    country_rows = len(countries or [])
    total_rows = len(all_rows)

    tier_1_count = sum(
        1 for r in all_rows if r.get("attribution_confidence") == "tier_1_gclid"
    )
    tier_2_count = sum(
        1 for r in all_rows if r.get("attribution_confidence") == "tier_2_source_tag"
    )
    tier_3_count = sum(
        1 for r in all_rows
        if r.get("attribution_confidence") == "tier_3_spend_weighted"
    )

    tier_1_share = tier_1_count / total_rows if total_rows > 0 else 0.0
    tier_2_share = tier_2_count / total_rows if total_rows > 0 else 0.0
    tier_3_share = tier_3_count / total_rows if total_rows > 0 else 0.0

    overall = _determine_overall_confidence(
        tier_1_share, tier_2_share, tier_3_share, total_rows
    )
    message = _generate_confidence_message(overall)

    return {
        "campaign_rows": campaign_rows,
        "country_rows": country_rows,
        "tier_1_count": tier_1_count,
        "tier_2_count": tier_2_count,
        "tier_3_count": tier_3_count,
        "tier_1_share": round(tier_1_share, 3),
        "tier_2_share": round(tier_2_share, 3),
        "tier_3_share": round(tier_3_share, 3),
        "overall_confidence": overall,
        "message": message,
    }


def decorate_roas_rows_with_confidence(rows: list) -> list:
    """Decorate ROAS rows with confidence metadata.

    Adds confidence_label, confidence_description, and trust_level
    to each row based on its attribution_confidence field.

    Args:
        rows: List of ROAS row dicts.

    Returns:
        Same list with confidence metadata added to each row.
    """
    for row in rows:
        confidence = row.get("attribution_confidence", "")
        defn = CONFIDENCE_DEFINITIONS.get(confidence, {})
        row["confidence_label"] = defn.get("label", "Unknown")
        row["confidence_description"] = defn.get("description", "Attribution method is unknown.")
        row["trust_level"] = defn.get("trust_level", "unknown")
    return rows
