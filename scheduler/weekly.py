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
from datetime import datetime, timedelta
from dotenv import load_dotenv
from scheduler.delivery import deliver_report
from scheduler.run_history import start_run, finish_run
from scheduler.sync_utils import max_source_date, persistence_succeeded
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

        # Write geo rows to database
        try:
            if run_id is not None:
                geo_count = db_writers.write_geo(run_id, geos)
                log.info("[weekly] Wrote %d geo rows to database", geo_count)
        except Exception as db_exc:  # noqa: BLE001
            log.error("[weekly] DB write geo failed: %s", db_exc)

        # Write keyword rows to database
        try:
            if run_id is not None:
                kw_count = db_writers.write_keywords(run_id, keywords)
                log.info("[weekly] Wrote %d keyword rows to database", kw_count)
        except Exception as db_exc:  # noqa: BLE001
            log.error("[weekly] DB write keywords failed: %s", db_exc)

        # Write search term rows to database
        try:
            st_batch_id = None
            window_end = datetime.utcnow().date()
            # Inclusive 14-day search-term window: today minus 13 days through today.
            window_start = window_end - timedelta(days=13)
            st_batch_id = db_writers.start_sync_batch(
                source="windsor",
                dataset="search_terms",
                sync_type="weekly",
                date_from=window_start,
                date_to=window_end,
                run_id=run_id,
            )

            st_count = db_writers.write_search_terms(
                run_id=run_id,
                search_term_rows=search_terms,
                sync_batch_id=st_batch_id or None,
            )
            if not persistence_succeeded(search_terms, st_count):
                raise RuntimeError(
                    f"Weekly search_terms persistence failed or wrote 0 rows "
                    f"for non-empty fetch ({len(search_terms or [])} rows)"
                )
            last_source_date = max_source_date(search_terms, fallback_date=window_end)
            if st_batch_id:
                db_writers.finish_sync_batch(
                    batch_id=st_batch_id,
                    status="success",
                    row_count=st_count,
                    last_source_date=last_source_date,
                )
            log.info("[weekly] Wrote %d search term rows to database", st_count)
        except Exception as db_exc:  # noqa: BLE001
            if st_batch_id:
                db_writers.finish_sync_batch(
                    batch_id=st_batch_id,
                    status="failed",
                    error_message=str(db_exc)[:1000],
                )
            log.error("[weekly] DB write search terms failed: %s", db_exc)

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

        # Step 5b: GCLID match + DB persistence
        print("Step 5b/6: Running GCLID match and persisting attribution...")
        try:
            from connectors.gclid_match import run_gclid_match, save_output as gclid_save
            from datetime import date as _date
            window_end = _date.today()
            window_start = window_end

            gclid_batch_id = db_writers.start_sync_batch(
                source="gclid",
                dataset="matches",
                sync_type="weekly",
                # window_start = window_end: GCLID match reads from JSON files
                # already written by the connector steps above; the "window"
                # is the run-date snapshot, not a date range fetch.
                date_from=window_start,
                date_to=window_end,
                run_id=run_id,
            )
            try:
                gclid_result = run_gclid_match()
                gclid_save(gclid_result)
                matched_rows = gclid_result.get("matched", [])
                coverage = gclid_result.get("coverage", {})

                row_count = db_writers.write_gclid_attribution(
                    run_id=run_id,
                    matched_rows=matched_rows,
                    sync_batch_id=gclid_batch_id or None,
                )

                if matched_rows and row_count == 0:
                    raise RuntimeError(
                        "GCLID attribution persistence wrote 0 rows for non-empty match output"
                    )

                db_writers.write_gclid_coverage_snapshot(
                    run_id=run_id,
                    coverage=coverage,
                    sync_batch_id=gclid_batch_id or None,
                )

                if gclid_batch_id:
                    db_writers.finish_sync_batch(
                        batch_id=gclid_batch_id,
                        status="success",
                        row_count=row_count,
                        last_source_date=window_end,
                    )
                log.info("[weekly] GCLID attribution: %d rows persisted", row_count)
            except Exception as gclid_exc:  # noqa: BLE001
                if gclid_batch_id:
                    db_writers.finish_sync_batch(
                        batch_id=gclid_batch_id,
                        status="failed",
                        error_message=str(gclid_exc)[:1000],
                    )
                log.warning("[weekly] GCLID attribution persistence failed: %s", gclid_exc)
        except Exception as gclid_import_exc:  # noqa: BLE001
            log.warning("[weekly] GCLID match step failed: %s", gclid_import_exc)

        # Step 6: Generate weekly report via advisor (deterministic by default)
        print("Step 6/6: Generating weekly report (deterministic advisor)...")
        from analysis.advisor import generate_weekly_report
        from analysis.rule_advisor import compute_ngram_findings
        ngram_findings = compute_ngram_findings(search_terms)
        report_path = generate_weekly_report(ngram_data=ngram_findings)

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
