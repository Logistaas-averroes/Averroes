"""
HubSpot Churn Input Connector
Reads and validates manual churn values from local YAML config.

Responsibility:
  - Load churn configuration from config/churn_input.yaml.
  - Provide monthly churn rate with campaign override support.
  - Validate churn rates are within valid bounds.

What is NOT in this file:
  - No LTV math beyond returning churn input.
  - No API endpoint logic.
  - No frontend-specific formatting.
"""

import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

CONFIG_FILE = Path(__file__).parent.parent / "config" / "churn_input.yaml"


def load_churn_input() -> dict:
    """Load churn configuration from config/churn_input.yaml.

    Returns the full config dict with keys:
      - default_monthly_churn
      - monthly (dict of month → rate)
      - campaign_overrides (dict of campaign → rate)
    """
    if not CONFIG_FILE.exists():
        logger.warning("Churn config not found at %s, using defaults", CONFIG_FILE)
        return {
            "default_monthly_churn": 0.03,
            "monthly": {},
            "campaign_overrides": {},
        }

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    return {
        "default_monthly_churn": config.get("default_monthly_churn", 0.03),
        "monthly": config.get("monthly", {}),
        "campaign_overrides": config.get("campaign_overrides", {}),
    }


def validate_churn_rate(rate) -> bool:
    """Validate that a churn rate is between 0 and 1 (inclusive).

    Returns True if valid, False otherwise.
    """
    try:
        rate_float = float(rate)
        return 0 <= rate_float <= 1
    except (TypeError, ValueError):
        return False


def get_monthly_churn(month: str, campaign: str | None = None) -> dict:
    """Get the applicable monthly churn rate for a given month and optional campaign.

    Priority:
      1. Campaign override (if campaign is specified and override exists)
      2. Monthly value (if specific month value exists)
      3. Default monthly churn

    Args:
        month: Month string in YYYY-MM format (e.g., "2026-05").
        campaign: Optional campaign name for campaign-specific override.

    Returns:
        Dict with keys:
          - monthly_churn_rate: float
          - source: str ("campaign_override", "monthly", or "default")
          - month: str
          - campaign: str | None
    """
    config = load_churn_input()
    campaign_overrides = config.get("campaign_overrides", {})
    monthly_values = config.get("monthly", {})
    default_churn = config.get("default_monthly_churn", 0.03)

    # Priority 1: Campaign override
    if campaign and campaign.lower() in campaign_overrides:
        rate = campaign_overrides[campaign.lower()]
        if validate_churn_rate(rate):
            return {
                "monthly_churn_rate": float(rate),
                "source": "campaign_override",
                "month": month,
                "campaign": campaign,
            }

    # Priority 2: Monthly value
    if month in monthly_values:
        rate = monthly_values[month]
        if validate_churn_rate(rate):
            return {
                "monthly_churn_rate": float(rate),
                "source": "monthly",
                "month": month,
                "campaign": campaign,
            }

    # Priority 3: Default
    return {
        "monthly_churn_rate": float(default_churn),
        "source": "default",
        "month": month,
        "campaign": campaign,
    }
