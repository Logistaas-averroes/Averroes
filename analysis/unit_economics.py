"""
Unit Economics Calculator
Computes LTV, CAC, LTV/CAC, and payback using HubSpot revenue and Windsor spend.

Responsibility:
  - Compute core unit economics from revenue and spend data.
  - Pure calculation module — no external API calls.
  - No frontend formatting.

Core formulas:
  avg_customer_lifetime_months = 1 / monthly_churn_rate
  ltv = hs_mrr * avg_customer_lifetime_months
  cac = spend / won_deals
  ltv_to_cac = avg_ltv / cac
  payback_months = cac / avg_mrr
"""

import logging

logger = logging.getLogger(__name__)


def compute_ltv_from_mrr(mrr: float, monthly_churn_rate: float) -> float:
    """Compute lifetime value from MRR and monthly churn rate.

    LTV = MRR * (1 / monthly_churn_rate)

    Args:
        mrr: Monthly recurring revenue per customer.
        monthly_churn_rate: Monthly churn rate (0 < rate <= 1).

    Returns:
        Computed LTV. Returns 0.0 if churn rate is 0 or invalid.
    """
    if monthly_churn_rate <= 0:
        logger.warning("Invalid churn rate %s, returning 0 for LTV", monthly_churn_rate)
        return 0.0
    avg_lifetime_months = 1.0 / monthly_churn_rate
    return mrr * avg_lifetime_months


def compute_cac(spend: float, won_deals: int) -> float | None:
    """Compute Customer Acquisition Cost.

    CAC = total spend / number of won deals

    Args:
        spend: Total ad spend in the period.
        won_deals: Number of won deals in the period.

    Returns:
        CAC value, or None if won_deals is 0.
    """
    if won_deals <= 0:
        return None
    return spend / won_deals


def compute_ltv_to_cac(avg_ltv: float, cac: float) -> float | None:
    """Compute LTV/CAC ratio.

    Args:
        avg_ltv: Average lifetime value across deals.
        cac: Customer acquisition cost.

    Returns:
        LTV/CAC ratio, or None if CAC is 0 or None.
    """
    if not cac or cac <= 0:
        return None
    return avg_ltv / cac


def compute_payback_months(cac: float, avg_mrr: float) -> float | None:
    """Compute payback period in months.

    Payback = CAC / avg MRR per customer

    Args:
        cac: Customer acquisition cost.
        avg_mrr: Average monthly recurring revenue per customer.

    Returns:
        Payback in months, or None if avg_mrr is 0 or invalid.
    """
    if not avg_mrr or avg_mrr <= 0:
        return None
    if not cac or cac <= 0:
        return None
    return cac / avg_mrr
