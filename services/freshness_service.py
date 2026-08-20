"""
services/freshness_service.py

PR-ADS-067 — Canonical Freshness Semantics & Zero-Row Truth States

Provides the canonical freshness status model used across the platform.
Every dataset is assigned one unambiguous truth state so the UI never
displays a generic "Fresh" badge when the underlying data is empty,
stale, failed, or blocked by a dependency.

Phase 1 — Read Only.  No external writes.
"""

from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
from typing import Any

# PR-ADS-153F: dataset keys come from the shared registry, never spelled here.
# A config that spells its own key can drift from the writer that stamps it, and
# then the dataset has no freshness signal while looking perfectly configured.
from services.dataset_keys import (
    CANONICAL_GEO_DATASET as _GEO_SYNC_DATASET,
    CANONICAL_GEO_SOURCE as _GEO_SYNC_SOURCE,
    CANONICAL_SPEND_DATASET as _SPEND_DATASET,
    CANONICAL_SPEND_SOURCE as _SPEND_SOURCE,
)


# ── Canonical Status Constants ──────────────────────────────────────────────

class CanonicalFreshnessStatus:
    """Enumeration of all possible canonical freshness states.

    PR-ADS-095 refined the model so System Status can separate three signals
    that were previously collapsed into ``failed``:
      - ``data_available_latest_sync_failed`` — rows exist, just the last sync failed
      - ``failed_no_data`` — sync failed and no usable rows are available
      - ``not_run_but_derivable`` — derived dataset hasn't run but upstream has rows
    """

    FRESH_WITH_DATA = "fresh_with_data"
    FRESH_BUT_EMPTY = "fresh_but_empty"
    STALE_WITH_DATA = "stale_with_data"
    STALE_AND_EMPTY = "stale_and_empty"
    FAILED = "failed"
    RUNNING = "running"
    NOT_RUN = "not_run"
    DEPENDENCY_BLOCKED = "dependency_blocked"
    DB_UNAVAILABLE = "db_unavailable"
    UNKNOWN = "unknown"

    # PR-ADS-095 refined states
    DATA_AVAILABLE_LATEST_SYNC_FAILED = "data_available_latest_sync_failed"
    FAILED_NO_DATA = "failed_no_data"
    NOT_RUN_BUT_DERIVABLE = "not_run_but_derivable"
    NOT_RUN_NO_UPSTREAM_DATA = "not_run_no_upstream_data"
    UNKNOWN_ROW_COUNT = "unknown_row_count"
    ROW_COUNT_NOT_ENABLED = "row_count_not_enabled"
    BLOCKED_BY_DEPENDENCY = "blocked_by_dependency"
    EMPTY_SUCCESS = "empty_success"

    ALL = [
        FRESH_WITH_DATA,
        FRESH_BUT_EMPTY,
        STALE_WITH_DATA,
        STALE_AND_EMPTY,
        FAILED,
        RUNNING,
        NOT_RUN,
        DEPENDENCY_BLOCKED,
        DB_UNAVAILABLE,
        UNKNOWN,
        DATA_AVAILABLE_LATEST_SYNC_FAILED,
        FAILED_NO_DATA,
        NOT_RUN_BUT_DERIVABLE,
        NOT_RUN_NO_UPSTREAM_DATA,
        UNKNOWN_ROW_COUNT,
        ROW_COUNT_NOT_ENABLED,
        BLOCKED_BY_DEPENDENCY,
        EMPTY_SUCCESS,
    ]


# ── Severity Levels ─────────────────────────────────────────────────────────

SEVERITY_MAP: dict[str, str] = {
    CanonicalFreshnessStatus.FRESH_WITH_DATA: "ok",
    CanonicalFreshnessStatus.FRESH_BUT_EMPTY: "warning",
    CanonicalFreshnessStatus.STALE_WITH_DATA: "warning",
    CanonicalFreshnessStatus.STALE_AND_EMPTY: "error",
    CanonicalFreshnessStatus.FAILED: "error",
    CanonicalFreshnessStatus.RUNNING: "neutral",
    CanonicalFreshnessStatus.NOT_RUN: "neutral",
    CanonicalFreshnessStatus.DEPENDENCY_BLOCKED: "warning",
    CanonicalFreshnessStatus.DB_UNAVAILABLE: "error",
    CanonicalFreshnessStatus.UNKNOWN: "neutral",
    # PR-ADS-095 refined states
    CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED: "warning",
    CanonicalFreshnessStatus.FAILED_NO_DATA: "error",
    CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE: "warning",
    CanonicalFreshnessStatus.NOT_RUN_NO_UPSTREAM_DATA: "error",
    CanonicalFreshnessStatus.UNKNOWN_ROW_COUNT: "neutral",
    CanonicalFreshnessStatus.ROW_COUNT_NOT_ENABLED: "neutral",
    CanonicalFreshnessStatus.BLOCKED_BY_DEPENDENCY: "error",
    CanonicalFreshnessStatus.EMPTY_SUCCESS: "warning",
}


# ── Has-data states ─────────────────────────────────────────────────────────
# States where usable rows exist in the selected window. Used to refine
# downstream derived states (e.g. NOT_RUN_BUT_DERIVABLE).
HAS_DATA_STATES = frozenset([
    CanonicalFreshnessStatus.FRESH_WITH_DATA,
    CanonicalFreshnessStatus.STALE_WITH_DATA,
    CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
])


# ── Dataset Configuration ───────────────────────────────────────────────────

DATASET_FRESHNESS_CONFIG: dict[str, dict[str, Any]] = {
    "campaigns": {
        "table": "campaigns",
        "date_column": "run_date",
        "source": "google_ads_api",
        "dataset": "campaigns",
        "stale_threshold_days": 8,
        "depends_on": [],
        "page": "campaigns",
    },
    # PR-ADS-143: the Campaign Evidence page reads canonical daily spend (not the
    # `campaigns` snapshot), so its freshness tracks the real upstream table.
    #
    # PR-ADS-153F found the writer stamping `source="google_ads"` while this
    # config expected `google_ads_api`, so the lookup matched nothing and the
    # ROAS denominator had NO working freshness signal (PR-ADS-153A §1.7). That
    # PR moved the config to the writer's spelling.
    #
    # PR-ADS-154 removes the choice entirely: `google_ads` and `google_ads_api`
    # were two names for one source, and keeping both is what let them drift.
    # There is now ONE key — `google_ads_api` — canonicalized at the writer
    # boundary, with the historical `google_ads` rows relabelled by an
    # idempotent migration so no accumulated history is orphaned. Both sides
    # import the key from services/dataset_keys, so neither can move alone.
    "canonical_spend": {
        "table": "google_ads_campaign_daily_spend",
        "date_column": "spend_date",
        "source": _SPEND_SOURCE,
        "dataset": _SPEND_DATASET,
        "stale_threshold_days": 8,
        "depends_on": [],
        "page": "campaigns",
    },
    # PR-ADS-153F: canonical per-country Google Ads spend. `source`/`dataset` are
    # imported from the ONE owner of this dataset
    # (services.google_ads_geo_sync_service.GEO_SYNC_SOURCE / GEO_SYNC_DATASET)
    # rather than spelled here, so the key cannot drift away from the writer the
    # way canonical_spend's did. Depends on canonical_spend because geo is
    # meaningless until there is a campaign total to reconcile it against.
    "canonical_geo": {
        "table": "google_ads_geo_daily_spend",
        "date_column": "spend_date",
        "source": _GEO_SYNC_SOURCE,
        "dataset": _GEO_SYNC_DATASET,
        "stale_threshold_days": 3,
        "depends_on": ["canonical_spend"],
        "page": "countries",
    },
    # PR-ADS-153B: canonical CRM funnel spine. `source`/`dataset` MUST equal the
    # keys the ingestion service stamps on its sync batches
    # (services/hubspot_contact_funnel_sync_service.SYNC_SOURCE / DATASET_*),
    # otherwise the dataset silently has no freshness signal — the defect class
    # the PR-ADS-153A audit found on canonical_spend.
    "contact_funnel": {
        "table": "hubspot_contact_funnel",
        "date_column": "last_modified_at",
        "source": "hubspot",
        "dataset": "contact_funnel",
        "stale_threshold_days": 2,
        "depends_on": [],
        "page": None,
    },
    # Stage-entry evidence recency. Same table, but the date column is the newest
    # lifecycle transition held — so a contact sync that is running while HubSpot
    # stage evidence has gone stale is still visible.
    "lifecycle_events": {
        "table": "hubspot_contact_funnel",
        "date_column": "latest_stage_entry_at",
        "source": "hubspot",
        "dataset": "lifecycle_events",
        "stale_threshold_days": 8,
        "depends_on": ["contact_funnel"],
        "page": None,
    },
    "search_terms": {
        "table": "search_terms",
        "date_column": "source_date",
        "source": "google_ads_api",
        "dataset": "search_terms",
        "stale_threshold_days": 8,
        "depends_on": [],
        "page": "search-terms",
    },
    "waste_terms": {
        "table": "waste_terms",
        "date_column": "run_date",
        "source": "analysis",
        "dataset": "waste_terms",
        "stale_threshold_days": 8,
        "depends_on": ["search_terms"],
        "page": "waste",
    },
    # PR-ADS-153F removed the "ngrams" entry. N-grams are computed on demand from
    # `search_terms`; there is no n-gram table, no writer and no sync batch, so
    # `(computed, ngrams)` matched no sync_state row and the dataset reported
    # "never run" permanently while the page worked fine. A freshness row with no
    # durable source is not evidence — the N-Gram page's real dependency is
    # `search_terms`, which has its own entry above.
    "keywords": {
        "table": "keywords",
        "date_column": "run_date",
        "source": "google_ads_api",
        "dataset": "keywords",
        "stale_threshold_days": 8,
        "depends_on": [],
        "page": None,   # legacy snapshot — audit-only; not a page dependency
    },
    "keyword_facts": {
        "table": "keyword_daily_facts",
        "date_column": "source_date",
        "source": "google_ads_api",
        "dataset": "keyword_facts",
        "stale_threshold_days": 8,
        "depends_on": [],
        "page": "keywords",   # PR-ADS-146: Keyword Evidence depends on durable facts
    },
    "geo": {
        "table": "geo",
        "date_column": "run_date",
        "source": "google_ads_api",
        "dataset": "geo",
        "stale_threshold_days": 8,
        "depends_on": [],
        "page": "geo",
    },
    "leads": {
        "table": "leads",
        "date_column": "created_at",
        "source": "hubspot",
        "dataset": "contacts",
        "stale_threshold_days": 8,
        "depends_on": [],
        "page": "leads",
    },
    "deals": {
        "table": "deals",
        "date_column": "close_date",
        "source": "hubspot",
        "dataset": "deals",
        "stale_threshold_days": 8,
        "depends_on": [],
        "page": "deals",
    },
    "gclid_attribution": {
        "table": "gclid_attribution",
        "date_column": "matched_at",
        "source": "gclid",
        "dataset": "matches",
        "stale_threshold_days": 8,
        "depends_on": [],
        "page": "gclid-attribution",
    },
    # PR-ADS-153F: this dataset is real (durable table + a real writer,
    # `db_writers.write_gclid_coverage_snapshot`), but nothing stamped a
    # `(gclid, coverage_snapshots)` sync batch, so it reported "never run"
    # forever while the table filled up normally. It is CONNECTED rather than
    # removed: scheduler/weekly.py and scheduler/monthly.py now open a batch
    # under this exact key. Aliasing it onto `(gclid, matches)` was rejected —
    # two configs sharing one (source, dataset) pair collide on the
    # `source/dataset` key the freshness endpoint reports under.
    "gclid_coverage_snapshots": {
        "table": "gclid_coverage_snapshots",
        "date_column": "snapshot_date",
        "source": "gclid",
        "dataset": "coverage_snapshots",
        "stale_threshold_days": 8,
        "depends_on": [],
        "page": "gclid-attribution",
    },
    # PR-ADS-153F removed the "historical_intelligence" entry. It named a table
    # (`historical_intelligence`) that does not exist in db/schema.py and is
    # created by nothing, with a `(analysis, historical_intelligence)` key no
    # writer stamps — so both its row-count query and its sync lookup were
    # guaranteed to fail. `analysis/historical_intelligence.py` computes campaign
    # trends on demand from tables that have their own freshness entries.
    # PR-ADS-151: Mailchimp read-only email-marketing evidence datasets.
    "mailchimp_campaigns": {
        "table": "mailchimp_campaigns",
        "date_column": "send_time",
        "source": "mailchimp",
        "dataset": "campaigns",
        "stale_threshold_days": 8,
        "depends_on": [],
        "page": "mailchimp",
    },
    "mailchimp_reports": {
        "table": "mailchimp_campaign_reports",
        "date_column": "last_report_update",
        "source": "mailchimp",
        "dataset": "reports",
        "stale_threshold_days": 8,
        "depends_on": [],
        "page": "mailchimp",
    },
    "mailchimp_audiences": {
        "table": "mailchimp_audience_snapshots",
        "date_column": "snapshot_date",
        "source": "mailchimp",
        "dataset": "audiences",
        "stale_threshold_days": 8,
        "depends_on": [],
        "page": "mailchimp",
    },
    # PR-ADS-153F removed the "mailchimp_attribution" entry — the third phantom.
    # Attribution feasibility is computed on demand by /api/mailchimp/audit from
    # `mailchimp_campaign_reports`; it has no table and no writer of its own, and
    # `(mailchimp, attribution)` is stamped by nothing, so it could only ever
    # report "never run". Its real evidence is the `mailchimp_reports` entry
    # above, which tracks the same table under the key its writer actually
    # stamps. (Pointing this entry at `reports` was rejected: two configs sharing
    # one (source, dataset) pair collide on the freshness endpoint's dataset key.)
}

# States that block dependents (upstream is unusable for downstream derivation)
BLOCKING_STATES = frozenset([
    CanonicalFreshnessStatus.FRESH_BUT_EMPTY,
    CanonicalFreshnessStatus.FAILED,
    CanonicalFreshnessStatus.FAILED_NO_DATA,
    CanonicalFreshnessStatus.DB_UNAVAILABLE,
    CanonicalFreshnessStatus.STALE_AND_EMPTY,
    CanonicalFreshnessStatus.NOT_RUN,
    CanonicalFreshnessStatus.NOT_RUN_NO_UPSTREAM_DATA,
    CanonicalFreshnessStatus.BLOCKED_BY_DEPENDENCY,
])


# ── Core Computation ────────────────────────────────────────────────────────

def compute_canonical_freshness(
    *,
    dataset: str,
    rows_in_window: int | None,
    latest_source_date: date | None,
    sync_status: str | None,
    latest_batch_status: str | None,
    latest_batch_row_count: int | None,
    last_successful_sync_at: datetime | None,
    stale_threshold_days: int,
    dependency_status: str | None = None,
    row_count_supported: bool | None = None,
) -> dict[str, Any]:
    """Compute the canonical freshness verdict for a single dataset.

    Pure function — no DB access, no side effects.

    Returns a dict with:
        canonical_status, severity, reason, next_action

    PR-ADS-095: ``row_count_supported`` lets callers signal whether the dataset
    is configured for row-count queries. When False, an absent ``rows_in_window``
    yields ROW_COUNT_NOT_ENABLED instead of UNKNOWN_ROW_COUNT.
    """

    downstream_not_run = sync_status is None and latest_batch_status is None

    # 1. Dependency blocked (PR-ADS-095: emit BLOCKED_BY_DEPENDENCY for actively
    #    broken upstream; emit NOT_RUN_NO_UPSTREAM_DATA when both upstream and
    #    downstream haven't run yet).
    if dependency_status and dependency_status in BLOCKING_STATES:
        dep_cfg = DATASET_FRESHNESS_CONFIG.get(dataset, {})
        deps = dep_cfg.get("depends_on", [])
        dep_names = ", ".join(d.replace("_", " ").title() for d in deps) if deps else "upstream dataset"
        upstream_not_run = dependency_status in (
            CanonicalFreshnessStatus.NOT_RUN,
            CanonicalFreshnessStatus.NOT_RUN_NO_UPSTREAM_DATA,
        )
        if upstream_not_run and downstream_not_run:
            return _result(
                CanonicalFreshnessStatus.NOT_RUN_NO_UPSTREAM_DATA,
                reason=f"{dataset.replace('_', ' ').title()} has not run, and upstream ({dep_names}) has no data yet.",
                next_action=f"Run {dep_names} sync first; derived analysis will become available after.",
            )
        return _result(
            CanonicalFreshnessStatus.BLOCKED_BY_DEPENDENCY,
            reason=f"{dataset.replace('_', ' ').title()} depends on {dep_names}. Dependency is {dependency_status.replace('_', ' ')}.",
            next_action=f"Fix {dep_names} first.",
        )

    # 2. DB unavailable (sync_status sentinel)
    if sync_status == "db_unavailable":
        return _result(
            CanonicalFreshnessStatus.DB_UNAVAILABLE,
            reason="Database connection unavailable.",
            next_action="Check database connectivity and restart if needed.",
        )

    # 3. Not run (PR-ADS-095: refine if upstream has data, this is derivable)
    if downstream_not_run:
        if dependency_status in HAS_DATA_STATES:
            dep_cfg = DATASET_FRESHNESS_CONFIG.get(dataset, {})
            deps = dep_cfg.get("depends_on", [])
            dep_names = ", ".join(d.replace("_", " ").title() for d in deps) if deps else "upstream dataset"
            return _result(
                CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE,
                reason=f"Upstream data exists ({dep_names}), but derived analysis has not been generated.",
                next_action="Run derived analysis from existing upstream data.",
            )
        return _result(
            CanonicalFreshnessStatus.NOT_RUN,
            reason="No sync state or sync batches exist for this dataset.",
            next_action="Trigger a sync via scheduler or manual run.",
        )

    # 4. Running
    if latest_batch_status == "running" or sync_status == "running":
        return _result(
            CanonicalFreshnessStatus.RUNNING,
            reason="Latest sync batch is currently running.",
            next_action="Wait for the current sync to complete.",
        )

    # 5. Failed — PR-ADS-095: distinguish "failed but has data" from "failed no data"
    if latest_batch_status == "failed" or sync_status == "failed":
        if rows_in_window is not None and rows_in_window > 0:
            return _result(
                CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED,
                reason="Latest sync failed, but usable rows exist in the selected window.",
                next_action="Review latest sync error, but page can still render using existing data.",
            )
        return _result(
            CanonicalFreshnessStatus.FAILED_NO_DATA,
            reason="Latest sync failed and no usable rows are available.",
            next_action="Check sync logs and retry source sync.",
        )

    # 6. Row count unavailable (cannot safely classify empty vs with_data).
    # PR-ADS-095: distinguish "not configured for row counts" from "row-count
    # query failed at runtime".
    if rows_in_window is None:
        batch_hint = ""
        if latest_batch_row_count is not None:
            batch_hint = f" Latest batch row count: {latest_batch_row_count}."
        if row_count_supported is False:
            return _result(
                CanonicalFreshnessStatus.ROW_COUNT_NOT_ENABLED,
                reason=f"Row-count query is not enabled for this dataset.{batch_hint}",
                next_action="Implement row-count diagnostic if this page depends on freshness.",
            )
        return _result(
            CanonicalFreshnessStatus.UNKNOWN_ROW_COUNT,
            reason=f"Row count query unavailable or not implemented for this dataset.{batch_hint}",
            next_action="Check row-count query support for this dataset.",
        )

    # 7. Determine staleness
    is_stale = _is_stale(last_successful_sync_at, latest_source_date, stale_threshold_days)
    has_rows = rows_in_window > 0

    # 8. Classify
    if is_stale:
        if has_rows:
            return _result(
                CanonicalFreshnessStatus.STALE_WITH_DATA,
                reason="Rows exist, but latest source/sync date is older than threshold.",
                next_action="Check scheduler schedule and trigger a fresh sync.",
            )
        else:
            batch_hint = ""
            if latest_batch_row_count is not None:
                batch_hint = f" Latest batch row count: {latest_batch_row_count}."
            return _result(
                CanonicalFreshnessStatus.STALE_AND_EMPTY,
                reason=f"No rows and no recent sync.{batch_hint}",
                next_action="Trigger sync and verify data source is producing rows.",
            )
    else:
        if has_rows:
            return _result(
                CanonicalFreshnessStatus.FRESH_WITH_DATA,
                reason="Data present and recently synced.",
                next_action="No action needed.",
            )
        else:
            batch_hint = ""
            if latest_batch_row_count is not None:
                batch_hint = f" Latest batch row count: {latest_batch_row_count}."
            # PR-ADS-095: when the latest sync batch succeeded AND reported zero
            # rows from the source, emit EMPTY_SUCCESS to signal "source has
            # nothing to deliver" rather than the more concerning
            # FRESH_BUT_EMPTY (window is empty but source data may exist).
            if (
                latest_batch_status == "success"
                and latest_batch_row_count == 0
            ):
                return _result(
                    CanonicalFreshnessStatus.EMPTY_SUCCESS,
                    reason=(
                        "Latest sync succeeded and the source returned zero rows."
                        f"{batch_hint}"
                    ),
                    next_action="Verify the source has data; otherwise this is a clean empty.",
                )
            return _result(
                CanonicalFreshnessStatus.FRESH_BUT_EMPTY,
                reason=f"Latest sync succeeded but no rows exist in the selected window.{batch_hint}",
                next_action="Check source pull, sync batch row count, and pipeline verifier.",
            )


# ── Helpers ─────────────────────────────────────────────────────────────────

def _result(status: str, reason: str, next_action: str) -> dict[str, Any]:
    return {
        "canonical_status": status,
        "severity": SEVERITY_MAP.get(status, "neutral"),
        "reason": reason,
        "next_action": next_action,
    }


def _is_stale(
    last_successful_sync_at: datetime | None,
    latest_source_date: date | None,
    stale_threshold_days: int,
) -> bool:
    """Determine if a dataset is stale based on available timestamps."""
    now = datetime.now(timezone.utc)
    threshold = timedelta(days=stale_threshold_days)

    # Prefer last_successful_sync_at for staleness check
    if last_successful_sync_at:
        if last_successful_sync_at.tzinfo is None:
            last_successful_sync_at = last_successful_sync_at.replace(tzinfo=timezone.utc)
        return (now - last_successful_sync_at) > threshold

    # Fall back to latest_source_date
    if latest_source_date:
        source_dt = datetime.combine(latest_source_date, datetime.min.time(), tzinfo=timezone.utc)
        return (now - source_dt) > threshold

    # No date info — consider stale
    return True


def canonical_status_display_label(status: str) -> str:
    """Return the human-readable label for a canonical status."""
    labels = {
        CanonicalFreshnessStatus.FRESH_WITH_DATA: "Fresh with data",
        CanonicalFreshnessStatus.FRESH_BUT_EMPTY: "Fresh but empty",
        CanonicalFreshnessStatus.STALE_WITH_DATA: "Stale with data",
        CanonicalFreshnessStatus.STALE_AND_EMPTY: "Stale and empty",
        CanonicalFreshnessStatus.FAILED: "Failed",
        CanonicalFreshnessStatus.RUNNING: "Running",
        CanonicalFreshnessStatus.NOT_RUN: "Not run",
        CanonicalFreshnessStatus.DEPENDENCY_BLOCKED: "Blocked by dependency",
        CanonicalFreshnessStatus.DB_UNAVAILABLE: "Database unavailable",
        CanonicalFreshnessStatus.UNKNOWN: "Unknown",
        # PR-ADS-095 refined states
        CanonicalFreshnessStatus.DATA_AVAILABLE_LATEST_SYNC_FAILED: "Data Available, Latest Sync Failed",
        CanonicalFreshnessStatus.FAILED_NO_DATA: "Failed, No Data",
        CanonicalFreshnessStatus.NOT_RUN_BUT_DERIVABLE: "Not Run, But Derivable",
        CanonicalFreshnessStatus.NOT_RUN_NO_UPSTREAM_DATA: "Not Run, No Upstream Data",
        CanonicalFreshnessStatus.UNKNOWN_ROW_COUNT: "Row Count Unavailable",
        CanonicalFreshnessStatus.ROW_COUNT_NOT_ENABLED: "Row Count Not Enabled",
        CanonicalFreshnessStatus.BLOCKED_BY_DEPENDENCY: "Blocked by Dependency",
        CanonicalFreshnessStatus.EMPTY_SUCCESS: "Empty Success",
    }
    return labels.get(status, "Unknown")
