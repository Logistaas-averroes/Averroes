"""
Weekly data refresh
Runs every Monday at 07:00 Asia/Amman (04:00 UTC) from the in-app scheduler.
Orchestrates: google_ads_source → hubspot_pull → durable writes → waste
detection → flag history → GCLID match.
No business logic lives here. This module only sequences the steps.

PR-ADS-162H: this job no longer produces a report. It used to end by building
the advisor report (lead quality → campaign truth → n-grams → markdown) and
emailing it; the Reports page that displayed it was deleted in PR-ADS-162G and
nobody received the email. What is left is every step a live page still reads:
the legacy leads / deals / geo / keywords snapshots, durable keyword facts, the
canonical search-term sync, the waste annotations and their flag history, and
the GCLID match with its coverage snapshot.

The function keeps its name (`run_weekly_report`) because the scheduler, the
manual run endpoint and the `weekly` run type are all keyed on it. It returns
the run outcome, not a report path: `success` when every step landed,
`partial` when the run finished and at least one step did not. A run that
cannot continue still raises, and is recorded `failed`.
"""

import logging
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
from scheduler.run_history import start_run, finish_run
from scheduler.sync_utils import (
    max_source_date,
    note_step_failure,
    persistence_succeeded,
    run_outcome,
)
import db.writers as db_writers

log = logging.getLogger(__name__)

load_dotenv()

def run_weekly_report():
    print(f"\n{'='*60}")
    print(f"LOGISTAAS WEEKLY DATA REFRESH — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}\n")

    run_record = start_run("weekly")
    run_id = None
    # PR-ADS-162H: every kept step that does not land is recorded here, and the
    # run ends `partial` if this is not empty. Before, a write that persisted
    # nothing was logged and the run still ended `success`.
    step_failures: list = []

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
            "Step 1/4: Pulling Google Ads data via direct Google Ads API "
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
        print("Step 2/4: Pulling HubSpot CRM data (30 days)...")
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
                if not persistence_succeeded(contacts, contacts_written):
                    note_step_failure(
                        step_failures, "write_leads",
                        f"wrote {contacts_written} of {len(contacts or [])} fetched contacts")

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
                if not persistence_succeeded(deals, deals_written):
                    note_step_failure(
                        step_failures, "write_deals",
                        f"wrote {deals_written} of {len(deals or [])} fetched deals")
            else:
                log.error("[weekly] DB write after Step 2: write_run returned no run_id")
                # No run row means no child row can be written: leads, deals,
                # geo and keywords are all skipped below. Said once, here.
                note_step_failure(
                    step_failures, "write_run",
                    "no run row was created — the leads, deals, geo and keyword "
                    "snapshots were not written and keyword facts were not synced")
        except Exception as db_exc:  # noqa: BLE001
            log.error("[weekly] DB write after Step 2 failed: %s", db_exc)
            run_id = None
            note_step_failure(step_failures, "write_leads / write_deals", db_exc)

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
                if not persistence_succeeded(geos, geo_count):
                    note_step_failure(
                        step_failures, "write_geo",
                        f"wrote {geo_count} of {len(geos or [])} fetched rows")
        except Exception as db_exc:  # noqa: BLE001
            log.error("[weekly] DB write geo failed: %s", db_exc)
            note_step_failure(step_failures, "write_geo", db_exc)

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
                if not persistence_succeeded(keywords, kw_count):
                    note_step_failure(
                        step_failures, "write_keywords",
                        f"wrote {kw_count} of {len(keywords or [])} fetched rows")

                # PR-ADS-146A: durable keyword facts via the ONE shared sync path
                # (the same sync_keyword_daily_facts used by daily / weekly /
                # monthly, the admin refresh action and the bootstrap — no
                # competing keyword-fact persistence implementations). It creates +
                # finishes its own keyword_facts sync batch and updates sync_state.
                from services.keyword_sync_service import sync_keyword_daily_facts  # noqa: PLC0415
                kdf = sync_keyword_daily_facts(
                    run_date - timedelta(days=29), run_date, "weekly", run_id=run_id)
                log.info("[weekly] Durable keyword facts: %s", kdf)
                if not (kdf or {}).get("ok"):
                    note_step_failure(
                        step_failures, "sync_keyword_daily_facts",
                        (kdf or {}).get("error") or "the sync did not report ok")
        except Exception as db_exc:  # noqa: BLE001
            log.error("[weekly] DB write keywords failed: %s", db_exc)
            note_step_failure(
                step_failures, "write_keywords / sync_keyword_daily_facts", db_exc)

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
        # JSON snapshot the GCLID match reads, and waste detection) then
        # describes rows that are genuinely in the database. Analysis over
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
                note_step_failure(
                    step_failures, "sync_recent_search_terms",
                    st.get("error") or "the sync did not report ok")
        except Exception as db_exc:  # noqa: BLE001
            log.error("[weekly] DB write search terms failed: %s", db_exc)
            note_step_failure(step_failures, "sync_recent_search_terms", db_exc)

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
        #
        # PR-ADS-156-F2 §1: the canonical rows are PASSED IN. This step used to
        # call `run_waste_detection()` with no arguments, and that function
        # reloaded data/ads_search_terms.json itself — so on a failed sync it
        # analysed the snapshot F1 had deliberately preserved and published the
        # findings under this run's timestamp. Protecting the file from being
        # destroyed and then reusing it as current evidence is the same untruth
        # from the other side.
        print("Step 3/4: Running waste detection...")
        from analysis.core import run_waste_detection
        waste_output = run_waste_detection(
            search_terms,
            search_term_evidence_available=search_terms_available)
        if not search_terms_available:
            print("  → search-term waste analysis unavailable (canonical sync failed)")

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
            if not search_terms_available:
                # PR-ADS-156-F2 §1: no successful current batch from evidence
                # that does not exist. A `success` here would advance the
                # waste_terms watermark over an interval nobody measured, and
                # the freshness page would show the dataset as current.
                if waste_batch_id:
                    db_writers.finish_sync_batch(
                        batch_id=waste_batch_id, status="failed",
                        error_message="canonical search-term evidence unavailable "
                                      "— no waste analysis was performed for this "
                                      "interval")
                note_step_failure(
                    step_failures, "run_waste_detection / write_waste_terms",
                    "canonical search-term evidence unavailable — no waste "
                    "analysis was performed for this interval")
            else:
                if run_id is not None and waste_output:
                    db_writers.write_waste_terms(run_id, waste_output.get("confirmed_waste_items", []))
                if waste_batch_id:
                    db_writers.finish_sync_batch(
                        batch_id=waste_batch_id, status="success",
                        row_count=len((waste_output or {}).get("confirmed_waste_items", [])))
        except Exception as db_exc:  # noqa: BLE001
            log.error("[weekly] DB write after Step 3 failed: %s", db_exc)
            note_step_failure(step_failures, "write_waste_terms", db_exc)
            if waste_batch_id:
                db_writers.finish_sync_batch(
                    batch_id=waste_batch_id, status="failed",
                    error_message=str(db_exc)[:1000])

        # PR-ADS-153D: durable flag HISTORY. This runs AFTER the annotations
        # land, so the canonical flagged population it reads already reflects
        # this run's classification. History is derived from that population
        # rather than from waste_terms directly, because waste_terms has no
        # campaign_id — identities built from a raw campaign name would never
        # match the ones the Search Terms page computes.
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
                note_step_failure(
                    step_failures, "record_flag_history",
                    history.get("reason") or "flag history unavailable")
            else:
                log.info("[weekly] recorded flag history for %d term(s)",
                         history.get("written", 0))
        except Exception as hist_exc:  # noqa: BLE001
            # Never FAIL the weekly run over an audit-trail write — but a run
            # that did not record it did not finish, and says so (`partial`).
            log.error("[weekly] flag history step failed: %s", hist_exc)
            note_step_failure(step_failures, "record_flag_history", hist_exc)

        # Step 4: GCLID match + DB persistence
        #
        # PR-ADS-162H: the lead-quality and campaign-truth steps that stood here
        # are gone, with the `campaigns` snapshot write they fed. They built
        # `outputs/lead_quality.json` and `outputs/campaign_truth.json` for the
        # report and nothing else read either file.
        print("Step 4/4: Running GCLID match and persisting attribution...")
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
                if not cov_written:
                    note_step_failure(
                        step_failures, "write_gclid_coverage_snapshot",
                        "gclid coverage snapshot wrote 0 rows")

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
                note_step_failure(
                    step_failures, "run_gclid_match / write_gclid_attribution",
                    gclid_exc)
        except Exception as gclid_import_exc:  # noqa: BLE001
            log.warning("[weekly] GCLID match step failed: %s", gclid_import_exc)
            note_step_failure(step_failures, "run_gclid_match", gclid_import_exc)

        # PR-ADS-162H: no report is generated and nothing is emailed. The run
        # record keeps its three report columns (no schema change) and writes
        # them as "nothing was produced, nothing was sent": report_path NULL,
        # delivery_attempted FALSE, delivery_success NULL.
        status, failed_step, error_message = run_outcome(step_failures)

        print(f"\n{'='*60}")
        print(f"Weekly data refresh {status} — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
        for failure in step_failures:
            print(f"  did not land: {failure['step']} — {failure['detail']}")
        print(f"{'='*60}\n")

        finish_run(
            run_record,
            status=status,
            report_path=None,
            delivery_attempted=False,
            delivery_success=None,
            failed_step=failed_step,
            error_message=error_message,
        )
        try:
            db_writers.update_run(run_id, run_record)
        except Exception as db_exc:  # noqa: BLE001
            log.error("[weekly] update_run failed: %s", db_exc)
        return {
            "run_type": "weekly",
            "status": status,
            "failed_steps": [f["step"] for f in step_failures],
        }

    except Exception as exc:
        finish_run(
            run_record,
            status="failed",
            report_path=None,
            delivery_attempted=False,
            delivery_success=None,
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
