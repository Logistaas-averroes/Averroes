"""
scheduler/incremental_sync.py

PR-ADS-073 — Daily Incremental Sync at 9 AM Asia/Amman

Responsibility:
  Run a rolling-window incremental sync for all major datasets.

  This is NOT the daily pulse (scheduler/daily.py) which runs anomaly
  detection and CRM delta checks.  This module's sole job is to pull
  fresh data from the Google Ads API and HubSpot and persist it to the
  local DB so that dashboard pages stay up to date.

PR-ADS-154 (production hotfix):
  - The pool is initialized and PROBED before any external connector is
    touched. A standalone `python -m scheduler.incremental_sync` process
    starts with `db.connection._pool = None`, so every persistence call
    received `conn is None` and the run pulled real data from Google Ads
    and HubSpot only to discard all of it — while reporting `partial` and
    exiting 0.
  - Windsor is gone from active orchestration. Production no longer uses
    it; the Google Ads API is the only platform-evidence source.
  - The CLI's exit code agrees with the reported status.

Doctrine:
  - Read-only from external platforms (Google Ads, HubSpot, FX providers).
  - Writes only to the local database.
  - Never modifies Google Ads campaigns, bids, budgets, keywords, or
    negative keywords.
  - Never modifies HubSpot contacts or deals.
  - Never triggers OCT uploads or external ad-platform mutations.

Data flow:
  External platforms → read-only pull → local DB → freshness metadata

Public entry point:
  run_daily_incremental_sync(
      *,
      lookback_days_ads: int = 14,
      lookback_days_hubspot_contacts: int = 14,
      lookback_days_hubspot_deals: int = 30,
      run_reason: str = "scheduled",
  ) -> dict
"""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db.writers as db_writers
from scheduler.sync_utils import max_source_date, persistence_succeeded
# PR-ADS-154 §3/§4: every (source, dataset) pair this scheduler stamps comes
# from the ONE registry. Spelling a key here as well is what let the writer and
# the freshness config drift apart until neither matched the other.
from services.dataset_keys import (
    CANONICAL_GEO_DATASET, CANONICAL_GEO_SOURCE,
    CANONICAL_SPEND_DATASET, CANONICAL_SPEND_SOURCE,
    DEAL_LEDGER_DATASET, DEAL_LEDGER_SOURCE,
    FX_DAILY_RATES_DATASET, FX_SOURCE,
    GCLID_MATCHES_DATASET, GCLID_SOURCE,
    SOURCE_CLASSIFICATION_DATASET, SOURCE_CLASSIFICATION_SOURCE,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config loading — reads sync.daily_incremental from config/thresholds.yaml.
# Falls back to hard-coded defaults if config is unavailable at import time.
# ---------------------------------------------------------------------------

_CONFIG_FALLBACK_ADS = 14
_CONFIG_FALLBACK_HUBSPOT_CONTACTS = 14
_CONFIG_FALLBACK_HUBSPOT_DEALS = 30


def _load_sync_config() -> dict:
    """Load sync.daily_incremental from config/thresholds.yaml.

    Returns the lookback_days sub-dict.  Falls back to hard-coded defaults
    if the config file is unavailable or the key is missing, so the module
    always has safe values regardless of deployment state.
    """
    try:
        import yaml  # noqa: PLC0415  # local import — yaml is optional at module load
        config_path = Path(__file__).resolve().parents[1] / "config" / "thresholds.yaml"
        with config_path.open() as f:
            cfg = yaml.safe_load(f)
        return cfg.get("sync", {}).get("daily_incremental", {}).get("lookback_days", {})
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "[incremental_sync] config load failed — using hard-coded lookback defaults: %s", exc
        )
        return {}


_SYNC_CONFIG = _load_sync_config()

DEFAULT_LOOKBACK_ADS = int(_SYNC_CONFIG.get("ads", _CONFIG_FALLBACK_ADS))
DEFAULT_LOOKBACK_HUBSPOT_CONTACTS = int(
    _SYNC_CONFIG.get("hubspot_contacts", _CONFIG_FALLBACK_HUBSPOT_CONTACTS)
)
DEFAULT_LOOKBACK_HUBSPOT_DEALS = int(
    _SYNC_CONFIG.get("hubspot_deals", _CONFIG_FALLBACK_HUBSPOT_DEALS)
)

# gclid/matches: no DB persistence path exists yet for incremental sync.
_GCLID_NOTE = (
    "unsupported_by_current_connector: no incremental-sync DB persistence path "
    "for gclid/matches. Dataset skipped."
)

# ── Retired datasets (PR-ADS-154) ───────────────────────────────────────────
# Production no longer uses Windsor.ai; the Google Ads API through the
# configured credentials is the only platform-evidence source. These four
# datasets were still being called on every run, spending time and credentials
# on a platform whose output nothing consumes.
#
# They are recorded EXPLICITLY rather than deleted from the summary. A dataset
# that simply disappears from the report is indistinguishable from one that was
# forgotten, and the next person to audit the run would have to diff two
# versions of this file to find out which. Each entry names the canonical
# replacement, or says plainly that there is none.
#
# `status: "retired"` is deliberately NOT `skipped` and NOT `success`: it never
# runs again, so it must never contribute to freshness or to the overall
# verdict, and it must never look like work that succeeded.
RETIRED_DATASETS: dict[str, dict] = {
    "windsor/campaigns": {
        "status": "retired",
        "replaced_by": "google_ads_api/canonical_spend",
        "note": ("Windsor.ai is no longer a production source. Campaign-daily "
                 "spend comes from the canonical Google Ads API service."),
    },
    "windsor/geo": {
        "status": "retired",
        "replaced_by": "google_ads_api/canonical_geo",
        "note": ("Windsor.ai is no longer a production source. Per-country "
                 "spend comes from the canonical Google Ads API geo service "
                 "(PR-ADS-153F), which is coverage-backed and reconciled."),
    },
    "windsor/search_terms": {
        "status": "retired",
        "replaced_by": "google_ads_api/search_terms",
        "note": ("Windsor.ai is no longer a production source. Search terms "
                 "come from the Google Ads API connector; that dataset is "
                 "refreshed by the weekly scheduler, not by this incremental "
                 "run, so no incremental entry replaces this one."),
    },
    "windsor/keywords": {
        "status": "retired",
        "replaced_by": None,
        "note": ("Windsor.ai is no longer a production source. NO canonical "
                 "Google Ads API incremental persistence path exists for "
                 "keywords today: `keyword_daily_facts` is written by the "
                 "weekly/monthly schedulers, not by this run. Reported as "
                 "unsupported rather than quietly kept on Windsor or reported "
                 "successful. Building an incremental keyword path is out of "
                 "scope for this hotfix."),
    },
}


#: Every (source, dataset) pair this scheduler stamps on ``sync_batches``.
#:
#: Declared so the contract test can enumerate them and prove each one is
#: registered. The production run logged "unknown source"/"unknown dataset" for
#: seven pairs, and the warning was the least of it: an unregistered pair is a
#: key the freshness configuration does not read, so the dataset reports "never
#: run" forever while its table fills up normally. Nothing fails; nothing shows.
#:
#: Datasets whose batches are opened by an owning SERVICE rather than by this
#: module (contact_funnel, mailchimp) are not listed — they are covered by their
#: own contract tests, next to the code that stamps them.
ACTIVE_SYNC_PAIRS: tuple[tuple[str, str], ...] = (
    ("hubspot", "contacts"),
    ("hubspot", "deals"),
    (GCLID_SOURCE, GCLID_MATCHES_DATASET),
    (DEAL_LEDGER_SOURCE, DEAL_LEDGER_DATASET),
    (SOURCE_CLASSIFICATION_SOURCE, SOURCE_CLASSIFICATION_DATASET),
    (CANONICAL_SPEND_SOURCE, CANONICAL_SPEND_DATASET),
    (FX_SOURCE, FX_DAILY_RATES_DATASET),
    (CANONICAL_GEO_SOURCE, CANONICAL_GEO_DATASET),
)


# ---------------------------------------------------------------------------
# Database readiness (PR-ADS-154 §1)
# ---------------------------------------------------------------------------

DB_UNAVAILABLE_REASON = "database_unavailable"


def ensure_database_ready() -> tuple[bool, str | None]:
    """Initialize the connection pool and PROVE it can serve a query.

    Returns ``(ready, detail)``. ``detail`` is None when ready.

    Why this exists
    ---------------
    A standalone ``python -m scheduler.incremental_sync`` process is not the
    Flask app: nothing has called :func:`db.connection.init_pool`, so the
    module-level ``_pool`` is ``None`` and every ``get_conn()`` yields ``None``.
    Each persistence call then degrades quietly to a no-op, which is exactly the
    right behaviour for a single writer and exactly the wrong behaviour for a
    whole run: the production run pulled real rows from Google Ads and HubSpot,
    wrote none of them, reported ``partial``, and exited 0.

    So the pool is not merely initialized — it is **probed**. ``init_pool()``
    swallows its own failure and leaves ``_pool = None``, and a pool that exists
    is still not a database that answers, so "we called init_pool" is not
    evidence. ``SELECT 1`` is.

    Read-only and side-effect free. The caller must abort the whole run on
    ``False``, before contacting any external platform: with no durable
    persistence, a pull is quota spent to produce nothing, and reporting on it
    would describe work whose results were discarded.
    """
    try:
        from db.connection import get_conn, init_pool  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return False, f"db.connection import failed: {exc}"

    try:
        init_pool()
    except Exception as exc:  # noqa: BLE001
        return False, f"init_pool failed: {exc}"

    try:
        with get_conn() as conn:
            if conn is None:
                return False, ("connection pool is not available "
                               "(no DATABASE_URL, or the database is unreachable)")
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                row = cur.fetchone()
        if not row or row[0] != 1:
            return False, "readiness probe returned no result"
    except Exception as exc:  # noqa: BLE001
        return False, f"readiness probe failed: {exc}"

    return True, None


def _database_unavailable_result(*, run_reason: str, started_at: str,
                                 detail: str | None) -> dict:
    """The structured result of a run that never started.

    Shaped like a completed run so every consumer — the CLI, the scheduler
    wrapper, the admin trigger — reads it the same way, but with no dataset
    entries at all. An empty `datasets` map is the honest report: nothing was
    attempted, so nothing may claim a status.
    """
    finished_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    message = (f"{DB_UNAVAILABLE_REASON}: {detail}" if detail
               else DB_UNAVAILABLE_REASON)
    log.error("[incremental_sync] aborting before any external pull — %s", message)
    return {
        "status": "failed",
        "reason": DB_UNAVAILABLE_REASON,
        "run_type": "daily_incremental_sync",
        "run_reason": run_reason,
        "run_id": None,
        "started_at": started_at,
        "finished_at": finished_at,
        "datasets": {},
        "errors": [message],
    }


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------

def run_daily_incremental_sync(
    *,
    lookback_days_ads: int = DEFAULT_LOOKBACK_ADS,
    lookback_days_hubspot_contacts: int = DEFAULT_LOOKBACK_HUBSPOT_CONTACTS,
    lookback_days_hubspot_deals: int = DEFAULT_LOOKBACK_HUBSPOT_DEALS,
    run_reason: str = "scheduled",
) -> dict:
    """Run daily rolling-window incremental sync for all major datasets.

    Syncs:
      - hubspot/contacts    (explicit date range via createdate filter)
      - hubspot/contact_funnel — CANONICAL all-source lifecycle spine,
                               watermarked on lastmodifieddate (PR-ADS-153B)
      - hubspot/deals       (via GCLID contacts pulled in the deals window)
      - gclid/matches       — skipped; no incremental DB path yet
      - hubspot/deal_ledger, hubspot/source_classification
      - google_ads_api/canonical_spend — the ROAS denominator
      - fx/daily_rates      — required before any USD figure is safe
      - google_ads_api/canonical_geo — CANONICAL per-country spend (PR-ADS-153F),
                               run after canonical spend and FX and followed by
                               geo reconciliation; resumable and coverage-backed
      - mailchimp/refresh   — read-only, clean skip when unconfigured

    The four Windsor datasets are recorded as ``retired`` (PR-ADS-154) and are
    never called: production no longer uses Windsor.ai.

    Each dataset failure is isolated: one failed dataset does not abort the
    rest.  The returned summary includes per-dataset status and an ``errors``
    list for datasets that raised unexpected exceptions.

    The run ABORTS before any external pull if the database is not ready — see
    :func:`ensure_database_ready`. A run with no durable persistence produces
    nothing, so spending Google Ads and HubSpot quota on it and then reporting
    per-dataset outcomes would describe work that was discarded.

    Returns a structured summary dict.  The caller (scheduler wrapper or
    manual trigger) is responsible for logging.

    NEVER writes to Google Ads, HubSpot, or any external platform.
    """
    now = datetime.now(tz=timezone.utc)
    started_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    today = now.date()

    # PR-ADS-154 §1: prove durable persistence BEFORE touching any external
    # platform. This must be the first thing the run does — a pull performed
    # without a database is quota spent to produce nothing, and every dataset
    # downstream would report on work whose results were thrown away.
    db_ready, db_detail = ensure_database_ready()
    if not db_ready:
        return _database_unavailable_result(
            run_reason=run_reason, started_at=started_at, detail=db_detail)

    date_from_contacts = today - timedelta(days=lookback_days_hubspot_contacts)
    date_from_deals = today - timedelta(days=lookback_days_hubspot_deals)

    datasets: dict[str, dict] = {}
    errors: list[str] = []

    # PR-ADS-114: create a genuine local run record so HubSpot contact/deal
    # persistence is attributed to a real run_id (never None). The per-dataset
    # persistence guard still prevents reporting success when rows were fetched
    # but zero rows were persisted.
    run_id = db_writers.write_run({
        "run_type": "daily_incremental_sync",
        "started_at": started_at,
        "status": "running",
    })
    # PR-ADS-154 §6: no run row means nothing this run writes can be attributed
    # to it, and `write_run` returns a falsy id rather than raising. The
    # readiness probe above should make this unreachable — which is precisely
    # why reaching it is worth failing on rather than continuing past.
    if not run_id:
        return _database_unavailable_result(
            run_reason=run_reason, started_at=started_at,
            detail="could not create a local run record (write_run returned no id)")

    log.info(
        "[incremental_sync] started reason=%s run_id=%s ads_lookback=%d "
        "contacts_lookback=%d deals_lookback=%d",
        run_reason, run_id, lookback_days_ads,
        lookback_days_hubspot_contacts, lookback_days_hubspot_deals,
    )

    # ── Retired: the four Windsor datasets (PR-ADS-154) ─────────────────────
    # Recorded, not deleted, so the report says what happened to them instead of
    # leaving the reader to diff two versions of this file. `retired` never
    # contributes to the overall verdict and never publishes freshness.
    for _name, _record in RETIRED_DATASETS.items():
        datasets[_name] = dict(_record)
        log.info("[incremental_sync] %s: retired (%s)",
                 _name, _record.get("replaced_by") or "no canonical replacement")

    # ── hubspot/contacts ──────────────────────────────────────────────────────
    datasets["hubspot/contacts"] = _sync_hubspot_contacts(
        run_id=run_id, date_from=date_from_contacts, date_to=today, errors=errors,
    )

    # ── hubspot/contact_funnel — CANONICAL CRM funnel spine (PR-ADS-153B) ────
    # Watermarked on lastmodifieddate, all sources, resumable. This is the ONE
    # writer of hubspot_contact_funnel; the legacy `leads` snapshot above keeps
    # serving pre-PR-ADS-153C pages until they migrate.
    datasets["hubspot/contact_funnel"] = _sync_contact_funnel(
        run_id=run_id, errors=errors,
    )

    # ── hubspot/deals (via GCLID contacts in the deal window) ────────────────
    datasets["hubspot/deals"] = _sync_hubspot_deals(
        run_id=run_id, date_from=date_from_deals, date_to=today, errors=errors,
    )

    # ── gclid/matches — closed-won deals by closedate → gclid_attribution ────
    # PR-ADS-114: this dataset now has a real persistence path. It pulls
    # closed-won deals DIRECTLY by closedate and writes GCLID-only attribution
    # rows (no synthetic GCLIDs) into gclid_attribution.
    datasets[f"{GCLID_SOURCE}/{GCLID_MATCHES_DATASET}"] = _sync_gclid_attribution(
        run_id=run_id, date_from=date_from_deals, date_to=today, errors=errors,
    )

    # ── hubspot/deal_ledger — PR-ADS-153E-A canonical deal ledger (shadow).
    # Reads HubSpot read-only across ALL tracked pipeline stages and writes only
    # the local canonical ledger. No page consumes it yet: 153E-A populates and
    # reconciles it, 153E-B migrates consumers. Orchestration only — every won,
    # currency, association and attribution decision lives in the service and
    # its pure rule modules, never here.
    datasets[f"{DEAL_LEDGER_SOURCE}/{DEAL_LEDGER_DATASET}"] = _sync_deal_ledger(
        run_id=run_id, errors=errors,
    )

    # ── hubspot/source_classification — keep acquisition-source classification
    # current (PR-ADS-117): classify newly-created contacts (all sources) and
    # attribute recent closed-won deals. Read-only from HubSpot; local DB only.
    datasets[f"{SOURCE_CLASSIFICATION_SOURCE}/{SOURCE_CLASSIFICATION_DATASET}"] = _sync_source_classification(
        run_id=run_id, date_from=date_from_contacts, date_to=today, errors=errors,
    )

    # ── google_ads/canonical_spend — keep canonical campaign-daily spend current
    # (PR-ADS-118) with a small daily lookback for late Google Ads adjustments.
    # Reads Google Ads read-only; writes only local canonical tables.
    datasets[f"{CANONICAL_SPEND_SOURCE}/{CANONICAL_SPEND_DATASET}"] = _sync_canonical_spend(
        run_id=run_id, date_to=today, errors=errors,
    )

    # ── fx/daily_rates — keep daily GBP→USD FX rates current (PR-ADS-119) so
    # native spend can be converted to USD reporting spend per spend_date. Reads
    # published reference rates read-only; writes only the local fx_rates table.
    datasets[f"{FX_SOURCE}/{FX_DAILY_RATES_DATASET}"] = _sync_fx_rates(
        run_id=run_id, date_to=today, errors=errors,
    )

    # ── google_ads/canonical_geo — PR-ADS-153F. Country ROAS needs per-country
    # spend that reconciles with the canonical campaign total, and until this PR
    # NOTHING scheduled it: the only caller was the manual Revenue Health button,
    # so canonical geo went stale the moment the window advanced past the last
    # human click and the page blocked itself (correctly) for a reason no health
    # surface could see.
    #
    # The ORDER here is deliberate and must not be rearranged:
    #   1. canonical campaign spend  — the reconciliation baseline
    #   2. FX rates                  — needed before any USD geo figure is safe
    #   3. canonical geo             — reconciled against (1), converted using (2)
    #   4. geo reconciliation        — evaluated only after 1-3 have landed
    # Reconciling before geo lands would score the previous run's coverage.
    datasets[f"{CANONICAL_GEO_SOURCE}/{CANONICAL_GEO_DATASET}"] = _sync_canonical_geo(
        run_id=run_id, date_to=today, errors=errors,
    )

    # ── google_ads/geo_reconciliation — step 4. Read-only diagnostics computed
    # AFTER spend, FX and geo are current, so the recorded verdict describes this
    # run's data rather than the previous one's. Publishes no external state and
    # never gates the sync: a reconciliation that reports "blocked" is a true
    # answer about the data, not a sync failure.
    datasets[f"{CANONICAL_GEO_SOURCE}/geo_reconciliation"] = _publish_geo_reconciliation(
        errors=errors,
    )

    # ── mailchimp — read-only email-marketing refresh (PR-ADS-151). Pulls recent
    # campaigns + refreshes recent reports + snapshots audiences. Skipped cleanly
    # when Mailchimp is not configured. GET-only; local DB writes only.
    datasets["mailchimp/refresh"] = _sync_mailchimp(run_id=run_id, errors=errors)

    finished_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    overall_status = _overall_status(datasets)

    # Reflect the true final state on the local run record.
    db_writers.update_run(run_id, {
        "finished_at": finished_at,
        "status": "success" if overall_status in ("success", "partial") else "failed",
        "error_message": "; ".join(errors)[:1000] if errors else None,
    })

    summary = {
        "status": overall_status,
        "run_type": "daily_incremental_sync",
        "run_reason": run_reason,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "lookback": {
            "ads_days": lookback_days_ads,
            "hubspot_contacts_days": lookback_days_hubspot_contacts,
            "hubspot_deals_days": lookback_days_hubspot_deals,
        },
        "datasets": datasets,
        "errors": errors,
    }

    log.info(
        "[incremental_sync] finished status=%s datasets=%s errors=%d",
        overall_status, list(datasets.keys()), len(errors),
    )
    return summary


# ---------------------------------------------------------------------------
# Per-dataset sync helpers
# ---------------------------------------------------------------------------

class BatchTrackingError(RuntimeError):
    """A dataset ran without a durable record that it ran.

    ``start_sync_batch`` returns 0 when the database is unavailable or the
    inputs are invalid, and the previous code simply skipped ``finish_sync_batch``
    in that case while still returning ``{"status": "success"}``. The row count
    was real; the evidence that anything was written was not. A dataset whose
    batch was never opened publishes no freshness, so calling it successful
    asserts something no surface can corroborate.
    """


def _require_batch(batch_id, dataset: str) -> int:
    """Return a usable batch id, or raise :class:`BatchTrackingError`.

    PR-ADS-154 §6. Called immediately after ``start_sync_batch`` so the dataset
    fails BEFORE the external pull, rather than pulling successfully and then
    discovering it cannot record the outcome.
    """
    if not batch_id:
        raise BatchTrackingError(
            f"{dataset}: sync batch could not be opened — the run has no durable "
            "record, so its outcome cannot be published as freshness")
    return int(batch_id)

def _sync_mailchimp(*, run_id, errors: list) -> dict:
    """Read-only Mailchimp incremental refresh (PR-ADS-151).

    Delegates to services.mailchimp_sync_service.run_incremental, which manages
    its own sync_batches for mailchimp/campaigns, mailchimp/reports and
    mailchimp/audiences. Returns a compact status; a not-configured Mailchimp is a
    clean skip (never an error). GET-only against Mailchimp.
    """
    try:
        from services.mailchimp_sync_service import run_incremental  # noqa: PLC0415
        result = run_incremental(run_id=run_id)
        status = result.get("status", "unknown")
        if status == "skipped":
            return {"status": "skipped", "reason": result.get("reason"),
                    "note": result.get("detail")}
        if status not in ("success", "partial"):
            err = f"mailchimp/refresh: {result.get('reason') or status}"
            errors.append(err)
            log.warning("[incremental_sync] %s", err)
        ds = result.get("datasets", {})
        return {
            "status": status,
            "campaigns": (ds.get("campaigns") or {}).get("written"),
            "reports_refreshed": (ds.get("reports") or {}).get("refreshed"),
            "audiences": (ds.get("audiences") or {}).get("written"),
        }
    except Exception as exc:  # noqa: BLE001
        err = f"mailchimp/refresh: {exc}"
        errors.append(err)
        log.warning("[incremental_sync] %s", err)
        return {"status": "failed", "error": str(exc)[:500]}


def _sync_hubspot_contacts(
    *, run_id, date_from, date_to, errors: list
) -> dict:
    """Pull and persist hubspot/contacts for the given date range."""
    from connectors.hubspot_pull import pull_paid_search_contacts_in_range  # noqa: PLC0415

    batch_id = db_writers.start_sync_batch(
        source="hubspot",
        dataset="contacts",
        sync_type="daily",
        date_from=date_from,
        date_to=date_to,
        run_id=run_id,
    )
    try:
        batch_id = _require_batch(batch_id, "hubspot/contacts")
        rows = pull_paid_search_contacts_in_range(
            date_from=str(date_from), date_to=str(date_to),
        )
        rows_pulled = len(rows)
        rows_written = db_writers.write_leads(run_id, rows)

        if not persistence_succeeded(rows, rows_written):
            raise RuntimeError(
                f"hubspot/contacts persistence failed: "
                f"{rows_pulled} pulled, {rows_written} written"
            )

        last_date = max_source_date(rows, fallback_date=date_to)
        if batch_id:
            db_writers.finish_sync_batch(
                batch_id=batch_id,
                status="success",
                row_count=rows_written,
                last_source_date=last_date,
            )
        return {"status": "success", "rows_pulled": rows_pulled, "rows_written": rows_written}

    except Exception as exc:  # noqa: BLE001
        err = f"hubspot/contacts: {exc}"
        errors.append(err)
        log.warning("[incremental_sync] %s", err)
        if batch_id:
            db_writers.finish_sync_batch(
                batch_id=batch_id,
                status="failed",
                error_message=str(exc)[:1000],
            )
        return {"status": "failed", "error": str(exc)[:500]}


def _sync_hubspot_deals(
    *, run_id, date_from, date_to, errors: list
) -> dict:
    """Pull and persist hubspot/deals for contacts created in the deals window.

    Deals are fetched via GCLID contacts (pull_deals_with_gclid).  Only
    contacts within the deals rolling window are queried; this avoids a
    full-history scan every day.

    Limitation: deals without a GCLID contact in the window are not captured
    in this incremental pass.  Full deal coverage requires historical backfill.
    """
    from connectors.hubspot_pull import (  # noqa: PLC0415
        pull_paid_search_contacts_in_range,
        pull_deals_with_gclid,
    )

    batch_id = db_writers.start_sync_batch(
        source="hubspot",
        dataset="deals",
        sync_type="daily",
        date_from=date_from,
        date_to=date_to,
        run_id=run_id,
    )
    try:
        batch_id = _require_batch(batch_id, "hubspot/deals")
        contacts = pull_paid_search_contacts_in_range(
            date_from=str(date_from), date_to=str(date_to),
        )
        rows = pull_deals_with_gclid(contacts)
        rows_pulled = len(rows)
        rows_written = db_writers.write_deals(run_id, rows)

        if not persistence_succeeded(rows, rows_written):
            raise RuntimeError(
                f"hubspot/deals persistence failed: "
                f"{rows_pulled} pulled, {rows_written} written"
            )

        last_date = max_source_date(rows, fallback_date=date_to)
        if batch_id:
            db_writers.finish_sync_batch(
                batch_id=batch_id,
                status="success",
                row_count=rows_written,
                last_source_date=last_date,
            )
        return {"status": "success", "rows_pulled": rows_pulled, "rows_written": rows_written}

    except Exception as exc:  # noqa: BLE001
        err = f"hubspot/deals: {exc}"
        errors.append(err)
        log.warning("[incremental_sync] %s", err)
        if batch_id:
            db_writers.finish_sync_batch(
                batch_id=batch_id,
                status="failed",
                error_message=str(exc)[:1000],
            )
        return {"status": "failed", "error": str(exc)[:500]}


def _sync_gclid_attribution(
    *, run_id, date_from, date_to, errors: list
) -> dict:
    """Pull closed-won deals by closedate and persist GCLID attribution rows.

    PR-ADS-114: the daily revenue path. Fetches closed-won deals DIRECTLY by
    closedate (never via recently-created contacts) and writes GCLID-only
    attribution rows into gclid_attribution. No synthetic GCLIDs are created;
    deals without click evidence are reported, not attributed.
    """
    from connectors.hubspot_pull import pull_closed_won_deals_in_range  # noqa: PLC0415
    from services.revenue_recovery_service import build_attribution_rows_from_deals  # noqa: PLC0415

    # PR-ADS-154: stamps `(gclid, matches)` — the key the freshness config
    # reads and the registry recognises. It previously stamped
    # `(hubspot, gclid_matches)`, which matched neither: the run logged
    # "unknown dataset 'gclid_matches'" and `gclid_attribution` reported
    # "never run" forever while its table filled up normally.
    batch_id = db_writers.start_sync_batch(
        source=GCLID_SOURCE,
        dataset=GCLID_MATCHES_DATASET,
        sync_type="daily",
        date_from=date_from,
        date_to=date_to,
        run_id=run_id,
    )
    try:
        batch_id = _require_batch(batch_id, "gclid/matches")
        deals = pull_closed_won_deals_in_range(
            date_from=str(date_from), date_to=str(date_to),
        )
        rows, counts = build_attribution_rows_from_deals(deals)
        rows_pulled = len(rows)
        rows_written = db_writers.write_gclid_attribution(run_id, rows)

        # Attribution rows are GCLID-only by construction; if any were prepared
        # but none persisted, that is a real persistence failure.
        if not persistence_succeeded(rows, rows_written):
            raise RuntimeError(
                f"gclid/matches persistence failed: "
                f"{rows_pulled} attributable, {rows_written} written"
            )

        last_date = max_source_date(
            [{"date": d.get("deal_close_date")} for d in deals], fallback_date=date_to,
        )
        if batch_id:
            db_writers.finish_sync_batch(
                batch_id=batch_id,
                status="success",
                row_count=rows_written,
                last_source_date=last_date,
            )
        return {
            "status": "success",
            "rows_pulled": rows_pulled,
            "rows_written": rows_written,
            "closed_won_deals_found": counts["closed_won_deals_found"],
            "closed_won_deals_with_gclid": counts["closed_won_deals_with_gclid"],
            "closed_won_deals_without_gclid": counts["closed_won_deals_without_gclid"],
        }

    except Exception as exc:  # noqa: BLE001
        err = f"gclid/matches: {exc}"
        errors.append(err)
        log.warning("[incremental_sync] %s", err)
        if batch_id:
            db_writers.finish_sync_batch(
                batch_id=batch_id,
                status="failed",
                error_message=str(exc)[:1000],
            )
        return {"status": "failed", "error": str(exc)[:500]}


def _sync_deal_ledger(*, run_id, errors: list) -> dict:
    """Orchestrate the PR-ADS-153E-A canonical deal-ledger sync.

    Contains NO revenue, currency, won-state or attribution logic — it starts a
    batch, calls the service, and records what happened. A failed or partial
    sync is reported as failed/partial: it must never surface as a successful
    zero-row result, because a silent revenue gap is worse than a loud failure.
    """
    batch_id = db_writers.start_sync_batch(
        source=DEAL_LEDGER_SOURCE, dataset=DEAL_LEDGER_DATASET, sync_type="daily",
        run_id=run_id)
    try:
        # A ledger sync with no durable batch cannot publish freshness and its
        # written count cannot be attributed to anything, so it fails before the
        # HubSpot pull rather than after it.
        batch_id = _require_batch(batch_id, "hubspot/deal_ledger")
        from services.hubspot_deal_sync_service import sync_deals  # noqa: PLC0415

        result = sync_deals(batch_id=batch_id)
    except Exception as exc:  # noqa: BLE001
        log.error("[incremental] deal ledger sync failed: %s", exc, exc_info=True)
        errors.append(f"hubspot/deal_ledger: {exc}")
        db_writers.finish_sync_batch(batch_id, status="failed",
                                     error_message=str(exc))
        return {"status": "failed", "error": str(exc), "rows": 0}

    status = result.get("status") or "failed"
    if status != "success":
        errors.append(
            f"hubspot/deal_ledger: {status} "
            f"({result.get('error') or 'incomplete'}; "
            f"{result.get('association_failures', 0)} association failure(s))")
    # sync_batches accepts success|failed only, so a PARTIAL sync is recorded
    # as failed with its reason — a partial run must never look successful.
    db_writers.finish_sync_batch(
        batch_id,
        status=("success" if status == "success" else "failed"),
        row_count=result.get("written", 0),
        error_message=(result.get("error")
                       or (None if status == "success" else status)))
    return {
        "status": status,
        "rows": result.get("written", 0),
        "deals_seen": result.get("deals_seen", 0),
        "skipped_stale": result.get("skipped_stale", 0),
        "association_failures": result.get("association_failures", 0),
        "complete": result.get("complete", False),
        "error": result.get("error"),
    }


def _sync_source_classification(
    *, run_id, date_from, date_to, errors: list
) -> dict:
    """Classify recent all-source contacts and attribute recent closed-won deals.

    PR-ADS-117: keeps the acquisition-source classification current between full
    backfills. Reads HubSpot read-only and writes only the local classification
    tables; never writes to HubSpot or Google Ads.
    """
    from connectors.hubspot_pull import (  # noqa: PLC0415
        pull_all_contacts_in_range,
        pull_closed_won_deals_with_sources_in_range,
    )
    from services.source_attribution_service import (  # noqa: PLC0415
        classify_contact_row, attribute_deal_row,
    )

    batch_id = db_writers.start_sync_batch(
        source=SOURCE_CLASSIFICATION_SOURCE, dataset=SOURCE_CLASSIFICATION_DATASET,
        sync_type="daily", date_from=date_from, date_to=date_to, run_id=run_id,
    )
    try:
        batch_id = _require_batch(batch_id, "hubspot/source_classification")
        contacts = pull_all_contacts_in_range(date_from=str(date_from), date_to=str(date_to))
        contact_rows = [classify_contact_row(c) for c in contacts]
        contacts_written = db_writers.upsert_contact_source_classification(contact_rows)

        deals = pull_closed_won_deals_with_sources_in_range(
            date_from=str(date_from), date_to=str(date_to))
        deal_rows = [attribute_deal_row(d) for d in deals]
        deals_written = db_writers.upsert_deal_source_attribution(deal_rows)

        # PR-ADS-154 §6: fail closed on pulled-positive / written-zero. This
        # dataset previously reported `success` with `contacts_classified` set
        # to the number of rows PREPARED, so a run that classified 900 contacts
        # and persisted none of them looked identical to one that persisted all
        # of them. Preparing a row in memory is not evidence of anything.
        if not persistence_succeeded(contact_rows, contacts_written):
            raise RuntimeError(
                f"contact classification persisted {contacts_written} of "
                f"{len(contact_rows)} row(s)")
        if not persistence_succeeded(deal_rows, deals_written):
            raise RuntimeError(
                f"deal source attribution persisted {deals_written} of "
                f"{len(deal_rows)} row(s)")

        db_writers.finish_sync_batch(
            batch_id=batch_id, status="success",
            row_count=contacts_written + deals_written, last_source_date=date_to,
        )
        # The reported figures are what LANDED, not what was prepared.
        return {
            "status": "success",
            "contacts_classified": contacts_written,
            "deals_attributed": deals_written,
            "contacts_pulled": len(contact_rows),
            "deals_pulled": len(deal_rows),
        }
    except Exception as exc:  # noqa: BLE001
        err = f"hubspot/source_classification: {exc}"
        errors.append(err)
        log.warning("[incremental_sync] %s", err)
        if batch_id:
            db_writers.finish_sync_batch(
                batch_id=batch_id, status="failed", error_message=str(exc)[:1000])
        return {"status": "failed", "error": str(exc)[:500]}


def _sync_contact_funnel(*, run_id, errors: list) -> dict:
    """Refresh the canonical HubSpot contact funnel (PR-ADS-153B).

    Delegates entirely to the single owning ingestion service — this scheduler
    never writes canonical contact rows itself, so ownership stays unambiguous.
    The service manages its own sync batches, durable watermark and bootstrap
    state, and never raises.
    """
    from services.hubspot_contact_funnel_sync_service import (  # noqa: PLC0415
        get_bootstrap_mode, run_contact_funnel_sync,
    )

    try:
        mode = get_bootstrap_mode()
        result = run_contact_funnel_sync(mode=mode, run_id=run_id)
        if result.get("status") == "failed":
            err = f"hubspot/contact_funnel: {result.get('error')}"
            errors.append(err)
            log.warning("[incremental_sync] %s", err)
        else:
            log.info(
                "[incremental_sync] hubspot/contact_funnel: mode=%s seen=%s written=%s",
                mode, result.get("contacts_seen"), result.get("contacts_written"),
            )
        return result
    except Exception as exc:  # noqa: BLE001
        err = f"hubspot/contact_funnel: {exc}"
        errors.append(err)
        log.warning("[incremental_sync] %s", err)
        return {"status": "failed", "error": str(exc)[:500]}


def _sync_canonical_spend(*, run_id, date_to, errors: list) -> dict:
    """Refresh canonical Google Ads spend for a small daily lookback window.

    PR-ADS-118: re-fetches the last few days so late Google Ads spend adjustments
    are captured. Idempotent upsert (no double counting). Records a verified
    coverage chunk. Reads Google Ads read-only; writes only local canonical
    tables.
    """
    from services.google_ads_spend_service import (  # noqa: PLC0415
        fetch_daily_spend, configured_customer_id, SpendPersistenceError,
        DAILY_SPEND_LOOKBACK_DAYS,
    )
    from datetime import timedelta as _td

    run_id_str = str(run_id) if run_id else None
    start = date_to - _td(days=DAILY_SPEND_LOOKBACK_DAYS - 1)
    batch_id = db_writers.start_sync_batch(
        source=CANONICAL_SPEND_SOURCE, dataset=CANONICAL_SPEND_DATASET,
        sync_type="daily", date_from=start, date_to=date_to, run_id=run_id,
    )
    try:
        batch_id = _require_batch(batch_id, "google_ads_api/canonical_spend")
        payload = fetch_daily_spend(str(start), str(date_to))
        rows = payload.get("rows", [])
        micros = sum(int(r.get("cost_micros") or 0) for r in rows)
        customer_id = payload.get("customer_id") or configured_customer_id()
        # Fail closed: a successful read must be durably persisted before the
        # chunk is marked verified and the dataset reported successful.
        written = db_writers.upsert_campaign_daily_spend(rows, sync_run_id=run_id_str)
        if rows and written != len(rows):
            raise SpendPersistenceError(f"spend upsert wrote {written}/{len(rows)} rows")
        ok = db_writers.upsert_spend_coverage(
            customer_id, str(start), str(date_to), "verified",
            rows_written=written, cost_micros_total=micros,
            source_query_version=payload.get("source_query_version"),
            sync_run_id=run_id_str,
        )
        if not ok:
            raise SpendPersistenceError("coverage-ledger upsert failed")
        # PR-ADS-120: also refresh the direct account-daily total for the same
        # window (best-effort) so campaign↔account reconciliation stays current.
        try:
            from services.google_ads_spend_service import fetch_account_daily_spend  # noqa: PLC0415
            acct = fetch_account_daily_spend(str(start), str(date_to))
            db_writers.upsert_account_daily_spend(acct.get("rows", []), sync_run_id=run_id_str)
        except Exception as exc:  # noqa: BLE001
            log.warning("[incremental_sync] account-daily spend refresh failed: %s", exc)
        if batch_id:
            db_writers.finish_sync_batch(
                batch_id=batch_id, status="success", row_count=written, last_source_date=date_to)
        return {"status": "success", "rows_written": written, "cost_micros": micros}
    except Exception as exc:  # noqa: BLE001
        err = f"google_ads/canonical_spend: {exc}"
        errors.append(err)
        log.warning("[incremental_sync] %s", err)
        # Record a `failed` coverage chunk against the real configured customer
        # ID (never overwrites a prior verified chunk — writer guard).
        try:
            db_writers.upsert_spend_coverage(
                configured_customer_id(), str(start), str(date_to), "failed",
                sync_run_id=run_id_str)
        except Exception:  # noqa: BLE001
            pass
        if batch_id:
            db_writers.finish_sync_batch(batch_id=batch_id, status="failed", error_message=str(exc)[:1000])
        return {"status": "failed", "error": str(exc)[:500]}


def _sync_fx_rates(*, run_id, date_to, errors: list) -> dict:
    """Refresh daily GBP→USD FX rates for a small lookback window (PR-ADS-119).

    Idempotent (only missing dates are fetched). Reads published reference rates
    read-only; writes ONLY the local fx_rates table. A fetch failure marks the
    dataset failed (FX coverage then stays incomplete, which safely blocks ROAS).
    """
    from services.fx_service import ensure_fx_rates, NATIVE_CURRENCY, REPORTING_CURRENCY  # noqa: PLC0415
    from datetime import timedelta as _td

    lookback_days = 7
    start = date_to - _td(days=lookback_days - 1)
    batch_id = db_writers.start_sync_batch(
        source=FX_SOURCE, dataset=FX_DAILY_RATES_DATASET, sync_type="daily",
        date_from=start, date_to=date_to, run_id=run_id,
    )
    try:
        batch_id = _require_batch(batch_id, "fx/daily_rates")
        result = ensure_fx_rates(start, date_to, base_currency=NATIVE_CURRENCY,
                                 quote_currency=REPORTING_CURRENCY, only_missing=True)
        failed = result.get("failed") or []
        if failed:
            raise RuntimeError(f"{len(failed)} FX date(s) failed to fetch")

        # PR-ADS-154 §6: "nothing was missing" and "everything failed to persist"
        # both arrive here as rows_written == 0 with an empty `failed` list —
        # `upsert_fx_rates` returns 0 on an unavailable database rather than
        # raising, so the fetch loop records no failure. `fetched` separates
        # them: it counts the dates this run actually went and got.
        fetched = int(result.get("fetched") or 0)
        written = int(result.get("rows_written") or 0)
        if fetched and written == 0:
            raise RuntimeError(
                f"fetched {fetched} FX rate(s) and persisted none — "
                "FX coverage stays incomplete, which correctly blocks USD ROAS")

        db_writers.finish_sync_batch(
            batch_id=batch_id, status="success",
            row_count=written, last_source_date=date_to)
        return {
            "status": "success",
            "rows_written": written,
            "rates_fetched": fetched,
            # Explicit, so a reader can tell an idle refresh from a busy one
            # rather than inferring it from a zero.
            "already_current": fetched == 0,
        }
    except Exception as exc:  # noqa: BLE001
        err = f"fx/daily_rates: {exc}"
        errors.append(err)
        log.warning("[incremental_sync] %s", err)
        if batch_id:
            db_writers.finish_sync_batch(batch_id=batch_id, status="failed", error_message=str(exc)[:1000])
        return {"status": "failed", "error": str(exc)[:500]}


def _sync_canonical_geo(*, run_id, date_to, errors: list) -> dict:
    """Refresh canonical Google Ads GEO spend for a small daily lookback (PR-ADS-153F).

    Delegates every decision to ``services.google_ads_geo_sync_service`` — the one
    owner of geo chunking, the durable coverage ledger, resume and the run lease.
    This function starts a batch, calls the service, and records exactly what
    happened.

    A ``partial`` run is recorded as FAILED on the sync batch, because
    ``sync_batches`` accepts success|failed only and a partially covered geo
    range must never surface as a healthy dataset — an under-covered geo
    denominator is precisely what makes Country ROAS wrong rather than absent.

    ``skipped_locked`` is reported as ``skipped``: another worker holds the
    lease, so this is not a failure and must not be counted as one. An
    **unreachable** lease store is the opposite — ``failed``, with a failed sync
    batch and an entry in the run's error list. The two used to share one
    outcome, which meant a database outage looked exactly like healthy
    concurrency and geo could stop syncing indefinitely without anything saying so.

    Reads Google Ads read-only; writes only local canonical tables.
    """
    # The dataset key comes from the shared registry, not from the geo service's
    # re-export: one import path for the key means one place to change it.
    from services.dataset_keys import (  # noqa: PLC0415
        CANONICAL_GEO_DATASET as GEO_SYNC_DATASET,
        CANONICAL_GEO_SOURCE as GEO_SYNC_SOURCE,
    )
    from services.google_ads_geo_sync_service import (  # noqa: PLC0415
        DAILY_GEO_LOOKBACK_DAYS, run_google_ads_geo_sync,
    )
    from datetime import timedelta as _td

    start = date_to - _td(days=DAILY_GEO_LOOKBACK_DAYS - 1)

    def _batch(status, **fields):
        """Record this attempt as one sync batch, opened only once we know it ran.

        A lease skip deliberately writes NO batch: the worker that holds the
        lease records the real outcome, and stamping a `failed` batch here would
        make a benign concurrency skip look like a broken geo dataset on every
        freshness surface.
        """
        bid = db_writers.start_sync_batch(
            source=GEO_SYNC_SOURCE, dataset=GEO_SYNC_DATASET, sync_type="daily",
            date_from=start, date_to=date_to, run_id=run_id,
        )
        # PR-ADS-154 §6: a batch that could not be opened is not a detail to
        # swallow. Without it this dataset publishes no freshness, so silently
        # continuing would let the run report a healthy geo refresh that no
        # surface can corroborate.
        if not bid:
            raise BatchTrackingError(
                "google_ads_api/canonical_geo: sync batch could not be opened")
        db_writers.finish_sync_batch(batch_id=bid, status=status, **fields)

    try:
        result = run_google_ads_geo_sync(
            date_from=str(start), date_to=str(date_to), dry_run=False,
            job_id=str(run_id) if run_id else None,
        )
    except Exception as exc:  # noqa: BLE001
        err = f"google_ads/canonical_geo: {exc}"
        errors.append(err)
        log.warning("[incremental_sync] %s", err)
        _batch("failed", error_message=str(exc)[:1000])
        return {"status": "failed", "error": str(exc)[:500]}

    status = result.get("status") or "failed"
    if status == "skipped_locked":
        # Genuinely benign: another worker owns the lease and will record the
        # real outcome. This is the ONLY lease result treated this way — a lease
        # store that could not be reached comes back as `failed` below, because
        # an outage that presents itself as healthy concurrency is an outage
        # nobody will notice.
        log.info("[incremental_sync] google_ads/canonical_geo: skipped (lease held)")
        return {"status": "skipped", "reason": result.get("reason"),
                "note": "another canonical geo sync was already running"}

    summary = result.get("summary") or {}
    if result.get("reason") == "lease_store_unavailable":
        err = ("google_ads/canonical_geo: failed (lease_store_unavailable) — the "
               "geo sync coordination store could not be reached, so no range "
               "was fetched and none can be certified")
        errors.append(err)
        log.error("[incremental_sync] %s", err)
        _batch("failed", error_message="lease_store_unavailable")
        return {"status": "failed", "reason": "lease_store_unavailable",
                "rows_written": 0, "chunks_verified": 0, "chunks_failed": 0,
                "chunks_skipped": 0, "coverage_complete": False,
                "errors": result.get("errors", [])}

    if status != "success":
        errors.append(f"google_ads/canonical_geo: {status} "
                      f"({summary.get('chunks_failed', 0)} chunk(s) failed)")
    _batch(
        "success" if status == "success" else "failed",
        row_count=summary.get("rows_written", 0),
        last_source_date=(date_to if status == "success" else None),
        error_message=(None if status == "success"
                       else "; ".join(result.get("errors") or [])[:1000] or status),
    )
    return {
        "status": status,
        "rows_written": summary.get("rows_written", 0),
        "chunks_verified": summary.get("chunks_verified", 0),
        "chunks_failed": summary.get("chunks_failed", 0),
        "chunks_skipped": summary.get("chunks_skipped", 0),
        "coverage_complete": result.get("coverage_complete"),
        "errors": result.get("errors", []),
    }


def _publish_geo_reconciliation(*, errors: list) -> dict:
    """Evaluate geo↔campaign reconciliation after the geo sync (PR-ADS-153F).

    Read-only. Runs LAST so the verdict describes the data this run produced.

    A reconciliation that was PERFORMED and disagreed is a truthful answer about
    the data, not a broken step: `mismatch` keeps the dataset `success`, because
    treating an honest disagreement as a sync failure would train operators to
    ignore real sync failures.

    PR-ADS-154 §6 draws the other half of that line. A reconciliation that could
    NOT be performed — `unavailable` (an input was unreadable) or `no_geo_data`
    (the geo total was not measurable) — is not an answer at all, and reporting
    it as `success` claimed a step had run that had not. The two cases used to
    return the same thing.
    """
    #: Reconciliation statuses that mean the comparison actually happened.
    evaluated = ("reconciled", "mismatch")
    try:
        from services.google_ads_geo_sync_service import build_geo_reconciliation  # noqa: PLC0415

        recon = build_geo_reconciliation("current_quarter")
        recon_status = recon.get("status")
        log.info("[incremental_sync] google_ads/geo_reconciliation: status=%s "
                 "country_spend_status=%s missing_dates=%d",
                 recon_status, recon.get("country_spend_status"),
                 len(recon.get("missing_geo_dates") or []))
        result = {
            "status": "success" if recon_status in evaluated else "failed",
            "reconciliation_status": recon_status,
            # Published explicitly so the post-deploy check can assert
            # "available and reconciled" without re-deriving either.
            "available": recon_status in evaluated,
            "reconciled": bool(recon.get("reconciled")),
            "country_spend_status": recon.get("country_spend_status"),
            "geo_ready": recon.get("geo_ready"),
            "geo_gap_codes": recon.get("geo_gap_codes") or [],
            "missing_geo_dates": len(recon.get("missing_geo_dates") or []),
            "reason": recon.get("reason"),
        }
        if result["status"] == "failed":
            err = (f"google_ads/geo_reconciliation: could not be evaluated "
                   f"(status={recon_status}, gaps={result['geo_gap_codes']})")
            result["error"] = err
            errors.append(err)
            log.warning("[incremental_sync] %s", err)
        return result
    except Exception as exc:  # noqa: BLE001
        err = f"google_ads/geo_reconciliation: {exc}"
        errors.append(err)
        log.warning("[incremental_sync] %s", err)
        return {"status": "failed", "error": str(exc)[:500]}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: Dataset outcomes that describe work NOT attempted, and therefore cannot vote
#: on the overall verdict. `skipped` = not applicable on this run (unconfigured
#: integration, no persistence path). `retired` = removed from orchestration for
#: good (PR-ADS-154). Neither may count as a success, because neither did
#: anything that could have succeeded.
NON_VOTING_STATUSES = frozenset({"skipped", "retired"})


def _overall_status(datasets: dict) -> str:
    """Derive overall sync status from individual dataset outcomes.

    Returns 'success' when every voting dataset succeeded, 'partial' when some
    succeeded and some did not, and 'failed' when none succeeded.

    Anything that is neither `success` nor a non-voting status counts as a
    failure. That is deliberate: an unrecognised status is not evidence of
    success, and defaulting the unknown case to "fine" is how a new outcome
    string silently turns a broken run green.
    """
    statuses = [v.get("status") for v in datasets.values()
                if v.get("status") not in NON_VOTING_STATUSES]
    if not statuses:
        return "success"  # nothing was attempted — nothing can have failed
    successes = statuses.count("success")
    failures  = len(statuses) - successes
    if failures == 0:
        return "success"
    if successes == 0:
        return "failed"
    return "partial"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    """Run one incremental sync and return the process exit code.

    PR-ADS-154 §5: the exit code AGREES with the reported status. It previously
    always fell off the end of the module at 0, so a run that reported `partial`
    with nine failed datasets was indistinguishable, to any caller, from a clean
    one — and an operator reading `echo $?` was told everything worked.

    Exit 0 means and only means: every dataset that ran, succeeded.
    """
    import json as _json
    import logging as _logging

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    result = run_daily_incremental_sync(run_reason="cli")
    print(_json.dumps(result, indent=2, default=str))
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
