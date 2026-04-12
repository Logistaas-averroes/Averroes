"""
Monthly Report Scheduler
Runs on the 1st of each month at 7am GMT via Render cron.
Orchestrates: windsor_pull → hubspot_pull → waste_detection → lead_quality → campaign_truth → advisor
No business logic lives here. This module only sequences the steps.
"""

import logging
import os
from datetime import datetime
from dotenv import load_dotenv
from scheduler.delivery import deliver_report

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

REQUIRED_DATA_FILES = [
    "data/ads_campaigns.json",
    "data/ads_search_terms.json",
    "data/crm_contacts.json",
]


def run_monthly_report():
    log.info("=" * 60)
    log.info(f"LOGISTAAS MONTHLY REPORT — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    log.info("=" * 60)

    # Step 1: Pull Google Ads data (30-day window)
    log.info("Step 1/6 START: Pulling Google Ads data via Windsor.ai (30 days)...")
    try:
        from connectors.windsor_pull import (
            pull_campaign_performance,
            pull_search_terms,
            pull_keyword_performance,
            pull_geo_performance,
            save_output as windsor_save,
        )
        campaigns = pull_campaign_performance(days_back=30)
        search_terms = pull_search_terms(days_back=30)
        keywords = pull_keyword_performance(days_back=30)
        geos = pull_geo_performance(days_back=30)
        windsor_save(campaigns, search_terms, keywords, geos)
        log.info(
            f"Step 1/6 END: Windsor pull complete — "
            f"{len(campaigns)} campaign rows, {len(search_terms)} search terms"
        )
    except Exception as e:
        log.error(f"Step 1/6 FAILED: Windsor pull error — {e}")
        return None

    # Step 2: Pull HubSpot CRM data (30-day window)
    log.info("Step 2/6 START: Pulling HubSpot CRM data (30 days)...")
    try:
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
        log.info(
            f"Step 2/6 END: HubSpot pull complete — "
            f"{len(contacts)} contacts, {len(deals)} deals with GCLID"
        )
    except Exception as e:
        log.error(f"Step 2/6 FAILED: HubSpot pull error — {e}")
        return None

    # Validate required data files exist before running analysis
    missing = [f for f in REQUIRED_DATA_FILES if not os.path.exists(f)]
    if missing:
        for f in missing:
            log.error(f"Required data file missing after connector pull: {f}")
        log.error("Aborting monthly report — required data files not found")
        return None

    # Step 3: Waste detection
    log.info("Step 3/6 START: Running waste detection...")
    try:
        from analysis.core import run_waste_detection
        run_waste_detection()
        log.info("Step 3/6 END: Waste detection complete")
    except Exception as e:
        log.error(f"Step 3/6 FAILED: Waste detection error — {e}")
        return None

    # Step 4: Lead quality analysis
    log.info("Step 4/6 START: Running lead quality analysis...")
    try:
        from analysis.core import run_lead_quality
        run_lead_quality()
        log.info("Step 4/6 END: Lead quality analysis complete")
    except Exception as e:
        log.error(f"Step 4/6 FAILED: Lead quality error — {e}")
        return None

    # Step 5: Campaign truth table
    log.info("Step 5/6 START: Building campaign truth table...")
    try:
        from analysis.core import run_campaign_truth
        run_campaign_truth()
        log.info("Step 5/6 END: Campaign truth table complete")
    except Exception as e:
        log.error(f"Step 5/6 FAILED: Campaign truth error — {e}")
        return None

    # Step 6: Generate monthly report via Claude API
    log.info("Step 6/6 START: Generating monthly report via Claude API...")
    try:
        from analysis.advisor import generate_monthly_report
        report_path = generate_monthly_report()
    except Exception as e:
        log.error(f"Step 6/6 FAILED: Advisor error — {e}")
        return None

    # Validate advisor returned a valid report path
    if not report_path:
        log.error("Step 6/6 FAILED: Advisor returned no report path")
        return None

    # Validate report file exists on disk
    if not os.path.exists(report_path):
        log.error(f"Step 6/6 FAILED: Report file not found at {report_path}")
        return None

    log.info(f"Step 6/6 END: Monthly report generated — {report_path}")

    log.info("=" * 60)
    log.info(f"Monthly report complete — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    log.info(f"Report saved: {report_path}")
    log.info("=" * 60)

    # Deliver report to configured recipient
    deliver_report(report_path)

    return report_path


if __name__ == "__main__":
    run_monthly_report()
