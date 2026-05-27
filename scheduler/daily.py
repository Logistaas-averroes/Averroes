"""
Daily Pulse Scheduler
Runs at 6am GMT every day via Render cron.
Fast path: anomaly detection, spend spikes, new junk terms, CRM delta.
"""

import logging
import os
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv
from scheduler.run_history import start_run, finish_run
from scheduler.sync_utils import max_source_date, persistence_succeeded
import db.writers as db_writers

load_dotenv()

log = logging.getLogger(__name__)

def run_daily_pulse():
    print(f"\n{'='*60}")
    print(f"LOGISTAAS DAILY PULSE — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}\n")

    run_record = start_run("daily")
    run_id = None

    try:
        # 1. Pull fresh data
        from connectors.windsor_pull import pull_campaign_performance
        from connectors.hubspot_pull import pull_paid_search_contacts, get_lead_quality_summary

        print("Step 1/5: Pulling Google Ads data (last 2 days)...")
        campaigns = pull_campaign_performance(days_back=2)

        print("Step 2/5: Pulling HubSpot contacts (last 2 days)...")
        contacts = pull_paid_search_contacts(days_back=2)
        crm_summary = get_lead_quality_summary(contacts)

        today = datetime.utcnow().date()

        # Write run record to database
        try:
            run_id = db_writers.write_run(run_record)
        except Exception as db_exc:  # noqa: BLE001
            log.error("[daily] DB write of run record failed: %s", db_exc)

        # Track HubSpot contacts daily sync
        if run_id is not None:
            contacts_batch_id = db_writers.start_sync_batch(
                source="hubspot",
                dataset="contacts",
                sync_type="daily",
                date_from=today - timedelta(days=2),
                date_to=today,
                run_id=run_id,
            )
            try:
                contacts_written = db_writers.write_leads(run_id, contacts)
                if not persistence_succeeded(contacts, contacts_written):
                    raise RuntimeError(
                        f"HubSpot contacts persistence failed or wrote 0 rows "
                        f"for {len(contacts or [])} fetched contacts"
                    )
                last_contact_date = max_source_date(contacts, fallback_date=today)
                if contacts_batch_id:
                    db_writers.finish_sync_batch(
                        batch_id=contacts_batch_id,
                        status="success",
                        row_count=contacts_written,
                        last_source_date=last_contact_date,
                    )
            except Exception as exc:  # noqa: BLE001
                if contacts_batch_id:
                    db_writers.finish_sync_batch(
                        batch_id=contacts_batch_id,
                        status="failed",
                        error_message=str(exc)[:1000],
                    )
                log.warning("[daily] HubSpot contacts sync failed: %s", exc)
        else:
            log.error("[daily] Skipping lead write and contacts sync tracking because write_run returned no run_id")

        # 2. Detect anomalies
        print("Step 3/5: Running anomaly detection...")
        anomalies = detect_anomalies(campaigns)
        label = "anomaly" if len(anomalies) == 1 else "anomalies"
        print(f"  → {len(anomalies)} {label} found.")

        # 3. Check for new junk terms (quick pattern match)
        print("Step 4/5: Checking for new junk search terms...")
        from connectors.windsor_pull import pull_search_terms
        search_terms = pull_search_terms(days_back=1)
        new_junk = detect_junk_terms(search_terms)
        label = "junk term" if len(new_junk) == 1 else "junk terms"
        print(f"  → {len(new_junk)} new {label} found.")

        # Track Windsor search_terms daily sync
        st_batch_id = db_writers.start_sync_batch(
            source="windsor",
            dataset="search_terms",
            sync_type="daily",
            date_from=today - timedelta(days=1),
            date_to=today,
            run_id=run_id,
        )
        try:
            st_count = db_writers.write_search_terms(
                run_id, search_terms, sync_batch_id=st_batch_id or None,
            )
            # PR-ADS-065: Distinguish empty pull from write failure
            fetched_count = len(search_terms or [])
            if fetched_count == 0:
                # Windsor returned nothing for daily 1-day window.
                if st_batch_id:
                    db_writers.finish_sync_batch(
                        batch_id=st_batch_id,
                        status="success",
                        row_count=0,
                        last_source_date=today,
                        error_message=(
                            "Windsor returned zero search-term rows for daily pull; "
                            "this may be normal for a 1-day window or may indicate issues."
                        ),
                    )
                log.info("[daily] Windsor search-term daily pull returned 0 rows (success)")
            elif not persistence_succeeded(search_terms, st_count):
                raise RuntimeError(
                    f"Windsor search_terms persistence failed: "
                    f"fetched {fetched_count} rows but wrote {st_count}"
                )
            else:
                log.info("[daily] Wrote %d search term rows to database", st_count)
                last_st_date = max_source_date(search_terms, fallback_date=today)
                if st_batch_id:
                    db_writers.finish_sync_batch(
                        batch_id=st_batch_id,
                        status="success",
                        row_count=st_count,
                        last_source_date=last_st_date,
                    )
        except Exception as exc:  # noqa: BLE001
            if st_batch_id:
                db_writers.finish_sync_batch(
                    batch_id=st_batch_id,
                    status="failed",
                    error_message=str(exc)[:1000],
                )
            log.warning("[daily] Daily search_terms sync failed: %s", exc)

        # Do not update sync_state for datasets that are pulled but not persisted
        # as durable source facts. Freshness means "stored locally and queryable",
        # not merely "fetched from an external API".
        # windsor/campaigns — campaign data is analysis output only, not raw Windsor facts
        # windsor/keywords  — not pulled in daily
        # windsor/geo       — not pulled in daily
        # hubspot/deals     — not pulled in daily
        # gclid/matches     — no DB persistence path yet

        # 4a. Generate and persist daily ROAS snapshot (PR-ADS-080C)
        print("Step 5/6: Generating daily ROAS snapshot...")
        try:
            from services.roas_snapshot_service import generate_roas_snapshot, save_roas_snapshot

            roas_snapshot = generate_roas_snapshot(window="60d")
            save_result = save_roas_snapshot(roas_snapshot)
            if save_result.get("status") == "success":
                print(f"  → ROAS snapshot saved: {save_result.get('snapshot_path')}")
            else:
                log.warning("[daily] ROAS snapshot save failed: %s", save_result.get("error"))
                print(f"  → ROAS snapshot save failed: {save_result.get('error')}")
        except Exception as roas_exc:  # noqa: BLE001
            log.warning("[daily] ROAS snapshot generation failed (non-fatal): %s", roas_exc)
            print(f"  → ROAS snapshot generation failed (non-fatal): {roas_exc}")

        # 4b. CRM delta check + budget pacing
        print("Step 6/6: Checking CRM delta and budget pacing...")
        crm_delta = check_crm_delta(campaigns, crm_summary)
        print(f"  → CRM delta alert: {crm_delta.get('alert', False)} ({crm_delta.get('message', '')})")

        budget_pacing = check_budget_pacing(campaigns)

        # Build result from collected signals
        has_issues = bool(anomalies) or bool(new_junk) or crm_delta.get("alert", False)
        result = {
            "status": "flagged" if has_issues else "clean",
            "date": datetime.utcnow().strftime("%Y-%m-%d"),
            "anomalies": anomalies,
            "new_junk_terms": new_junk,
            "crm_delta": crm_delta,
            "budget_pacing": budget_pacing,
        }

        # 5. Save and deliver
        save_daily_report(result)
        deliver_report(result)

        print(f"\nDaily pulse complete. Status: {result.get('status', 'unknown')}")

        finish_run(
            run_record,
            status="success",
            report_path=f"outputs/daily_{datetime.utcnow().strftime('%Y-%m-%d')}.json",
            delivery_attempted=result.get("status") != "clean",
            delivery_success=None,  # daily deliver_report has no return value yet (Phase 3)
        )
        try:
            db_writers.update_run(run_id, run_record)
        except Exception as db_exc:  # noqa: BLE001
            log.error("[daily] update_run failed: %s", db_exc)
        return result

    except Exception as exc:
        finish_run(
            run_record,
            status="failed",
            failed_step=getattr(exc, "_step", None),
            error_message=str(exc),
        )
        try:
            db_writers.update_run(run_id, run_record)
        except Exception as db_exc:  # noqa: BLE001
            log.error("[daily] update_run (failed run) failed: %s", db_exc)
        raise


def detect_anomalies(campaigns: list) -> list:
    """Detect spend spikes vs 7-day average.

    Requires historical data from connectors to compute baselines.
    Returns an empty list if no historical data is available.
    """
    anomalies = []

    # Load 7-day historical from connector output
    hist_path = "data/ads_campaigns.json"
    if not os.path.exists(hist_path):
        return anomalies

    with open(hist_path) as f:
        historical = json.load(f)

    # Build 7-day avg by campaign
    hist_by_campaign = {}
    for row in historical:
        name = row.get("campaign", "unknown")
        spend = float(row.get("spend", 0) or 0)
        hist_by_campaign[name] = hist_by_campaign.get(name, [])
        hist_by_campaign[name].append(spend)

    avg_by_campaign = {
        k: sum(v) / len(v)
        for k, v in hist_by_campaign.items()
        if v
    }

    # Check current vs average
    for row in campaigns:
        name = row.get("campaign", "unknown")
        current = float(row.get("spend", 0) or 0)
        avg = avg_by_campaign.get(name, 0)

        if avg > 0 and current > avg * 1.3:
            anomalies.append({
                "type": "spend_spike",
                "campaign": name,
                "current_spend": current,
                "avg_spend": round(avg, 2),
                "pct_above_avg": round((current / avg - 1) * 100, 1)
            })

    return anomalies


def detect_junk_terms(search_terms: list) -> list:
    """Quick pattern match for new junk search terms."""
    import yaml

    with open("config/junk_patterns.yaml") as f:
        patterns = yaml.safe_load(f)

    all_junk_words = []
    for category in patterns.get("patterns", {}).values():
        all_junk_words.extend(category.get("terms", []))

    junk_found = []
    for term_row in search_terms:
        term = (term_row.get("search_term") or "").lower()
        for junk_word in all_junk_words:
            if junk_word.lower() in term:
                junk_found.append({
                    "search_term": term,
                    "matched_pattern": junk_word,
                    "spend": term_row.get("spend", 0),
                    "campaign": term_row.get("campaign"),
                })
                break

    return junk_found


def check_crm_delta(campaigns: list, crm_summary: dict) -> dict:
    """Compare Ads conversions vs HubSpot contacts created."""
    ads_conversions = sum(float(c.get("conversions", 0) or 0) for c in campaigns)
    hs_contacts = crm_summary.get("total", 0)

    delta_pct = 0
    if ads_conversions > 0:
        delta_pct = round((abs(ads_conversions - hs_contacts) / ads_conversions) * 100, 1)

    return {
        "ads_conversions": ads_conversions,
        "hubspot_contacts": hs_contacts,
        "delta_pct": delta_pct,
        "alert": delta_pct > 20,
        "message": f"Ads shows {ads_conversions} conversions, HubSpot shows {hs_contacts} contacts ({delta_pct}% delta)"
    }


def check_budget_pacing(campaigns: list) -> list:
    """Flag campaigns on track to exhaust budget early."""
    # Simplified pacing check — full version in weekly
    return []


def save_daily_report(result: dict):
    os.makedirs("outputs", exist_ok=True)
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    path = f"outputs/daily_{date_str}.json"
    with open(path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"Report saved: {path}")


def deliver_report(result: dict):
    """Send report via email/Slack if alerts exist."""
    if result.get("status") == "clean":
        print("No issues found — no alert sent.")
        return

    report_text = result.get("report", result.get("message", ""))
    print(f"\n{'─'*40}")
    print("DAILY PULSE ALERT")
    print('─'*40)
    print(report_text)

    # TODO: Add email/Slack delivery here in Phase 3


if __name__ == "__main__":
    run_daily_pulse()
