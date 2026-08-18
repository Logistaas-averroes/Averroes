"""
Monthly Report Scheduler
Runs on the 1st of each month at 7am GMT via Render cron.
Orchestrates: google_ads_source → hubspot_pull → waste_detection → lead_quality → campaign_truth → advisor
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

    run_record = start_run("monthly")
    delivery_attempted = False
    delivery_ok = None
    run_id = None

    # Step 1: Pull Google Ads data (30-day window)
    log.info("Step 1/6 START: Pulling Google Ads data via direct Google Ads API (30 days)...")
    try:
        from connectors.google_ads_source import (
            pull_campaign_performance,
            pull_search_terms,
            pull_keyword_performance,
            pull_geo_performance,
            save_output as google_ads_save,
        )
        campaigns = pull_campaign_performance(days_back=30)
        search_terms = pull_search_terms(days_back=30)
        keywords = pull_keyword_performance(days_back=30)
        geos = pull_geo_performance(days_back=30)
        google_ads_save(campaigns, search_terms, keywords, geos)
        log.info(
            f"Step 1/6 END: Google Ads API pull complete — "
            f"{len(campaigns)} campaign rows, {len(search_terms)} search terms"
        )
    except Exception as e:
        log.error(f"Step 1/6 FAILED: Google Ads API pull error — {e}")
        finish_run(
            run_record,
            status="failed",
            failed_step="Step 1/6: Google Ads API pull",
            error_message=str(e),
        )
        if run_id is not None:
            try:
                db_writers.update_run(run_id, run_record)
            except Exception as db_exc:  # noqa: BLE001
                log.error("update_run failed: %s", db_exc)
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

        # Write run record + leads + deals to database
        run_date = datetime.utcnow().date()
        try:
            run_id = db_writers.write_run(run_record)
            if run_id is not None:
                # Track HubSpot contacts monthly sync (freshness watermark)
                contacts_batch_id = db_writers.start_sync_batch(
                    source="hubspot",
                    dataset="contacts",
                    sync_type="monthly",
                    date_from=run_date - timedelta(days=30),
                    date_to=run_date,
                    run_id=run_id,
                )
                contacts_written = db_writers.write_leads(run_id, contacts)
                if contacts_batch_id:
                    contacts_persisted = persistence_succeeded(contacts, contacts_written)
                    db_writers.finish_sync_batch(
                        batch_id=contacts_batch_id,
                        status="success" if contacts_persisted else "failed",
                        row_count=contacts_written,
                        last_source_date=max_source_date(contacts, fallback_date=run_date),
                        error_message=None if contacts_persisted else (
                            f"write_leads returned 0 for {len(contacts or [])} fetched contacts"
                        ),
                    )

                # Track HubSpot deals monthly sync (freshness watermark)
                deals_batch_id = db_writers.start_sync_batch(
                    source="hubspot",
                    dataset="deals",
                    sync_type="monthly",
                    date_from=run_date - timedelta(days=30),
                    date_to=run_date,
                    run_id=run_id,
                )
                deals_written = db_writers.write_deals(run_id, deals)
                if deals_batch_id:
                    deals_persisted = persistence_succeeded(deals, deals_written)
                    db_writers.finish_sync_batch(
                        batch_id=deals_batch_id,
                        status="success" if deals_persisted else "failed",
                        row_count=deals_written,
                        last_source_date=max_source_date(deals, fallback_date=run_date),
                        error_message=None if deals_persisted else (
                            f"write_deals returned 0 for {len(deals or [])} fetched deals"
                        ),
                    )
            else:
                log.error("DB write after Step 2: write_run returned no run_id; skipping child writes")
        except Exception as db_exc:  # noqa: BLE001
            log.error("DB write after Step 2 failed: %s", db_exc)
            run_id = None

        # Write geo rows to database
        try:
            if run_id is not None:
                geo_batch_id = db_writers.start_sync_batch(
                    source="google_ads_api",
                    dataset="geo",
                    sync_type="monthly",
                    date_from=run_date - timedelta(days=30),
                    date_to=run_date,
                    run_id=run_id,
                )
                geo_count = db_writers.write_geo(run_id, geos)
                log.info("Wrote %d geo rows to database (run_id=%s)", geo_count, run_id)
                if geo_batch_id:
                    geo_persisted = persistence_succeeded(geos, geo_count)
                    db_writers.finish_sync_batch(
                        batch_id=geo_batch_id,
                        status="success" if geo_persisted else "failed",
                        row_count=geo_count,
                        last_source_date=max_source_date(geos, fallback_date=run_date),
                        error_message=None if geo_persisted else (
                            f"write_geo returned 0 for {len(geos or [])} fetched rows"
                        ),
                    )
        except Exception as db_exc:  # noqa: BLE001
            log.error("DB write geo failed: %s", db_exc)

        # Write keyword rows to database
        try:
            if run_id is not None:
                kw_batch_id = db_writers.start_sync_batch(
                    source="google_ads_api",
                    dataset="keywords",
                    sync_type="monthly",
                    date_from=run_date - timedelta(days=30),
                    date_to=run_date,
                    run_id=run_id,
                )
                kw_count = db_writers.write_keywords(run_id, keywords)
                log.info("Wrote %d keyword rows to database (run_id=%s)", kw_count, run_id)
                if kw_batch_id:
                    kw_persisted = persistence_succeeded(keywords, kw_count)
                    db_writers.finish_sync_batch(
                        batch_id=kw_batch_id,
                        status="success" if kw_persisted else "failed",
                        row_count=kw_count,
                        last_source_date=max_source_date(keywords, fallback_date=run_date),
                        error_message=None if kw_persisted else (
                            f"write_keywords returned 0 for {len(keywords or [])} fetched rows"
                        ),
                    )

                # PR-ADS-146A: durable keyword facts via the ONE shared sync path.
                from services.keyword_sync_service import sync_keyword_daily_facts  # noqa: PLC0415
                kdf = sync_keyword_daily_facts(
                    run_date - timedelta(days=29), run_date, "monthly", run_id=run_id)
                log.info("Durable keyword facts (run_id=%s): %s", run_id, kdf)
        except Exception as db_exc:  # noqa: BLE001
            log.error("DB write keywords failed: %s", db_exc)

        # Write search term rows to database (non-fatal)
        # Google Ads API honours the requested window (days_back=30) directly.
        try:
            st_batch_id = None
            window_end = datetime.utcnow().date()
            # days_back=30 means 30 inclusive days (today − 29 through today).
            window_start = window_end - timedelta(days=29)
            st_batch_id = db_writers.start_sync_batch(
                source="google_ads_api",
                dataset="search_terms",
                sync_type="monthly",
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
                    f"Monthly search_terms persistence failed or wrote 0 rows "
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
            log.info("Wrote %d search term rows to database (run_id=%s)", st_count, run_id)
        except Exception as db_exc:  # noqa: BLE001
            if st_batch_id:
                db_writers.finish_sync_batch(
                    batch_id=st_batch_id,
                    status="failed",
                    error_message=str(db_exc)[:1000],
                )
            log.error("DB write search terms failed: %s", db_exc)

    except Exception as e:
        log.error(f"Step 2/6 FAILED: HubSpot pull error — {e}")
        finish_run(
            run_record,
            status="failed",
            failed_step="Step 2/6: HubSpot pull",
            error_message=str(e),
        )
        if run_id is not None:
            try:
                db_writers.update_run(run_id, run_record)
            except Exception as db_exc:  # noqa: BLE001
                log.error("update_run failed: %s", db_exc)
        return None

    # Validate required data files exist before running analysis
    missing = [f for f in REQUIRED_DATA_FILES if not os.path.exists(f)]
    if missing:
        for f in missing:
            log.error(f"Required data file missing after connector pull: {f}")
        log.error("Aborting monthly report — required data files not found")
        finish_run(
            run_record,
            status="failed",
            failed_step="pre-analysis data validation",
            error_message=f"Missing files: {', '.join(missing)}",
        )
        if run_id is not None:
            try:
                db_writers.update_run(run_id, run_record)
            except Exception as db_exc:  # noqa: BLE001
                log.error("update_run failed: %s", db_exc)
        return None

    # Step 3: Waste detection
    log.info("Step 3/6 START: Running waste detection...")
    try:
        from analysis.core import run_waste_detection
        waste_output = run_waste_detection()
        log.info("Step 3/6 END: Waste detection complete")

        # Write waste terms to database.
        # PR-ADS-153F: wrapped in a real `(analysis, waste_terms)` sync batch —
        # see the matching comment in scheduler/weekly.py. The freshness config
        # for this dataset has always existed; until now nothing stamped the key
        # it reads, so it could only ever report "never run".
        waste_batch_id = db_writers.start_sync_batch(
            source="analysis", dataset="waste_terms", sync_type="monthly",
            run_id=run_id,
        )
        try:
            if run_id is not None and waste_output:
                db_writers.write_waste_terms(run_id, waste_output.get("confirmed_waste_items", []))
            if waste_batch_id:
                db_writers.finish_sync_batch(
                    batch_id=waste_batch_id, status="success",
                    row_count=len((waste_output or {}).get("confirmed_waste_items", [])))
        except Exception as db_exc:  # noqa: BLE001
            log.error("DB write after Step 3 failed: %s", db_exc)
            if waste_batch_id:
                db_writers.finish_sync_batch(
                    batch_id=waste_batch_id, status="failed",
                    error_message=str(db_exc)[:1000])

    except Exception as e:
        log.error(f"Step 3/6 FAILED: Waste detection error — {e}")
        finish_run(
            run_record,
            status="failed",
            failed_step="Step 3/6: Waste detection",
            error_message=str(e),
        )
        if run_id is not None:
            try:
                db_writers.update_run(run_id, run_record)
            except Exception as db_exc:  # noqa: BLE001
                log.error("update_run failed: %s", db_exc)
        return None

    # Step 4: Lead quality analysis
    log.info("Step 4/6 START: Running lead quality analysis...")
    try:
        from analysis.core import run_lead_quality
        run_lead_quality()
        log.info("Step 4/6 END: Lead quality analysis complete")
    except Exception as e:
        log.error(f"Step 4/6 FAILED: Lead quality error — {e}")
        finish_run(
            run_record,
            status="failed",
            failed_step="Step 4/6: Lead quality",
            error_message=str(e),
        )
        if run_id is not None:
            try:
                db_writers.update_run(run_id, run_record)
            except Exception as db_exc:  # noqa: BLE001
                log.error("update_run failed: %s", db_exc)
        return None

    # Step 5: Campaign truth table
    log.info("Step 5/6 START: Building campaign truth table...")
    try:
        from analysis.core import run_campaign_truth
        campaign_truth = run_campaign_truth()
        log.info("Step 5/6 END: Campaign truth table complete")

        # Write campaigns to database (with freshness tracking)
        try:
            if run_id is not None and campaign_truth:
                campaign_rows = campaign_truth.get("campaigns", [])
                campaigns_batch_id = db_writers.start_sync_batch(
                    source="google_ads_api",
                    dataset="campaigns",
                    sync_type="monthly",
                    date_from=run_date - timedelta(days=30),
                    date_to=run_date,
                    run_id=run_id,
                )
                campaigns_written = db_writers.write_campaigns(run_id, campaign_rows)
                if campaigns_batch_id:
                    campaigns_persisted = persistence_succeeded(campaign_rows, campaigns_written)
                    db_writers.finish_sync_batch(
                        batch_id=campaigns_batch_id,
                        status="success" if campaigns_persisted else "failed",
                        row_count=campaigns_written,
                        last_source_date=run_date,
                        error_message=None if campaigns_persisted else (
                            f"write_campaigns returned 0 for {len(campaign_rows)} truth-table rows"
                        ),
                    )
        except Exception as db_exc:  # noqa: BLE001
            log.error("DB write after Step 5 failed: %s", db_exc)

        # Step 5b: GCLID match + DB persistence
        log.info("Step 5b/6 START: Running GCLID match and persisting attribution...")
        try:
            from connectors.gclid_match import run_gclid_match, save_output as gclid_save
            from datetime import date as _date
            window_end = _date.today()
            window_start = window_end

            gclid_batch_id = db_writers.start_sync_batch(
                source="gclid",
                dataset="matches",
                sync_type="monthly",
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

                # PR-ADS-153F: the coverage snapshot gets its OWN sync batch under
                # the key its freshness config reads, `(gclid, coverage_snapshots)`.
                # The table and the writer have always existed; nothing ever
                # stamped that key, so the dataset reported "never run" forever
                # while snapshots accumulated normally.
                cov_batch_id = db_writers.start_sync_batch(
                    source="gclid", dataset="coverage_snapshots",
                    sync_type="monthly", date_from=window_start,
                    date_to=window_end, run_id=run_id,
                )
                db_writers.write_gclid_coverage_snapshot(
                    run_id=run_id,
                    coverage=coverage,
                    sync_batch_id=gclid_batch_id or None,
                )
                if cov_batch_id:
                    db_writers.finish_sync_batch(
                        batch_id=cov_batch_id, status="success", row_count=1,
                        last_source_date=window_end,
                    )

                if gclid_batch_id:
                    db_writers.finish_sync_batch(
                        batch_id=gclid_batch_id,
                        status="success",
                        row_count=row_count,
                        last_source_date=window_end,
                    )
                log.info("Step 5b/6 END: GCLID attribution: %d rows persisted", row_count)
            except Exception as gclid_exc:  # noqa: BLE001
                if gclid_batch_id:
                    db_writers.finish_sync_batch(
                        batch_id=gclid_batch_id,
                        status="failed",
                        error_message=str(gclid_exc)[:1000],
                    )
                log.warning("Step 5b/6 WARN: GCLID attribution persistence failed: %s", gclid_exc)
        except Exception as gclid_import_exc:  # noqa: BLE001
            log.warning("Step 5b/6 WARN: GCLID match step failed: %s", gclid_import_exc)

    except Exception as e:
        log.error(f"Step 5/6 FAILED: Campaign truth error — {e}")
        finish_run(
            run_record,
            status="failed",
            failed_step="Step 5/6: Campaign truth",
            error_message=str(e),
        )
        if run_id is not None:
            try:
                db_writers.update_run(run_id, run_record)
            except Exception as db_exc:  # noqa: BLE001
                log.error("update_run failed: %s", db_exc)
        return None

    # Step 6: Generate monthly report via advisor (deterministic by default)
    log.info("Step 6/6 START: Generating monthly report (deterministic advisor)...")
    try:
        from analysis.advisor import generate_monthly_report
        from analysis.rule_advisor import compute_ngram_findings
        ngram_findings = compute_ngram_findings(search_terms)
        report_path = generate_monthly_report(ngram_data=ngram_findings)
    except Exception as e:
        log.error(f"Step 6/6 FAILED: Advisor error — {e}")
        finish_run(
            run_record,
            status="failed",
            failed_step="Step 6/6: Advisor",
            error_message=str(e),
        )
        if run_id is not None:
            try:
                db_writers.update_run(run_id, run_record)
            except Exception as db_exc:  # noqa: BLE001
                log.error("update_run failed: %s", db_exc)
        return None

    # Validate advisor returned a valid report path
    if not report_path:
        log.error("Step 6/6 FAILED: Advisor returned no report path")
        finish_run(
            run_record,
            status="failed",
            failed_step="Step 6/6: Advisor",
            error_message="Advisor returned no report path",
        )
        if run_id is not None:
            try:
                db_writers.update_run(run_id, run_record)
            except Exception as db_exc:  # noqa: BLE001
                log.error("update_run failed: %s", db_exc)
        return None

    # Validate report file exists on disk
    if not os.path.exists(report_path):
        log.error(f"Step 6/6 FAILED: Report file not found at {report_path}")
        finish_run(
            run_record,
            status="failed",
            failed_step="Step 6/6: Report file missing",
            error_message=f"Report file not found at {report_path}",
        )
        if run_id is not None:
            try:
                db_writers.update_run(run_id, run_record)
            except Exception as db_exc:  # noqa: BLE001
                log.error("update_run failed: %s", db_exc)
        return None

    log.info(f"Step 6/6 END: Monthly report generated — {report_path}")

    log.info("=" * 60)
    log.info(f"Monthly report complete — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    log.info(f"Report saved: {report_path}")
    log.info("=" * 60)

    # Deliver report to configured recipient
    delivery_attempted = True
    delivery_ok = deliver_report(report_path)

    finish_run(
        run_record,
        status="success",
        report_path=report_path,
        delivery_attempted=delivery_attempted,
        delivery_success=delivery_ok,
    )
    if run_id is not None:
        try:
            db_writers.update_run(run_id, run_record)
        except Exception as db_exc:  # noqa: BLE001
            log.error("update_run failed: %s", db_exc)
    return report_path


if __name__ == "__main__":
    run_monthly_report()
