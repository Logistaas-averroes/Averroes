"""
Weekly Report Scheduler
Runs every Monday at 7am GMT via Render cron.
Orchestrates: windsor_pull → hubspot_pull → waste_detection → lead_quality → campaign_truth → advisor
No business logic lives here. This module only sequences the steps.

Report generation uses the deterministic advisor by default (ADVISOR_MODE=deterministic).
Set ADVISOR_MODE=claude to use Claude API (requires ANTHROPIC_API_KEY).
"""

import logging
import os
from datetime import datetime
from dotenv import load_dotenv
from scheduler.delivery import deliver_report
from scheduler.run_history import start_run, finish_run
import db.writers as db_writers

log = logging.getLogger(__name__)

load_dotenv()


def run_weekly_report():
    print(f"\n{'='*60}")
    print(f"LOGISTAAS WEEKLY REPORT — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}\n")

    run_record = start_run("weekly")
    delivery_attempted = False
    delivery_ok = None
    run_id = None

    try:
        # Step 1: Pull Google Ads data (30-day window)
        print("Step 1/6: Pulling Google Ads data via Windsor.ai (30 days)...")
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
        print(f"  Windsor pull complete — {len(campaigns)} campaign rows, {len(search_terms)} search terms")

        # Step 2: Pull HubSpot CRM data (30-day window)
        print("Step 2/6: Pulling HubSpot CRM data (30 days)...")
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
        print(f"  HubSpot pull complete — {len(contacts)} contacts, {len(deals)} deals with GCLID")

        # Write run record + leads + deals to database
        try:
            run_id = db_writers.write_run(run_record)
            if run_id is not None:
                db_writers.write_leads(run_id, contacts)
                db_writers.write_deals(run_id, deals)
            else:
                log.error("[weekly] DB write after Step 2: write_run returned no run_id")
        except Exception as db_exc:  # noqa: BLE001
            log.error("[weekly] DB write after Step 2 failed: %s", db_exc)
            run_id = None

        # Step 3: Waste detection
        print("Step 3/6: Running waste detection...")
        from analysis.core import run_waste_detection
        waste_output = run_waste_detection()

        # Write waste terms to database
        try:
            if run_id is not None and waste_output:
                db_writers.write_waste_terms(run_id, waste_output.get("confirmed_waste_items", []))
        except Exception as db_exc:  # noqa: BLE001
            log.error("[weekly] DB write after Step 3 failed: %s", db_exc)

        # Step 4: Lead quality analysis
        print("Step 4/6: Running lead quality analysis...")
        from analysis.core import run_lead_quality
        run_lead_quality()

        # Step 5: Campaign truth table
        print("Step 5/6: Building campaign truth table...")
        from analysis.core import run_campaign_truth
        campaign_truth = run_campaign_truth()

        # Write campaigns to database
        try:
            if run_id is not None and campaign_truth:
                db_writers.write_campaigns(run_id, campaign_truth.get("campaigns", []))
        except Exception as db_exc:  # noqa: BLE001
            log.error("[weekly] DB write after Step 5 failed: %s", db_exc)

        # Step 6: Generate weekly report via advisor (deterministic by default)
        print("Step 6/6: Generating weekly report (deterministic advisor)...")
        from analysis.advisor import generate_weekly_report
        report_path = generate_weekly_report()

        print(f"\n{'='*60}")
        print(f"Weekly report complete — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
        if report_path:
            print(f"Report saved: {report_path}")
            delivery_attempted = True
            delivery_ok = deliver_report(report_path)
        print(f"{'='*60}\n")

        finish_run(
            run_record,
            status="success",
            report_path=report_path,
            delivery_attempted=delivery_attempted,
            delivery_success=delivery_ok,
        )
        try:
            db_writers.update_run(run_id, run_record)
        except Exception as db_exc:  # noqa: BLE001
            log.error("[weekly] update_run failed: %s", db_exc)
        return report_path

    except Exception as exc:
        finish_run(
            run_record,
            status="failed",
            delivery_attempted=delivery_attempted,
            delivery_success=delivery_ok,
            failed_step=getattr(exc, "_step", None),
            error_message=str(exc),
        )
        try:
            db_writers.update_run(run_id, run_record)
        except Exception as db_exc:  # noqa: BLE001
            log.error("[weekly] update_run (failed run) failed: %s", db_exc)
        raise


if __name__ == "__main__":
    run_weekly_report()
