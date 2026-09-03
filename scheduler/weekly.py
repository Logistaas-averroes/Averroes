"""
Weekly Report Scheduler
Runs every Monday at 7am GMT via Render cron.
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
        #
        # PR-ADS-156-F1 §6: search terms are NOT pulled here. This step used to
        # call `pull_search_terms(days_back=60)` and then, ~150 lines later, the
        # canonical service pulled the same 60 days again — two Google Ads
        # queries per weekly run for one dataset, and two answers that nothing
        # reconciled. The analysis below now reads the rows the canonical sync
        # actually persisted, so the snapshot and the database describe the same
        # observation by construction rather than by coincidence.
        print(
            "Step 1/6: Pulling Google Ads data via direct Google Ads API "
            "(campaigns/keywords/geo=30d; search_terms=60d, pulled once by the "
            "canonical service below)..."
        )
        from connectors.google_ads_source import (
            pull_campaign_performance,
            pull_keyword_performance,
            pull_geo_performance,
            save_output as google_ads_save,
        )
        campaigns = pull_campaign_performance(days_back=30)
        keywords = pull_keyword_performance(days_back=30)
        geos = pull_geo_performance(days_back=30)
        # `None` for search terms leaves data/ads_search_terms.json untouched
        # until the canonical sync below has actually persisted this week's rows.
        google_ads_save(campaigns, None, keywords, geos)
        print(f"  Google Ads API pull complete — {len(campaigns)} campaign rows")

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
        run_date = datetime.utcnow().date()
        try:
            run_id = db_writers.write_run(run_record)
            if run_id is not None:
                # Track HubSpot contacts weekly sync (freshness watermark)
                contacts_batch_id = db_writers.start_sync_batch(
                    source="hubspot",
                    dataset="contacts",
                    sync_type="weekly",
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

                # Track HubSpot deals weekly sync (freshness watermark)
                deals_batch_id = db_writers.start_sync_batch(
                    source="hubspot",
                    dataset="deals",
                    sync_type="weekly",
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
                log.error("[weekly] DB write after Step 2: write_run returned no run_id")
        except Exception as db_exc:  # noqa: BLE001
            log.error("[weekly] DB write after Step 2 failed: %s", db_exc)
            run_id = None

        # Write geo rows to database
        try:
            if run_id is not None:
                geo_batch_id = db_writers.start_sync_batch(
                    source="google_ads_api",
                    dataset="geo",
                    sync_type="weekly",
                    date_from=run_date - timedelta(days=30),
                    date_to=run_date,
                    run_id=run_id,
                )
                geo_count = db_writers.write_geo(run_id, geos)
                log.info("[weekly] Wrote %d geo rows to database", geo_count)
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
            log.error("[weekly] DB write geo failed: %s", db_exc)

        # Write keyword rows to database
        try:
            if run_id is not None:
                kw_batch_id = db_writers.start_sync_batch(
                    source="google_ads_api",
                    dataset="keywords",
                    sync_type="weekly",
                    date_from=run_date - timedelta(days=30),
                    date_to=run_date,
                    run_id=run_id,
                )
                kw_count = db_writers.write_keywords(run_id, keywords)
                log.info("[weekly] Wrote %d keyword rows to database", kw_count)
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

                # PR-ADS-146A: durable keyword facts via the ONE shared sync path
                # (the same sync_keyword_daily_facts used by daily / weekly /
                # monthly, the admin refresh action and the bootstrap — no
                # competing keyword-fact persistence implementations). It creates +
                # finishes its own keyword_facts sync batch and updates sync_state.
                from services.keyword_sync_service import sync_keyword_daily_facts  # noqa: PLC0415
                kdf = sync_keyword_daily_facts(
                    run_date - timedelta(days=29), run_date, "weekly", run_id=run_id)
                log.info("[weekly] Durable keyword facts: %s", kdf)
        except Exception as db_exc:  # noqa: BLE001
            log.error("[weekly] DB write keywords failed: %s", db_exc)

        # Write search term rows to database
        #
        # PR-ADS-156 §5: persistence goes through the ONE shared canonical
        # search-term service. What used to be here was the second of three
        # inline copies of pull → batch → write → judge → finish, and it was the
        # copy that recorded a zero-row pull as `success` while writing the error
        # message "evidence pipeline unavailable" — so a legitimately quiet week
        # and a broken connector left the same durable record.
        #
        # The 60-day weekly recovery window is preserved: different triggers may
        # ask for different windows, they simply no longer carry different rules
        # for what a successful sync means.
        #
        # PR-ADS-156-F1 §6: this is the ONLY search-term pull in the weekly run.
        # `include_rows=True` returns the rows that were persisted, and they are
        # adopted only when the sync reports ok — everything downstream (the
        # JSON snapshot, waste detection, the n-gram findings in the report)
        # then describes rows that are genuinely in the database. Analysis over
        # transient rows is analysis nobody can audit afterwards.
        search_terms = []
        search_terms_available = False
        try:
            from services.search_term_sync_service import (  # noqa: PLC0415
                sync_recent_search_terms,
            )
            st = sync_recent_search_terms("weekly", days=60, run_id=run_id,
                                          include_rows=True)
            if st.get("ok"):
                search_terms = st.get("rows") or []
                search_terms_available = True
                log.info("[weekly] Canonical search-term sync: %s", {
                    k: st.get(k) for k in
                    ("date_from", "date_to", "fetched", "written", "verified_empty")})
            else:
                log.error(
                    "[weekly] Canonical search-term sync failed (%s) — "
                    "search-term analysis is unavailable for this run",
                    st.get("error"))
        except Exception as db_exc:  # noqa: BLE001
            log.error("[weekly] DB write search terms failed: %s", db_exc)

        # The local JSON snapshot is written from the SAME rows, and only when
        # they were persisted. Overwriting last week's snapshot with an empty
        # list because this week's pull failed would destroy the only remaining
        # copy of the previous observation.
        if search_terms_available:
            google_ads_save(None, search_terms, None, None)
            print(f"  Snapshot saved — {len(search_terms)} persisted search terms")
        else:
            log.warning("[weekly] search-term snapshot not refreshed — the "
                        "canonical sync did not persist rows this run")
            print("  Search-term snapshot NOT refreshed (canonical sync failed)")

        # Step 3: Waste detection
        print("Step 3/6: Running waste detection...")
        from analysis.core import run_waste_detection
        waste_output = run_waste_detection()

        # Write waste terms to database.
        # PR-ADS-153F: wrapped in a real sync batch. `waste_terms` has always had
        # a durable table and a real writer, but nothing stamped an
        # `(analysis, waste_terms)` batch — so its freshness entry matched no
        # sync_state row and the dataset reported "never run" forever while the
        # table filled up normally. A freshness row without a writer is not
        # evidence; this makes the writer real rather than deleting the row.
        waste_batch_id = db_writers.start_sync_batch(
            source="analysis", dataset="waste_terms", sync_type="weekly",
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
            log.error("[weekly] DB write after Step 3 failed: %s", db_exc)
            if waste_batch_id:
                db_writers.finish_sync_batch(
                    batch_id=waste_batch_id, status="failed",
                    error_message=str(db_exc)[:1000])

        # PR-ADS-153D: durable flag HISTORY. This runs AFTER the annotations
        # land, so the canonical flagged population it reads already reflects
        # this run's classification. History is derived from that population
        # rather than from waste_terms directly, because waste_terms has no
        # campaign_id — identities built from a raw campaign name would never
        # match the ones Search Terms and the Action Queue compute.
        #
        # Local database only. No Google Ads, HubSpot or Mailchimp call, and no
        # human review decision is ever modified by an observation.
        try:
            from services.search_term_flag_history_service import (  # noqa: PLC0415
                record_flag_history,
            )
            history = record_flag_history()
            if not history.get("available"):
                log.warning("[weekly] flag history not recorded: %s",
                            history.get("reason"))
            else:
                log.info("[weekly] recorded flag history for %d term(s)",
                         history.get("written", 0))
        except Exception as hist_exc:  # noqa: BLE001
            # Never fail the weekly run over an audit-trail write.
            log.error("[weekly] flag history step failed: %s", hist_exc)

        # Step 4: Lead quality analysis
        print("Step 4/6: Running lead quality analysis...")
        from analysis.core import run_lead_quality
        run_lead_quality()

        # Step 5: Campaign truth table
        print("Step 5/6: Building campaign truth table...")
        from analysis.core import run_campaign_truth
        campaign_truth = run_campaign_truth()

        # Write campaigns to database (with freshness tracking)
        try:
            if run_id is not None and campaign_truth:
                campaign_rows = campaign_truth.get("campaigns", [])
                campaigns_batch_id = db_writers.start_sync_batch(
                    source="google_ads_api",
                    dataset="campaigns",
                    sync_type="weekly",
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

                # PR-ADS-153F: the coverage snapshot gets its OWN sync batch under
                # the key its freshness config reads, `(gclid, coverage_snapshots)`.
                # The table and the writer have always existed; nothing ever
                # stamped that key, so the dataset reported "never run" forever
                # while snapshots accumulated normally.
                cov_batch_id = db_writers.start_sync_batch(
                    source="gclid", dataset="coverage_snapshots",
                    sync_type="weekly", date_from=window_start,
                    date_to=window_end, run_id=run_id,
                )
                # The snapshot is stamped with its OWN batch, and that batch is
                # finished with the REAL outcome. Attributing the row to the
                # attribution batch would break the linkage the ledger exists to
                # provide, and reporting success unconditionally would make this
                # dataset healthy-looking on a failed insert — the same "looks
                # monitored, reports nothing true" defect this PR removes.
                cov_written = db_writers.write_gclid_coverage_snapshot(
                    run_id=run_id,
                    coverage=coverage,
                    sync_batch_id=cov_batch_id or None,
                )
                if cov_batch_id:
                    db_writers.finish_sync_batch(
                        batch_id=cov_batch_id,
                        status="success" if cov_written else "failed",
                        row_count=cov_written,
                        last_source_date=window_end if cov_written else None,
                        error_message=(None if cov_written else
                                       "gclid coverage snapshot wrote 0 rows"),
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
        # PR-ADS-156-F1 §6: n-grams over the PERSISTED rows, or not at all.
        # `None` reaches the report as "unavailable"; an empty list would reach
        # it as "no wasteful n-grams this week", which is a different claim and
        # one this run has no evidence for.
        ngram_findings = (compute_ngram_findings(search_terms)
                          if search_terms_available else None)
        if not search_terms_available:
            log.warning("[weekly] n-gram findings omitted — the canonical "
                        "search-term sync did not persist rows this run")
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
