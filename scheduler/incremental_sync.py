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
    KEYWORD_FACTS_DATASET, KEYWORD_FACTS_SOURCE,
    SEARCH_TERMS_DATASET, SEARCH_TERMS_SOURCE,
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
#: PR-ADS-154C — datasets that still RUN but are NOT authoritative for any
#: production metric. Distinct from `RETIRED_DATASETS`, which never run at all.
#:
#: The production run that motivated this register showed HubSpot 429 responses
#: in the legacy `hubspot/deals` contact-association scan while the canonical
#: `hubspot/deal_ledger` completed with zero association failures and zero write
#: failures. Both statements were true, and read side by side they invited the
#: wrong conclusion: that revenue truth had been degraded. It had not — the two
#: datasets answer different questions, and only one of them is truth.
#:
#: The legacy scan is kept for migration evidence and reconciliation. Marking it
#: here is what makes "it did not contaminate the totals" checkable from the
#: payload rather than something a reader has to be told. Nothing is deleted.
LEGACY_NON_AUTHORITATIVE_DATASETS: dict[str, dict] = {
    "hubspot/deals": {
        "authoritative": False,
        "superseded_by": "hubspot/deal_ledger",
        "note": ("Legacy GCLID-contact association scan. Retained for migration "
                 "evidence and reconciliation only. It holds the GCLID-attributable "
                 "SUBSET of deals, so it can never stand in for all-source revenue, "
                 "and a partial or rate-limited result here does not move canonical "
                 "truth readiness — which is derived from the canonical deal ledger "
                 "and the geo reconciliation, never from this scan."),
    },
}

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
        # PR-ADS-156: this note used to say search terms were refreshed "by the
        # weekly scheduler, not by this incremental run, so no incremental entry
        # replaces this one". That was true when it was written and stopped
        # being true here: the replacement now runs in THIS run, every day,
        # through the shared canonical service. A registry that describes the
        # system as it used to be is worse than one with a gap in it, because a
        # reader has no reason to doubt it.
        "note": ("Windsor.ai is no longer a production source. Search terms come "
                 "from the direct Google Ads API through the shared canonical "
                 "sync service, refreshed on a rolling recovery window by THIS "
                 "incremental run (as well as by the daily/weekly/monthly "
                 "schedulers, which call the same service)."),
    },
    "windsor/keywords": {
        "status": "retired",
        "replaced_by": "google_ads_api/keyword_facts",
        # PR-ADS-156: this claimed "NO canonical Google Ads API incremental
        # persistence path exists for keywords today". `keyword_sync_service`
        # existed and was already the single durable writer; what was missing
        # was a call from this run, which is now here.
        "note": ("Windsor.ai is no longer a production source. Durable keyword "
                 "facts come from the direct Google Ads API through "
                 "`keyword_sync_service`, refreshed on the established 30-day "
                 "correction window by THIS incremental run. The legacy "
                 "`keywords` snapshot is a different, non-authoritative dataset "
                 "and is never Keyword Evidence."),
    },
}


#: The dataset labels used in the run report, the log lines and the `errors`
#: list — one definition, so they cannot drift apart.
#:
#: PR-ADS-154 renamed the report keys onto the canonical source
#: (`google_ads_api/...`) but left several log and error strings spelling the
#: superseded `google_ads/...`. An operator greps the errors list and then looks
#: for that dataset in the JSON, so two spellings for one dataset is the same
#: class of defect this PR exists to remove — just in the human-facing layer.
LABEL_CANONICAL_SPEND = f"{CANONICAL_SPEND_SOURCE}/{CANONICAL_SPEND_DATASET}"
LABEL_CANONICAL_GEO = f"{CANONICAL_GEO_SOURCE}/{CANONICAL_GEO_DATASET}"
LABEL_GEO_RECONCILIATION = f"{CANONICAL_GEO_SOURCE}/geo_reconciliation"
LABEL_FX = f"{FX_SOURCE}/{FX_DAILY_RATES_DATASET}"
LABEL_DEAL_LEDGER = f"{DEAL_LEDGER_SOURCE}/{DEAL_LEDGER_DATASET}"
LABEL_SOURCE_CLASSIFICATION = (
    f"{SOURCE_CLASSIFICATION_SOURCE}/{SOURCE_CLASSIFICATION_DATASET}")
LABEL_GCLID_MATCHES = f"{GCLID_SOURCE}/{GCLID_MATCHES_DATASET}"
LABEL_KEYWORD_FACTS = f"{KEYWORD_FACTS_SOURCE}/{KEYWORD_FACTS_DATASET}"
LABEL_SEARCH_TERMS = f"{SEARCH_TERMS_SOURCE}/{SEARCH_TERMS_DATASET}"


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

#: Pairs this run SCHEDULES but whose batches are opened by the owning service
#: rather than by this module.
#:
#: PR-ADS-156 makes an exclusion that was previously only a sentence in the
#: comment above into a checkable list. `ACTIVE_SYNC_PAIRS` is enumerated by a
#: contract test that AST-scans THIS file for `start_sync_batch` calls, so a pair
#: stamped inside a service would fail it — and the temptation would be to relax
#: the test, which is the one guard standing between a mistyped key and a dataset
#: that silently reports "never run" forever.
#:
#: So they are declared separately and checked separately: still enumerable,
#: still proven registered, without weakening the assertion that this module
#: stamps exactly what it says it stamps.
SERVICE_OWNED_SYNC_PAIRS: tuple[tuple[str, str], ...] = (
    (KEYWORD_FACTS_SOURCE, KEYWORD_FACTS_DATASET),
    (SEARCH_TERMS_SOURCE, SEARCH_TERMS_DATASET),
)


# ---------------------------------------------------------------------------
# Database readiness (PR-ADS-154 §1)
# ---------------------------------------------------------------------------

#: THE canonical run type this scheduler writes to `runs.run_type`.
#:
#: PR-ADS-154A: spelled once, and deliberately NOT shortened to fit a column.
#: It is 22 characters, `runs.run_type` was VARCHAR(20), and the fix was to
#: widen the column — the value is already what scheduler output, tests,
#: monitoring and diagnostics key on, so the schema moves to the contract
#: rather than the contract to the schema.
RUN_TYPE = "daily_incremental_sync"

#: The database could not be reached at all — no pool, or the probe failed.
DB_UNAVAILABLE_REASON = "database_unavailable"

#: The database ANSWERED and then rejected the run record. PR-ADS-154A: these
#: two were one reason, and production paid for it. The readiness probe passed,
#: `write_run()` failed on `value too long for type character varying(20)`, and
#: the run reported `database_unavailable` — sending the operator to check
#: connectivity that was already fine, while the actual defect (a schema column
#: narrower than the value the application has always written) went unnamed.
#:
#: "We could not reach the database" and "the database refused this row" call
#: for completely different fixes, so they are completely different reasons.
RUN_RECORD_WRITE_FAILED_REASON = "run_record_write_failed"


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

    PR-ADS-155-F1 moved the implementation to :mod:`db.connection`, beside the
    pool it initializes, because two more standalone commands needed exactly
    this and a second copy is how two entry points come to disagree about what
    "ready" means. This wrapper keeps the name the scheduler and its tests use;
    the contract is unchanged.
    """
    from db.writers import safe_db_error  # noqa: PLC0415

    try:
        from db.connection import ensure_database_ready as _ready  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return False, f"db.connection import failed: {safe_db_error(exc)}"

    return _ready()


def _aborted_before_start(*, reason: str, run_reason: str, started_at: str,
                          detail: str | None) -> dict:
    """The structured result of a run that never started.

    Shaped like a completed run so every consumer — the CLI, the scheduler
    wrapper, the admin trigger — reads it the same way, but with no dataset
    entries at all. An empty `datasets` map is the honest report: nothing was
    attempted, so nothing may claim a status.

    ``reason`` is the machine-readable cause, and it must name what actually
    happened: see :data:`RUN_RECORD_WRITE_FAILED_REASON`. ``detail`` carries the
    human-readable specifics and is already redacted by
    :func:`db.writers.safe_db_error` when it originates from a driver error.
    """
    finished_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    message = f"{reason}: {detail}" if detail else reason
    log.error("[incremental_sync] aborting before any external pull — %s", message)
    return {
        "status": "failed",
        "execution_status": "failed",
        # PR-ADS-154B §3: a run that never started learned nothing about the
        # data, so truth is UNKNOWN — not `not_ready`, which would report a
        # finding about canonical completeness that nobody established. Present
        # on this path too, so a consumer can read the same keys from any result
        # instead of testing which shape it got.
        "truth_status": TRUTH_UNKNOWN,
        "campaign_coverage_complete": False,
        "fx_coverage_complete": False,
        "geo_coverage_complete": False,
        "comparison_like_for_like": False,
        "geo_reconciled": False,
        "geo_ready": False,
        "gap_codes": [reason],
        "reason": reason,
        "run_type": RUN_TYPE,
        "run_reason": run_reason,
        "run_id": None,
        "started_at": started_at,
        "finished_at": finished_at,
        "datasets": {},
        "errors": [message],
    }


def _database_unavailable_result(*, run_reason: str, started_at: str,
                                 detail: str | None) -> dict:
    """The run could not reach the database at all."""
    return _aborted_before_start(
        reason=DB_UNAVAILABLE_REASON, run_reason=run_reason,
        started_at=started_at, detail=detail)


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
    run_id, run_write_error = db_writers.write_run_detailed({
        "run_type": RUN_TYPE,
        "started_at": started_at,
        "status": "running",
    })
    # PR-ADS-154 §6: no run row means nothing this run writes can be attributed
    # to it, and `write_run` returns a falsy id rather than raising.
    #
    # PR-ADS-154A: the REASON is `run_record_write_failed`, not
    # `database_unavailable`. The probe above just succeeded, so the database is
    # demonstrably reachable — it answered and then rejected this row. Reporting
    # a connectivity problem here sent an operator to check a connection that
    # was already fine while the real defect (`runs.run_type` was VARCHAR(20);
    # `daily_incremental_sync` is 22 characters) went unnamed. The driver's own
    # message is carried through, redacted, because it IS the diagnosis.
    if not run_id:
        return _aborted_before_start(
            reason=RUN_RECORD_WRITE_FAILED_REASON,
            run_reason=run_reason, started_at=started_at,
            detail=(run_write_error
                    or "write_run returned no id and no error detail"))

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
    # PR-ADS-154C: stamp the non-authoritative classification onto the RESULT,
    # not just the register, so a reader of the JSON can see that a degraded or
    # rate-limited legacy scan is not a statement about revenue truth. The run
    # that motivated this had 429s here beside a clean canonical deal ledger.
    datasets["hubspot/deals"].update(LEGACY_NON_AUTHORITATIVE_DATASETS["hubspot/deals"])

    # ── gclid/matches — closed-won deals by closedate → gclid_attribution ────
    # PR-ADS-114: this dataset now has a real persistence path. It pulls
    # closed-won deals DIRECTLY by closedate and writes GCLID-only attribution
    # rows (no synthetic GCLIDs) into gclid_attribution.
    datasets[LABEL_GCLID_MATCHES] = _sync_gclid_attribution(
        run_id=run_id, date_from=date_from_deals, date_to=today, errors=errors,
    )

    # ── hubspot/deal_ledger — PR-ADS-153E-A canonical deal ledger (shadow).
    # Reads HubSpot read-only across ALL tracked pipeline stages and writes only
    # the local canonical ledger. No page consumes it yet: 153E-A populates and
    # reconciles it, 153E-B migrates consumers. Orchestration only — every won,
    # currency, association and attribution decision lives in the service and
    # its pure rule modules, never here.
    datasets[LABEL_DEAL_LEDGER] = _sync_deal_ledger(
        run_id=run_id, errors=errors,
    )

    # ── hubspot/source_classification — keep acquisition-source classification
    # current (PR-ADS-117): classify newly-created contacts (all sources) and
    # attribute recent closed-won deals. Read-only from HubSpot; local DB only.
    datasets[LABEL_SOURCE_CLASSIFICATION] = _sync_source_classification(
        run_id=run_id, date_from=date_from_contacts, date_to=today, errors=errors,
    )

    # ── google_ads_api/canonical_spend — keep canonical campaign-daily spend current
    # (PR-ADS-118) with a small daily lookback for late Google Ads adjustments.
    # Reads Google Ads read-only; writes only local canonical tables.
    datasets[LABEL_CANONICAL_SPEND] = _sync_canonical_spend(
        run_id=run_id, date_to=today, errors=errors,
    )

    # ── fx/daily_rates — keep daily GBP→USD FX rates current (PR-ADS-119) so
    # native spend can be converted to USD reporting spend per spend_date. Reads
    # published reference rates read-only; writes only the local fx_rates table.
    datasets[LABEL_FX] = _sync_fx_rates(
        run_id=run_id, date_to=today, errors=errors,
    )

    # ── google_ads_api/canonical_geo — PR-ADS-153F. Country ROAS needs per-country
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
    datasets[LABEL_CANONICAL_GEO] = _sync_canonical_geo(
        run_id=run_id, date_to=today, errors=errors,
    )

    # ── google_ads_api/geo_reconciliation — step 4. Read-only diagnostics computed
    # AFTER spend, FX and geo are current, so the recorded verdict describes this
    # run's data rather than the previous one's. Publishes no external state and
    # never gates the sync: a reconciliation that reports "blocked" is a true
    # answer about the data, not a sync failure.
    datasets[LABEL_GEO_RECONCILIATION] = _publish_geo_reconciliation(
        errors=errors,
    )

    # ── google_ads_api/keyword_facts + google_ads_api/search_terms — PR-ADS-156.
    # The two Platform Evidence datasets. Until this PR neither was refreshed by
    # the primary incremental run: keyword facts waited for the weekly/monthly
    # scheduler and search terms for one of three schedulers that each had their
    # own rules, so a successful daily run proved nothing about either page.
    #
    # Placed AFTER canonical spend and FX deliberately. Both are read-only pulls
    # that write only their own tables, so they cannot affect the executive
    # contracts above; running them last means an evidence outage can never
    # delay the spend, FX and geo chain that revenue truth depends on.
    datasets[LABEL_KEYWORD_FACTS] = _sync_keyword_facts(run_id=run_id, errors=errors)
    datasets[LABEL_SEARCH_TERMS] = _sync_search_terms(run_id=run_id, errors=errors)

    # ── mailchimp — read-only email-marketing refresh (PR-ADS-151). Pulls recent
    # campaigns + refreshes recent reports + snapshots audiences. Skipped cleanly
    # when Mailchimp is not configured. GET-only; local DB writes only.
    datasets["mailchimp/refresh"] = _sync_mailchimp(run_id=run_id, errors=errors)

    finished_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    overall_status = _overall_status(datasets)
    # PR-ADS-154B §3: execution health and truth readiness are different
    # questions and are now answered separately. `status` keeps its meaning
    # exactly — every dataset that ran, ran cleanly — so nothing downstream that
    # reads it changes behaviour.
    truth = build_truth_block(datasets)
    # PR-ADS-156 §6: a THIRD question, answered separately again. Executive
    # truth is untouched by it — see `build_evidence_block`.
    evidence = build_evidence_block(datasets)

    # Reflect the true final state on the local run record.
    db_writers.update_run(run_id, {
        "finished_at": finished_at,
        "status": "success" if overall_status in ("success", "partial") else "failed",
        "error_message": "; ".join(errors)[:1000] if errors else None,
    })

    summary = {
        "status": overall_status,
        # The same verdict under an unambiguous name. `status` is retained
        # because callers, the CLI exit code and the deployment checks all read
        # it; `execution_status` exists so a reader never has to guess which of
        # the two questions it was answering.
        "execution_status": overall_status,
        **truth,
        **evidence,
        "run_type": RUN_TYPE,
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
        "[incremental_sync] finished execution_status=%s truth_status=%s "
        "evidence_status=%s geo_ready=%s gaps=%s evidence_gaps=%s "
        "datasets=%s errors=%d",
        overall_status, truth["truth_status"], evidence["evidence_status"],
        truth["geo_ready"], truth["gap_codes"], evidence["evidence_gap_codes"],
        list(datasets.keys()), len(errors),
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
        batch_id = _require_batch(batch_id, LABEL_GCLID_MATCHES)
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
        err = f"{LABEL_GCLID_MATCHES}: {exc}"
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
        batch_id = _require_batch(batch_id, LABEL_DEAL_LEDGER)
        from services.hubspot_deal_sync_service import sync_deals  # noqa: PLC0415

        result = sync_deals(batch_id=batch_id)
    except Exception as exc:  # noqa: BLE001
        log.error("[incremental] deal ledger sync failed: %s", exc, exc_info=True)
        errors.append(f"{LABEL_DEAL_LEDGER}: {exc}")
        db_writers.finish_sync_batch(batch_id, status="failed",
                                     error_message=str(exc))
        return {"status": "failed", "error": str(exc), "rows": 0}

    status = result.get("status") or "failed"
    if status != "success":
        errors.append(
            f"{LABEL_DEAL_LEDGER}: {status} "
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
        batch_id = _require_batch(batch_id, LABEL_SOURCE_CLASSIFICATION)
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
        err = f"{LABEL_SOURCE_CLASSIFICATION}: {exc}"
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
        batch_id = _require_batch(batch_id, LABEL_CANONICAL_SPEND)
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
        err = f"{LABEL_CANONICAL_SPEND}: {exc}"
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


# ── PR-ADS-156 §2 — the two Platform Evidence datasets ──────────────────────
# Both delegate to their owning service rather than pulling and writing here.
# This module's job is to SCHEDULE them and to disclose what happened; the rules
# for what a successful sync means live once, next to the writer, so a second
# definition cannot appear in a scheduler.
#
# The disclosure below is deliberately uniform across both datasets: an operator
# reading the run report should not have to learn two vocabularies to answer the
# same question about two datasets.

def _evidence_dataset_result(label: str, stats: dict, *, requested_from,
                             requested_to, errors: list) -> dict:
    """Shape one evidence service's stats into the run report's dataset entry.

    ``verified_empty`` travels as its own field rather than being inferred from
    ``rows_written == 0`` downstream. A failed pull writes zero rows too, and
    the entire point of the flag is that those two are not the same event.
    """
    ok = bool(stats.get("ok"))
    status = "success" if ok else "failed"
    if not ok:
        err = f"{label}: {stats.get('error') or 'evidence sync failed'}"
        errors.append(err)
        log.warning("[incremental_sync] %s", err)
    return {
        "status": status,
        "source": stats.get("source"),
        "dataset": stats.get("dataset"),
        "date_from": stats.get("date_from") or str(requested_from),
        "date_to": stats.get("date_to") or str(requested_to),
        "rows_fetched": stats.get("fetched", 0),
        "rows_prepared": stats.get("prepared", 0),
        "rows_written": stats.get("written", 0),
        "rows_rejected": stats.get("rejected", stats.get("skipped", 0)),
        "rejected_reasons": stats.get("rejected_reasons") or {},
        "latest_source_date": stats.get("latest_source_date"),
        "sync_batch_id": stats.get("batch_id"),
        # A successful query over an interval with no eligible rows. Success,
        # and NOT the same as a failure that also returned nothing.
        "verified_empty": bool(stats.get("verified_empty")),
        # PR-ADS-156-F2 §2 — whether the batch row itself was finalized, and
        # what may nevertheless be in the table if it was not. `rows_written`
        # above is what this run CERTIFIES; these two say what happened when the
        # rows landed but the certificate did not, which is the one failure the
        # data alone can never reveal.
        "batch_finalized": bool(stats.get("batch_finalized")),
        "rows_possibly_written": stats.get("rows_possibly_written",
                                           stats.get("written", 0)),
        "db_unavailable": bool(stats.get("db_unavailable")),
        "error": (stats.get("error") or None) if not ok else None,
        # This dataset's own verdict, so a reader never has to derive
        # authority from where the entry happens to sit in the report.
        "authoritative": True,
        # Read-only against Google Ads. Stated on every run, successful or not.
        "external_writes_performed": False,
    }


def _sync_keyword_facts(*, run_id, errors: list) -> dict:
    """Refresh durable ``keyword_daily_facts`` on the established correction window.

    PR-ADS-156 §3. Delegates to `keyword_sync_service` — the same function the
    weekly, monthly, bootstrap and admin-refresh paths already call. No second
    keyword writer is created here, and the 30-day window is the service's own
    ``DEFAULT_INCREMENTAL_DAYS`` rather than a number chosen again in this file.
    """
    from services.keyword_sync_service import (  # noqa: PLC0415
        DEFAULT_INCREMENTAL_DAYS, sync_recent_keyword_facts,
    )

    try:
        stats = sync_recent_keyword_facts(
            "daily", days=DEFAULT_INCREMENTAL_DAYS, run_id=run_id)
    except Exception as exc:  # noqa: BLE001
        err = f"{LABEL_KEYWORD_FACTS}: {exc}"
        errors.append(err)
        log.warning("[incremental_sync] %s", err)
        return {"status": "failed", "error": str(exc)[:500],
                "verified_empty": False, "external_writes_performed": False,
                "authoritative": True}
    return _evidence_dataset_result(
        LABEL_KEYWORD_FACTS, stats,
        requested_from=stats.get("date_from"), requested_to=stats.get("date_to"),
        errors=errors)


def _sync_search_terms(*, run_id, errors: list) -> dict:
    """Refresh durable ``search_terms`` on a rolling recovery window.

    PR-ADS-156 §4. Delegates to the shared canonical search-term service, which
    is now also what the daily, weekly and monthly schedulers call. The window is
    wider than the interval between runs, so a missed day is recovered by the
    next run instead of leaving a permanent hole; the write is an upsert on the
    natural key, so overlapping runs cost time and not correctness.
    """
    from services.search_term_sync_service import (  # noqa: PLC0415
        DEFAULT_LOOKBACK_DAYS, sync_recent_search_terms,
    )

    try:
        stats = sync_recent_search_terms(
            "daily", days=DEFAULT_LOOKBACK_DAYS, run_id=run_id)
    except Exception as exc:  # noqa: BLE001
        err = f"{LABEL_SEARCH_TERMS}: {exc}"
        errors.append(err)
        log.warning("[incremental_sync] %s", err)
        return {"status": "failed", "error": str(exc)[:500],
                "verified_empty": False, "external_writes_performed": False,
                "authoritative": True}
    return _evidence_dataset_result(
        LABEL_SEARCH_TERMS, stats,
        requested_from=stats.get("date_from"), requested_to=stats.get("date_to"),
        errors=errors)


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
        batch_id = _require_batch(batch_id, LABEL_FX)
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
        err = f"{LABEL_FX}: {exc}"
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
                f"{LABEL_CANONICAL_GEO}: sync batch could not be opened")
        db_writers.finish_sync_batch(batch_id=bid, status=status, **fields)

    try:
        result = run_google_ads_geo_sync(
            date_from=str(start), date_to=str(date_to), dry_run=False,
            job_id=str(run_id) if run_id else None,
        )
    except Exception as exc:  # noqa: BLE001
        err = f"{LABEL_CANONICAL_GEO}: {exc}"
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
        log.info("[incremental_sync] %s: skipped (lease held)", LABEL_CANONICAL_GEO)
        return {"status": "skipped", "reason": result.get("reason"),
                "note": "another canonical geo sync was already running"}

    summary = result.get("summary") or {}
    if result.get("reason") == "lease_store_unavailable":
        err = (f"{LABEL_CANONICAL_GEO}: failed (lease_store_unavailable) — the "
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
        errors.append(f"{LABEL_CANONICAL_GEO}: {status} "
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
        log.info("[incremental_sync] %s: status=%s "
                 "country_spend_status=%s missing_dates=%d",
                 LABEL_GEO_RECONCILIATION, recon_status,
                 recon.get("country_spend_status"),
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
            # PR-ADS-154B §3 — the three coverage facts behind the verdict, so a
            # reader can see WHICH input is short without opening the database.
            # `mismatch` alone never said whether the totals genuinely disagree
            # or whether one of them was measured over a half-fetched range.
            "campaign_coverage_complete": recon.get("coverage_status") == "complete",
            "fx_coverage_complete": recon.get("fx_coverage_status") == "complete",
            "geo_coverage_complete": bool(recon.get("geo_coverage_complete")),
            # PR-ADS-154B §2.
            "comparison_like_for_like": bool(recon.get("comparison_like_for_like")),
            "scope_customer_id": recon.get("scope_customer_id"),
        }
        if result["status"] == "failed":
            err = (f"{LABEL_GEO_RECONCILIATION}: could not be evaluated "
                   f"(status={recon_status}, gaps={result['geo_gap_codes']})")
            result["error"] = err
            errors.append(err)
            log.warning("[incremental_sync] %s", err)
        return result
    except Exception as exc:  # noqa: BLE001
        err = f"{LABEL_GEO_RECONCILIATION}: {exc}"
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


#: Truth-readiness verdicts. `ready` means the canonical dataset is complete AND
#: reconciled; `not_ready` means it demonstrably is not; `unknown` means the
#: evaluation could not be performed, which is not the same as either.
TRUTH_READY = "ready"
TRUTH_NOT_READY = "not_ready"
TRUTH_UNKNOWN = "unknown"

#: PR-ADS-154B-F1. Published when truth is anything other than READY and nothing
#: else could say why. A non-ready verdict with an empty gap list is unactionable
#: — the reader is told to fix something and not told what — so an inconsistency
#: in the inputs surfaces as its own code rather than as silence.
#:
#: The guard covers ``unknown`` as well as ``not_ready``, though only the latter
#: can reach it today: an unevaluated reconciliation is already given a reason
#: earlier in :func:`build_truth_block`. It is written to the broader condition
#: deliberately, so the invariant holds if that earlier branch ever changes.
GAP_NOT_READY_WITHOUT_REASON = "geo_truth_not_ready_without_reason"


# ── PR-ADS-156 §6 — evidence readiness, kept OUT of executive truth ─────────
#: Gap codes for the Platform Evidence datasets. Deliberately their own
#: vocabulary: a keyword pull that failed says nothing about whether the HubSpot
#: revenue ledger or the canonical campaign-spend contract is usable, and folding
#: it into `gap_codes` would make it look as though it did.
EVIDENCE_GAP_KEYWORD_FACTS = "keyword_facts_refresh_failed"
EVIDENCE_GAP_SEARCH_TERMS = "search_terms_refresh_failed"
EVIDENCE_GAP_NOT_RUN = "evidence_refresh_did_not_run"

EVIDENCE_READY = "ready"
EVIDENCE_PARTIAL = "partial"
EVIDENCE_NOT_READY = "not_ready"


def build_evidence_block(datasets: dict) -> dict:
    """Did the two Platform Evidence datasets refresh? A separate question.

    PR-ADS-156 §6. Keyword and search-term freshness must be visible when it
    fails — that is the whole point of scheduling them — but it must not
    redefine executive truth. A search-term outage does not make closed-won
    revenue unusable, and reporting it as `truth_status: not_ready` would be a
    false alarm on the figure the business actually steers by, while burying the
    true alarm about the page that IS broken.

    The converse matters just as much: a failed evidence refresh is never
    suppressed because the executive contracts happen to be fine. Both states
    are published, side by side, each answering its own question.

    ``not_ready`` and ``partial`` are distinct: one dataset down is a page out of
    date, both down is the evidence layer out of date, and an operator triages
    those differently.
    """
    entries = {
        EVIDENCE_GAP_KEYWORD_FACTS: datasets.get(LABEL_KEYWORD_FACTS),
        EVIDENCE_GAP_SEARCH_TERMS: datasets.get(LABEL_SEARCH_TERMS),
    }
    gaps, ok_count, present = [], 0, 0
    for gap_code, entry in entries.items():
        if not isinstance(entry, dict):
            # The run never reached this dataset. Reported as its own gap rather
            # than as a failure: nobody looked, which is not the same as looking
            # and finding nothing.
            gaps.append(EVIDENCE_GAP_NOT_RUN)
            continue
        present += 1
        if entry.get("status") == "success":
            ok_count += 1
        else:
            gaps.append(gap_code)

    if present == 0:
        status = EVIDENCE_NOT_READY
    elif ok_count == present and not gaps:
        status = EVIDENCE_READY
    elif ok_count == 0:
        status = EVIDENCE_NOT_READY
    else:
        status = EVIDENCE_PARTIAL

    return {
        "evidence_status": status,
        "evidence_gap_codes": sorted(set(gaps)),
        "evidence_datasets": {
            LABEL_KEYWORD_FACTS: (datasets.get(LABEL_KEYWORD_FACTS) or {}).get("status"),
            LABEL_SEARCH_TERMS: (datasets.get(LABEL_SEARCH_TERMS) or {}).get("status"),
        },
        # Stated here as well as per dataset: whatever happened to the evidence
        # refresh, nothing was written to Google Ads.
        "evidence_external_writes_performed": False,
    }


def build_truth_block(datasets: dict) -> dict:
    """Summarise whether the canonical dataset is TRUE, not whether the run WORKED.

    PR-ADS-154B §3. These were one field. ``status`` votes on per-dataset
    execution — did each step run without an operational error — and geo
    reconciliation deliberately reports ``success`` when it performs the
    comparison and the totals disagree, because an honest disagreement is a
    working step reporting a real answer.

    Both of those are right, and together they produced ``status: success`` on a
    run whose country spend was ``unavailable``, whose campaign and FX coverage
    were incomplete, and whose geo did not reconcile. Nothing lied; the question
    "did the pipeline run?" was simply being read as "is the data usable?".

    So the run now answers both, separately. ``execution_status`` keeps its exact
    meaning. ``truth_status`` is new and answers the second question.

    ``unknown`` is a real third state: a run that never reached the
    reconciliation step knows nothing about the data, and reporting that as
    ``not_ready`` would claim a finding nobody made.

    PR-ADS-154B-F1 — readiness DEFERS to the shared gate; it does not re-derive it
    ------------------------------------------------------------------------------
    The first version of this function required ``reconciled`` — exact equality of
    the two totals — on top of the shared gate's ``geo_ready``. That is precisely
    the mistake PR-ADS-153F blocker 1 removed one layer down, reintroduced one
    layer up, and the docstring above it claimed the opposite was happening.

    ``country_geo_ready`` accepts ``reconciled_with_residual``: the PR-ADS-131
    case where Google Ads' geographic view does not assign some spend to any
    country, the shortfall is surfaced as an explicit residual bucket, and every
    coverage, scope and completeness condition passes. The totals genuinely do
    not match, by design, and the country figures are still safe to use.
    Requiring exact equality here overrode that verdict and produced the
    incoherent state this fix exists to remove::

        country_spend_status: reconciled_with_residual   geo_ready: true   (dataset)
        truth_status: not_ready                          geo_ready: false  (top level)
        gap_codes: []            <- blocked, with nothing to act on

    So ``reconciled`` stays exactly what it was — a fact about whether the raw
    totals matched, published unchanged, False for an accepted residual, because
    pretending otherwise would be the opposite error. It simply stops being a
    PRECONDITION of readiness. Readiness is: the comparison happened, at a
    like-for-like scope, over complete coverage on all three inputs, and the
    shared gate says the country denominator is usable, with no gaps outstanding.
    """
    recon = datasets.get(LABEL_GEO_RECONCILIATION) or {}
    evaluated = bool(recon.get("available"))

    campaign_ok = bool(recon.get("campaign_coverage_complete"))
    fx_ok = bool(recon.get("fx_coverage_complete"))
    geo_cov_ok = bool(recon.get("geo_coverage_complete"))
    like_for_like = bool(recon.get("comparison_like_for_like"))
    reconciled = bool(recon.get("reconciled"))
    # THE shared gate's verdict — `country_geo_ready(country_spend_status)`,
    # computed once in google_ads_geo_sync_service and passed through. Read, not
    # recomputed: two implementations of "is this usable" is how the same window
    # became ready on one surface and blocked on another.
    geo_ready = bool(recon.get("geo_ready"))

    gap_codes = list(recon.get("geo_gap_codes") or [])
    if not evaluated and not gap_codes:
        # The step did not produce a verdict at all. Say which, rather than
        # publishing an empty gap list that reads like "no problems found".
        gap_codes = [recon.get("reason") or "geo_reconciliation_not_evaluated"]

    if not evaluated:
        truth_status = TRUTH_UNKNOWN
    elif (geo_ready and like_for_like and campaign_ok and fx_ok
            and geo_cov_ok and not gap_codes):
        truth_status = TRUTH_READY
    else:
        truth_status = TRUTH_NOT_READY

    # PR-ADS-154B-F1: ANY non-ready verdict must carry something to act on —
    # `not_ready` and `unknown` alike, which is why this tests `!= TRUTH_READY`
    # rather than the narrower `== TRUTH_NOT_READY`. In practice only
    # `not_ready` can arrive here with an empty list, because an unevaluated
    # reconciliation was already given a reason above; the broader condition is
    # a backstop for a future change to that branch, not a live second path.
    #
    # Every path that blocks has a reason; if the inputs ever combine so that
    # none of them was recorded, that is itself the finding, and it is published
    # rather than left as an empty list a reader would take for "no problems
    # found". No credentials or connection details are involved — these are the
    # gate's own machine codes and three booleans.
    if truth_status != TRUTH_READY and not gap_codes:
        gap_codes = [GAP_NOT_READY_WITHOUT_REASON]
        log.warning(
            "[incremental_sync] truth is %s but the reconciliation reported no "
            "gap codes — publishing %s. geo_ready=%s like_for_like=%s "
            "campaign_coverage=%s fx_coverage=%s geo_coverage=%s",
            truth_status, GAP_NOT_READY_WITHOUT_REASON, geo_ready, like_for_like,
            campaign_ok, fx_ok, geo_cov_ok)

    return {
        "truth_status": truth_status,
        "campaign_coverage_complete": campaign_ok,
        "fx_coverage_complete": fx_ok,
        "geo_coverage_complete": geo_cov_ok,
        "comparison_like_for_like": like_for_like,
        # Exact equality of the two totals, unchanged and still observable. False
        # for an accepted residual — which is the honest answer, and no longer
        # the same question as "is this usable".
        "geo_reconciled": reconciled,
        # Agrees with `truth_status` by construction, so the two can never
        # disagree in a published payload.
        "geo_ready": truth_status == TRUTH_READY,
        "gap_codes": [] if truth_status == TRUTH_READY else gap_codes,
    }


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
