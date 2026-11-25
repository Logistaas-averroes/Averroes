"""
Monthly data refresh
Runs on the 1st of each month at 08:00 Asia/Amman (05:00 UTC) from the in-app
scheduler.
Orchestrates: google_ads_source → hubspot_pull → durable writes → waste
detection → flag history → GCLID match.
No business logic lives here. This module only sequences the steps.

PR-ADS-165A: the weekly job is retired, and this is now the ONLY scheduled
writer of the legacy geo snapshot, the waste annotations, the
search-term flag history and the GCLID coverage snapshot. (PR-ADS-177C: it no
longer writes the legacy `keywords` snapshot, which nothing reads.) Those datasets are
therefore at most one month old by design (see
`services.freshness_service.MONTHLY_REFRESH_STALE_DAYS`). The flag-history
step moved here from the weekly job, which was its only caller.

PR-ADS-162H: this job no longer produces a report. It used to end by building
the advisor report (lead quality → campaign truth → n-grams → markdown) and
emailing it; the Reports page that displayed it was deleted in PR-ADS-162G and
the email had failed to send every month. What is left is every step a live
page still reads: the legacy leads / deals / geo snapshots, durable
keyword facts, the canonical search-term sync, the waste annotations, and the
GCLID match with its coverage snapshot.

The function keeps its name (`run_monthly_report`) because the scheduler, the
manual run endpoint and the `monthly` run type are all keyed on it. It returns
the run outcome, not a report path: `success` when every step landed,
`partial` when the run finished and at least one step did not, `failed` when a
step it cannot continue past failed. It does not raise for a failed step — the
returned status is the only place a caller learns of one, so callers must read
it rather than treat "no exception" as success.
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

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Files the connector pulls must have left on disk before the analysis steps
# run. Unchanged by PR-ADS-162H: `ads_campaigns.json` is no longer read by this
# job (campaign truth is gone) but is still the daily pulse's anomaly baseline,
# and the other two are inputs of the GCLID match and waste detection below.
REQUIRED_DATA_FILES = [
    "data/ads_campaigns.json",
    "data/ads_search_terms.json",
    "data/crm_contacts.json",
]


def _failed(run_record: dict, run_id, failed_step: str, error_message: str) -> dict:
    """Record a run that could not continue, and return its outcome.

    PR-ADS-162H: the six copies of this block each ended `return None`, which a
    caller could tell from success only because success returned a report path.
    With no report there is nothing left to tell them apart, so the outcome is
    returned explicitly."""
    finish_run(
        run_record,
        status="failed",
        report_path=None,
        delivery_attempted=False,
        delivery_success=None,
        failed_step=failed_step,
        error_message=error_message,
    )
    if run_id is not None:
        try:
            db_writers.update_run(run_id, run_record)
        except Exception as db_exc:  # noqa: BLE001
            log.error("update_run failed: %s", db_exc)
    return {
        "run_type": "monthly",
        "status": "failed",
        "failed_steps": [failed_step],
        "error": error_message,
    }


def run_monthly_report():
    log.info("=" * 60)
    log.info(f"LOGISTAAS MONTHLY DATA REFRESH — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    log.info("=" * 60)

    run_record = start_run("monthly")
    run_id = None
    # PR-ADS-162H: every kept step that does not land is recorded here, and the
    # run ends `partial` if this is not empty. Before, a write that persisted
    # nothing was logged and the run still ended `success`.
    step_failures: list = []

    # Step 1: Pull Google Ads data (30-day window)
    #
    # PR-ADS-156-F1 §6: search terms are NOT pulled here. This step used to call
    # `pull_search_terms(days_back=30)` and the canonical service then pulled the
    # same 30 days again later in the run — two Google Ads queries for one
    # dataset, and two answers nothing reconciled. The analysis further down now
    # reads the rows the canonical sync persisted, so the snapshot and the
    # database describe the same observation by construction.
    log.info("Step 1/4 START: Pulling Google Ads data via direct Google Ads API (30 days)...")
    search_terms = []
    search_terms_available = False
    try:
        from connectors.google_ads_source import (
            pull_campaign_performance,
            pull_keyword_performance,
            pull_geo_performance,
            save_output as google_ads_save,
        )
        campaigns = pull_campaign_performance(days_back=30)
        keywords = pull_keyword_performance(days_back=30)
        geos = pull_geo_performance(days_back=30)
        # `None` leaves data/ads_search_terms.json alone until the canonical
        # sync below has actually persisted this month's rows.
        google_ads_save(campaigns, None, keywords, geos)
        log.info(
            f"Step 1/4 END: Google Ads API pull complete — "
            f"{len(campaigns)} campaign rows"
        )
    except Exception as e:
        log.error(f"Step 1/4 FAILED: Google Ads API pull error — {e}")
        return _failed(run_record, run_id, "Step 1/4: Google Ads API pull", str(e))

    # Step 2: Pull HubSpot CRM data (30-day window)
    log.info("Step 2/4 START: Pulling HubSpot CRM data (30 days)...")
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
            f"Step 2/4 END: HubSpot pull complete — "
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
                if not persistence_succeeded(contacts, contacts_written):
                    note_step_failure(
                        step_failures, "write_leads",
                        f"wrote {contacts_written} of {len(contacts or [])} fetched contacts")

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
                if not persistence_succeeded(deals, deals_written):
                    note_step_failure(
                        step_failures, "write_deals",
                        f"wrote {deals_written} of {len(deals or [])} fetched deals")
            else:
                log.error("DB write after Step 2: write_run returned no run_id; skipping child writes")
                # No run row means no child row can be written: leads, deals,
                # geo and keyword facts are all skipped below. Said once, here.
                note_step_failure(
                    step_failures, "write_run",
                    "no run row was created — the leads, deals and geo snapshots "
                    "were not written and keyword facts were not synced")
        except Exception as db_exc:  # noqa: BLE001
            log.error("DB write after Step 2 failed: %s", db_exc)
            run_id = None
            note_step_failure(step_failures, "write_leads / write_deals", db_exc)

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
                if not persistence_succeeded(geos, geo_count):
                    note_step_failure(
                        step_failures, "write_geo",
                        f"wrote {geo_count} of {len(geos or [])} fetched rows")
        except Exception as db_exc:  # noqa: BLE001
            log.error("DB write geo failed: %s", db_exc)
            note_step_failure(step_failures, "write_geo", db_exc)

        # PR-ADS-177C: the legacy `keywords` snapshot is no longer written. Its
        # last in-product reader (Dashboard → Campaigns' keyword themes) went in
        # PR-ADS-177B and the orphaned /api/keywords endpoint in PR-ADS-177C, so
        # the write fed nothing. Only our own table is affected: this was a
        # write to the local database, never to Google Ads or HubSpot. The
        # keyword pull above is unchanged (it is a read). Durable keyword facts
        # (the Keywords evidence) keep their one shared sync path below.
        try:
            if run_id is not None:
                # PR-ADS-146A: durable keyword facts via the ONE shared sync path.
                from services.keyword_sync_service import sync_keyword_daily_facts  # noqa: PLC0415
                kdf = sync_keyword_daily_facts(
                    run_date - timedelta(days=29), run_date, "monthly", run_id=run_id)
                log.info("Durable keyword facts (run_id=%s): %s", run_id, kdf)
                if not (kdf or {}).get("ok"):
                    note_step_failure(
                        step_failures, "sync_keyword_daily_facts",
                        (kdf or {}).get("error") or "the sync did not report ok")
        except Exception as db_exc:  # noqa: BLE001
            log.error("Durable keyword facts sync failed: %s", db_exc)
            note_step_failure(step_failures, "sync_keyword_daily_facts", db_exc)

        # Write search term rows to database (non-fatal)
        #
        # PR-ADS-156 §5: persistence goes through the ONE shared canonical
        # search-term service — the third inline copy of pull → batch → write →
        # judge → finish is gone. The 30-day monthly recovery window is
        # preserved; only the rules are now shared.
        #
        # PR-ADS-156-F1 §6: and this is the ONLY search-term pull in the monthly
        # run. `include_rows=True` returns what was persisted; the rows are
        # adopted only when the sync reports ok, so the snapshot and the waste
        # analysis never describe rows the database does not hold.
        try:
            from services.search_term_sync_service import (  # noqa: PLC0415
                sync_recent_search_terms,
            )
            st = sync_recent_search_terms("monthly", days=30, run_id=run_id,
                                          include_rows=True)
            if st.get("ok"):
                search_terms = st.get("rows") or []
                search_terms_available = True
                log.info("Canonical search-term sync (run_id=%s): %s", run_id, {
                    k: st.get(k) for k in
                    ("date_from", "date_to", "fetched", "written", "verified_empty")})
                google_ads_save(None, search_terms, None, None)
            else:
                log.error(
                    "Canonical search-term sync failed (run_id=%s): %s — "
                    "search-term analysis is unavailable for this run and the "
                    "previous snapshot is left in place",
                    run_id, st.get("error"))
                note_step_failure(
                    step_failures, "sync_recent_search_terms",
                    st.get("error") or "the sync did not report ok")
        except Exception as db_exc:  # noqa: BLE001
            log.error("DB write search terms failed: %s", db_exc)
            note_step_failure(step_failures, "sync_recent_search_terms", db_exc)

    except Exception as e:
        log.error(f"Step 2/4 FAILED: HubSpot pull error — {e}")
        return _failed(run_record, run_id, "Step 2/4: HubSpot pull", str(e))

    # Validate required data files exist before running analysis
    missing = [f for f in REQUIRED_DATA_FILES if not os.path.exists(f)]
    if missing:
        for f in missing:
            log.error(f"Required data file missing after connector pull: {f}")
        log.error("Aborting monthly run — required data files not found")
        return _failed(run_record, run_id, "pre-analysis data validation",
                       f"Missing files: {', '.join(missing)}")

    # Step 3: Waste detection
    log.info("Step 3/4 START: Running waste detection...")
    try:
        # PR-ADS-156-F2 §1: the canonical rows are PASSED IN. This step used to
        # call `run_waste_detection()` with no arguments, and that function
        # reloaded data/ads_search_terms.json itself — so on a failed sync it
        # analysed the snapshot F1 had deliberately preserved and published the
        # findings under this run's timestamp.
        from analysis.core import run_waste_detection
        waste_output = run_waste_detection(
            search_terms,
            search_term_evidence_available=search_terms_available)
        if search_terms_available:
            log.info("Step 3/4 END: Waste detection complete")
        else:
            log.warning("Step 3/4 END: search-term waste analysis unavailable — "
                        "the canonical sync did not persist rows this run")

        # Write waste terms to database.
        # PR-ADS-153F: wrapped in a real `(analysis, waste_terms)` sync batch.
        # `waste_terms` has always had a durable table and a real writer, but
        # nothing stamped an `(analysis, waste_terms)` batch — so its freshness
        # entry matched no sync_state row and the dataset reported "never run"
        # forever while the table filled up normally. A freshness row without
        # a writer is not evidence; this makes the writer real.
        waste_batch_id = db_writers.start_sync_batch(
            source="analysis", dataset="waste_terms", sync_type="monthly",
            run_id=run_id,
        )
        try:
            if not search_terms_available:
                # PR-ADS-156-F2 §1: no successful current batch from evidence
                # that does not exist — a `success` here would advance the
                # waste_terms watermark over an interval nobody measured.
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
            log.error("DB write after Step 3 failed: %s", db_exc)
            note_step_failure(step_failures, "write_waste_terms", db_exc)
            if waste_batch_id:
                db_writers.finish_sync_batch(
                    batch_id=waste_batch_id, status="failed",
                    error_message=str(db_exc)[:1000])

    except Exception as e:
        log.error(f"Step 3/4 FAILED: Waste detection error — {e}")
        return _failed(run_record, run_id, "Step 3/4: Waste detection", str(e))

    # PR-ADS-153D: durable flag HISTORY. PR-ADS-165A moved this step here from
    # the retired weekly job, which was its only scheduled caller — without it
    # `search_term_review.first_flagged_at` / `latest_flagged_at` would never
    # be written again and Search Terms → Flagged would show "first flagged: —"
    # for every term the step above flags.
    #
    # It runs AFTER the annotations land, so the canonical flagged population
    # it reads already reflects this run's classification. History is derived
    # from that population rather than from waste_terms directly, because
    # waste_terms has no campaign_id — identities built from a raw campaign
    # name would never match the ones the Search Terms page computes.
    #
    # Local database only, read 200 rows a page: no Google Ads, HubSpot or
    # Mailchimp call, and no human review decision is ever modified by an
    # observation. Each observation is dated by the term's last SOURCE date,
    # not by this run's clock, so replaying it over unchanged evidence writes
    # nothing new.
    try:
        from services.search_term_flag_history_service import (  # noqa: PLC0415
            record_flag_history,
        )
        history = record_flag_history()
        if not history.get("available"):
            log.warning("flag history not recorded: %s", history.get("reason"))
            note_step_failure(
                step_failures, "record_flag_history",
                history.get("reason") or "flag history unavailable")
        else:
            log.info("recorded flag history for %d term(s)",
                     history.get("written", 0))
    except Exception as hist_exc:  # noqa: BLE001
        # Never FAIL the run over an audit-trail write — but a run that did
        # not record it did not finish, and says so (`partial`).
        log.error("flag history step failed: %s", hist_exc)
        note_step_failure(step_failures, "record_flag_history", hist_exc)

    # Step 4: GCLID match + DB persistence
    #
    # PR-ADS-162H: the lead-quality and campaign-truth steps that stood here are
    # gone, with the `campaigns` snapshot write they fed. They built
    # `outputs/lead_quality.json` and `outputs/campaign_truth.json` for the
    # report and nothing else read either file.
    log.info("Step 4/4 START: Running GCLID match and persisting attribution...")
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
            log.info("Step 4/4 END: GCLID attribution: %d rows persisted", row_count)
        except Exception as gclid_exc:  # noqa: BLE001
            if gclid_batch_id:
                db_writers.finish_sync_batch(
                    batch_id=gclid_batch_id,
                    status="failed",
                    error_message=str(gclid_exc)[:1000],
                )
            log.warning("Step 4/4 WARN: GCLID attribution persistence failed: %s", gclid_exc)
            note_step_failure(
                step_failures, "run_gclid_match / write_gclid_attribution",
                gclid_exc)
    except Exception as gclid_import_exc:  # noqa: BLE001
        log.warning("Step 4/4 WARN: GCLID match step failed: %s", gclid_import_exc)
        note_step_failure(step_failures, "run_gclid_match", gclid_import_exc)

    # PR-ADS-162H: no report is generated and nothing is emailed. The run record
    # keeps its three report columns (no schema change) and writes them as
    # "nothing was produced, nothing was sent": report_path NULL,
    # delivery_attempted FALSE, delivery_success NULL.
    #
    # The run used to be `failed` when the advisor returned no report path or
    # the file was missing. Neither check exists now: a missing report cannot
    # fail a run that does not make one.
    status, failed_step, error_message = run_outcome(step_failures)

    log.info("=" * 60)
    log.info(f"Monthly data refresh {status} — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    for failure in step_failures:
        log.warning("  did not land: %s — %s", failure["step"], failure["detail"])
    log.info("=" * 60)

    finish_run(
        run_record,
        status=status,
        report_path=None,
        delivery_attempted=False,
        delivery_success=None,
        failed_step=failed_step,
        error_message=error_message,
    )
    if run_id is not None:
        try:
            db_writers.update_run(run_id, run_record)
        except Exception as db_exc:  # noqa: BLE001
            log.error("update_run failed: %s", db_exc)
    return {
        "run_type": "monthly",
        "status": status,
        "failed_steps": [f["step"] for f in step_failures],
    }


if __name__ == "__main__":
    run_monthly_report()
