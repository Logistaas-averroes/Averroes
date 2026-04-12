"""
Weekly Report Scheduler
Runs every Monday at 7am GMT via Render cron.
Orchestrates: windsor_pull → hubspot_pull → gclid_match → waste_detection → lead_quality → campaign_truth → advisor
No business logic lives here. This module only sequences the steps.
"""

import logging
import os
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] weekly: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Files expected to exist after each step — checked before proceeding.
_WINDSOR_FILES = [
    "data/ads_campaigns.json",
    "data/ads_search_terms.json",
    "data/ads_keywords.json",
    "data/ads_geos.json",
]
_HUBSPOT_FILES = [
    "data/crm_contacts.json",
    "data/crm_deals.json",
    "data/crm_summary.json",
]
_GCLID_FILES = [
    "data/matched_gclid.json",
    "data/gclid_coverage.json",
]
# Minimum data inputs required before running analysis
_ANALYSIS_INPUT_FILES = [
    "data/ads_campaigns.json",
    "data/ads_search_terms.json",
    "data/ads_keywords.json",
    "data/crm_contacts.json",
    "data/crm_deals.json",
]


def _require_files(paths: list, step_name: str) -> None:
    """Abort with a clear message if any expected output file is missing."""
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        msg = f"{step_name}: required files missing — {', '.join(missing)}"
        logger.error(msg)
        raise RuntimeError(msg)


def run_weekly_report():
    logger.info("=" * 60)
    logger.info("LOGISTAAS WEEKLY REPORT — %s", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
    logger.info("=" * 60)

    # Step 1: Pull Google Ads data (30-day window)
    logger.info("Step 1/7: Pulling Google Ads data via Windsor.ai (30 days)...")
    from connectors.windsor_pull import (
        pull_campaign_performance,
        pull_search_terms,
        pull_keyword_performance,
        pull_geo_performance,
        save_output as windsor_save,
    )
    campaigns = pull_campaign_performance(days_back=30)
    search_terms = pull_search_terms(days_back=14)  # 14-day window: Windsor search term data is unreliable beyond 14 days
    keywords = pull_keyword_performance(days_back=30)
    geos = pull_geo_performance(days_back=30)
    windsor_save(campaigns, search_terms, keywords, geos)
    _require_files(_WINDSOR_FILES, "Step 1 (Windsor pull)")
    logger.info("Step 1 complete — %d campaign rows, %d search terms", len(campaigns), len(search_terms))

    # Step 2: Pull HubSpot CRM data (30-day window)
    logger.info("Step 2/7: Pulling HubSpot CRM data (30 days)...")
    from connectors.hubspot_pull import (
        pull_paid_search_contacts,
        pull_deals_with_gclid,
        get_lead_quality_summary,
        save_output as hubspot_save,
    )
    contacts = pull_paid_search_contacts(days_back=30)
    deals = pull_deals_with_gclid(contacts)
    crm_summary = get_lead_quality_summary(contacts)
    hubspot_save(contacts, deals, crm_summary)
    _require_files(_HUBSPOT_FILES, "Step 2 (HubSpot pull)")
    logger.info("Step 2 complete — %d contacts, %d deals with GCLID", len(contacts), len(deals))

    # Step 3: GCLID reconciliation
    logger.info("Step 3/7: Running GCLID reconciliation...")
    from connectors.gclid_match import run_gclid_match, save_output as gclid_save
    gclid_result = run_gclid_match()
    if not gclid_result:
        msg = "Step 3 (GCLID reconciliation): run_gclid_match() returned no result"
        logger.error(msg)
        raise RuntimeError(msg)
    gclid_save(gclid_result)
    _require_files(_GCLID_FILES, "Step 3 (GCLID reconciliation)")
    logger.info("Step 3 complete — %d matched records", len(gclid_result["matched"]))

    # Pre-analysis sanity check: all required data inputs must be present
    _require_files(_ANALYSIS_INPUT_FILES, "Pre-analysis data check")
    logger.info("Pre-analysis data check passed")

    # Step 4: Waste detection
    logger.info("Step 4/7: Running waste detection...")
    from analysis.core import run_waste_detection
    run_waste_detection()
    _require_files(["outputs/waste_report.json"], "Step 4 (waste detection)")
    logger.info("Step 4 complete — waste_report.json written")

    # Step 5: Lead quality analysis
    logger.info("Step 5/7: Running lead quality analysis...")
    from analysis.core import run_lead_quality
    run_lead_quality()
    _require_files(["outputs/lead_quality.json"], "Step 5 (lead quality)")
    logger.info("Step 5 complete — lead_quality.json written")

    # Step 6: Campaign truth table
    logger.info("Step 6/7: Building campaign truth table...")
    from analysis.core import run_campaign_truth
    run_campaign_truth()
    _require_files(["outputs/campaign_truth.json"], "Step 6 (campaign truth)")
    logger.info("Step 6 complete — campaign_truth.json written")

    # Step 7: Generate weekly report via Claude API
    logger.info("Step 7/7: Generating weekly report via Claude API...")
    from analysis.advisor import generate_weekly_report
    report_path = generate_weekly_report()

    if not report_path:
        msg = "Step 7 (advisor): generate_weekly_report() returned None — report was not produced"
        logger.error(msg)
        raise RuntimeError(msg)

    if not os.path.exists(report_path):
        msg = f"Step 7 (advisor): report path returned but file not found — {report_path}"
        logger.error(msg)
        raise RuntimeError(msg)

    logger.info("Step 7 complete — report saved: %s", report_path)
    logger.info("=" * 60)
    logger.info("Weekly report complete — %s", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
    logger.info("=" * 60)

    return report_path


if __name__ == "__main__":
    run_weekly_report()
