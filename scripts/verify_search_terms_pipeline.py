#!/usr/bin/env python3
"""
scripts/verify_search_terms_pipeline.py

Read-only Search Terms pipeline verification script.

Inspects every stage of the Search Terms evidence pipeline:
  direct Google Ads API pull → shared canonical search-term sync service
  → search_terms table → /api/search-terms → UI

Produces a final verdict indicating pipeline health or the specific failure point.

PR-ADS-156 §8 — this script used to describe, and check, a pipeline that no
longer exists. It read `sync_state` and `sync_batches` for `source='windsor'`,
pulled live rows through `connectors.windsor_pull`, and told the operator to
"Verify Windsor plan/API access". Windsor was retired in PR-ADS-154, so every
one of those checks was reading a table nothing writes and recommending a
remedy nobody can apply.

It also had no exit code and no database initialisation: run as `python -m`
nothing calls `init_pool`, so `get_conn()` yields None, `available` is False,
and the script confidently reports DB_UNAVAILABLE against a healthy database.

Three distinctions it could not previously draw, and now must:
  * a source failure vs a VERIFIED EMPTY interval (a successful query with no
    eligible rows is a measurement, not an outage);
  * fresh vs stale (rows in an old table are not evidence of a recent sync);
  * complete vs partial history (all-time coverage is disclosed, never assumed).

Usage:
    python scripts/verify_search_terms_pipeline.py --days 60 --db-only --pretty
    python scripts/verify_search_terms_pipeline.py --days 60 --pull-live --pretty
    python scripts/verify_search_terms_pipeline.py --days 60 --api-url "$APP_URL" --admin-token "$ADMIN_API_TOKEN" --pretty
    python scripts/verify_search_terms_pipeline.py --days 60 --json

PR-ADS-065 — Search Terms Pipeline Verification & Repair
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.dataset_keys import (  # noqa: E402
    SEARCH_TERMS_DATASET, SEARCH_TERMS_SOURCE,
)

log = logging.getLogger(__name__)

# ─── Verdicts ────────────────────────────────────────────────────────────────

class Verdict:
    """Provider-neutral pipeline verdicts.

    PR-ADS-156 §8 renamed the three Windsor-specific values. The old ATTRIBUTE
    names are kept as aliases at the bottom so existing callers and tests keep
    resolving — what changed is that they no longer name a retired vendor in
    operator-facing output, and no longer send anyone to check it.
    """

    OK = "OK"
    NOT_DEPLOYED_OR_NOT_RUN_AFTER_DEPLOYMENT = "NOT_DEPLOYED_OR_NOT_RUN_AFTER_DEPLOYMENT"
    SOURCE_PULL_EMPTY = "SOURCE_PULL_EMPTY"
    SOURCE_MISSING_SEARCH_TERM_FIELD = "SOURCE_MISSING_SEARCH_TERM_FIELD"
    FILE_EMPTY = "FILE_EMPTY"
    DB_WRITE_FAILED = "DB_WRITE_FAILED"
    DB_HAS_ROWS_API_EMPTY = "DB_HAS_ROWS_API_EMPTY"
    API_HAS_ROWS_UI_LIKELY_FILTERED = "API_HAS_ROWS_UI_LIKELY_FILTERED"
    FRESH_BUT_EMPTY = "FRESH_BUT_EMPTY"
    DB_UNAVAILABLE = "DB_UNAVAILABLE"
    UNKNOWN = "UNKNOWN"

    # ── PR-ADS-156 §8: the states the old vocabulary could not express ──────
    #: A successful canonical sync over an interval Google Ads reported no
    #: eligible query data for. Healthy — and deliberately NOT the same as a
    #: pull that returned nothing because it failed.
    VERIFIED_EMPTY = "VERIFIED_EMPTY"
    #: The canonical sync batch is recorded `failed`. The interval was attempted
    #: and not covered.
    CANONICAL_SYNC_FAILED = "CANONICAL_SYNC_FAILED"
    #: No canonical sync state at all — never run against this database.
    CANONICAL_SYNC_NEVER_RUN = "CANONICAL_SYNC_NEVER_RUN"

    #: PR-ADS-156-F3 review §2 — the Google Ads account this command reports on
    #: cannot be resolved, so there is no canonical population to count. Its own
    #: verdict rather than DB_UNAVAILABLE (the database is fine) or UNKNOWN (the
    #: cause is known exactly, and it is fixable in one environment variable).
    ACCOUNT_NOT_CONFIGURED = "ACCOUNT_NOT_CONFIGURED"
    #: Rows exist, but the newest one is older than the staleness threshold. An
    #: old table is not evidence of a recent sync.
    STALE = "STALE"
    #: Current window healthy; all-time history is NOT proven complete.
    PARTIAL_HISTORY = "PARTIAL_HISTORY"

    # Deprecated aliases — same values as the provider-neutral names above.
    WINDSOR_PULL_EMPTY = SOURCE_PULL_EMPTY
    WINDSOR_PULL_MISSING_SEARCH_TERM_FIELD = SOURCE_MISSING_SEARCH_TERM_FIELD


#: Verdicts a healthy (or explicitly accepted) run may exit zero on.
HEALTHY_VERDICTS = frozenset({Verdict.OK, Verdict.PARTIAL_HISTORY})
ACCEPTABLE_WITH_FLAG = frozenset({Verdict.VERIFIED_EMPTY})


# ─── Verdict Logic ───────────────────────────────────────────────────────────

def compute_search_terms_verdict(
    *,
    db_available: bool = False,
    db_rows_window: int = 0,
    window_days: int = 60,
    sync_status: Optional[str] = None,
    latest_weekly_run: Optional[str] = None,
    live_pull_rows: Optional[int] = None,
    live_pull_has_search_term: Optional[bool] = None,
    api_rows: Optional[int] = None,
    file_rows: Optional[int] = None,
    verified_empty: bool = False,
    stale: bool = False,
    max_source_date: Optional[str] = None,
    history_complete: Optional[bool] = None,
    scope_available: bool = True,
) -> tuple[str, str]:
    """Compute pipeline verdict and reason from gathered evidence.

    Returns (verdict, reason) tuple.
    """
    # PR-ADS-156-F3 review §2 — checked FIRST, and deliberately so. Without a
    # resolved account every row count below is zero, and zero rows would be
    # read as an empty or broken pipeline: the command would report a data
    # problem for what is a configuration problem, and send an operator looking
    # for the wrong thing entirely.
    if not scope_available:
        return (
            Verdict.ACCOUNT_NOT_CONFIGURED,
            "GOOGLE_ADS_CUSTOMER_ID is not configured, so the canonical "
            "search-term population cannot be identified. No row counts are "
            "reported: counting every account's rows would answer a different "
            "question than the one asked.",
        )

    # DB unavailable
    if not db_available:
        return Verdict.DB_UNAVAILABLE, "Database connection unavailable"

    # Not deployed or not run
    if latest_weekly_run is None and db_rows_window == 0 and sync_status is None:
        return (
            Verdict.NOT_DEPLOYED_OR_NOT_RUN_AFTER_DEPLOYMENT,
            "No weekly run found and no sync state for search_terms; "
            "pipeline may not have run since deployment",
        )

    # Live pull was attempted and returned 0
    if live_pull_rows is not None and live_pull_rows == 0 and db_rows_window == 0:
        return (
            Verdict.WINDSOR_PULL_EMPTY,
            "Windsor pull returned 0 rows and DB has 0 rows; "
            "source is empty or REST endpoint not returning data",
        )

    # Live pull returned rows but missing search_term field
    if live_pull_rows is not None and live_pull_rows > 0 and live_pull_has_search_term is False:
        return (
            Verdict.WINDSOR_PULL_MISSING_SEARCH_TERM_FIELD,
            f"Windsor returned {live_pull_rows} rows but search_term field is missing; "
            "field mapping may be wrong",
        )

    # Live pull has rows but DB is empty → write failed
    if live_pull_rows is not None and live_pull_rows > 0 and db_rows_window == 0:
        return (
            Verdict.DB_WRITE_FAILED,
            f"Windsor returned {live_pull_rows} rows but DB has 0; "
            "write_search_terms() may be failing silently",
        )

    # PR-ADS-156 §8 — a canonical sync that FAILED is not a data finding.
    if sync_status == "failed":
        return (
            Verdict.CANONICAL_SYNC_FAILED,
            "The canonical google_ads_api/search_terms sync batch is recorded "
            "failed: the interval was attempted and is NOT covered. Row counts "
            "below describe older syncs, not this one.",
        )

    # Sync success but DB empty. Two very different events used to share this
    # verdict, and the distinction is the whole point of PR-ADS-156 §4:
    #   * the sync ran, queried the interval and Google Ads had no eligible
    #     query data — a MEASUREMENT, and healthy;
    #   * something wrote a success without covering the interval — suspicious.
    # `verified_empty` is what the canonical service records for the first, so
    # it is read here rather than guessed from the row count.
    if sync_status in ("success", "fresh") and db_rows_window == 0:
        if verified_empty:
            return (
                Verdict.VERIFIED_EMPTY,
                f"Canonical sync succeeded over the {window_days}-day window and "
                "Google Ads reported no eligible search-term rows. The interval "
                "was queried; it is empty, not missing.",
            )
        return (
            Verdict.FRESH_BUT_EMPTY,
            f"Sync state shows success but search_terms table has 0 rows in {window_days}-day window; "
            "pipeline completed but produced no evidence",
        )

    # File check
    if file_rows is not None and file_rows == 0 and db_rows_window == 0:
        return Verdict.FILE_EMPTY, "ads_search_terms.json is empty and DB has 0 rows"

    # DB has rows but API returns 0
    if db_rows_window > 0 and api_rows is not None and api_rows == 0:
        return (
            Verdict.DB_HAS_ROWS_API_EMPTY,
            f"DB has {db_rows_window} rows in {window_days}-day window but API returned 0; "
            "API query or auth may be filtering incorrectly",
        )

    # API has rows but UI might filter (informational — can't check UI programmatically)
    if api_rows is not None and api_rows > 0 and db_rows_window > 0:
        return Verdict.OK, f"Pipeline OK: DB has {db_rows_window} rows, API returned {api_rows}"

    # PR-ADS-156 §8 — rows in an old table are not evidence of a recent sync.
    # Checked BEFORE the healthy verdicts below, which is the point: the old
    # order returned OK on any non-zero count, so a pipeline that stopped
    # running a month ago reported "Pipeline OK" for as long as its rows stayed
    # inside the requested window.
    if stale:
        return (
            Verdict.STALE,
            f"search_terms holds {db_rows_window} row(s) in the {window_days}-day "
            f"window but the newest source_date is {max_source_date or 'unknown'}, "
            "older than the freshness threshold. Old rows are not a recent sync.",
        )

    # DB has rows and no API check → OK, or OK with history disclosed.
    if db_rows_window > 0:
        if history_complete is False:
            return (
                Verdict.PARTIAL_HISTORY,
                f"Current window healthy ({db_rows_window} rows in {window_days} "
                "days), but all-time search-term history is NOT proven complete. "
                "Disclosed rather than claimed.",
            )
        return Verdict.OK, f"Pipeline OK: DB has {db_rows_window} rows in {window_days}-day window"

    # Fallback
    return Verdict.UNKNOWN, "Could not determine pipeline state from available evidence"


# ─── DB Checks ───────────────────────────────────────────────────────────────

def _check_db(days: int) -> dict[str, Any]:
    """Read-only DB inspection for search_terms pipeline."""
    result: dict[str, Any] = {
        "available": False,
        "rows_7d": 0,
        "rows_14d": 0,
        "rows_30d": 0,
        "rows_60d": 0,
        "rows_requested_window": 0,
        "max_source_date": None,
        "blank_search_term_count": 0,
        "null_campaign_count": 0,
        "rows_with_spend": 0,
        "rows_with_clicks": 0,
        "latest_weekly_run": None,
        "latest_daily_run": None,
        "sync_status": None,
        "sync_batch_latest": None,
    }

    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from analysis.search_term_scope import canonical_scope, claimed_scope
        from db.connection import get_conn
    except Exception:
        return result

    # PR-ADS-156-F3 review §2 — an operator running this command is asking the
    # same question the dashboard asks, and must be given the same population.
    # Every count below was bounded on the date window alone, so during the
    # cutover it reported roughly double what the product displayed: an operator
    # verifying the pipeline would have seen 32,367 healthy rows and concluded
    # everything was fine, from the very command meant to catch it.
    scope = canonical_scope()
    claimed = claimed_scope()
    result["scope_available"] = scope.available
    result["scope_customer_id"] = scope.customer_id
    if not scope.available:
        # Fail closed. Counting every account's rows because this one cannot be
        # resolved would answer a different question than the one asked, and
        # would answer it confidently.
        result["scope_reason"] = scope.reason
        result["error"] = (
            "canonical search-term scope unavailable "
            f"({scope.reason}) — no row counts are reported, because a count "
            "over an unresolved account is not the count that was asked for")
        return result

    try:
        with get_conn() as conn:
            if conn is None:
                return result
            result["available"] = True

            with conn.cursor() as cur:
                # Row counts by window, over the CANONICAL population only.
                for window, key in [(7, "rows_7d"), (14, "rows_14d"), (30, "rows_30d"), (60, "rows_60d")]:
                    cur.execute(
                        "SELECT COUNT(*) FROM search_terms WHERE "
                        + scope.and_("source_date >= NOW() - INTERVAL '1 day' * %s"),
                        (*scope.params, window),
                    )
                    row = cur.fetchone()
                    result[key] = int(row[0]) if row else 0

                # Max source_date — of a canonical row. The newest row overall
                # could be a Windsor row, and reporting that as the pipeline's
                # high-water mark is how a stopped pipeline looks current.
                cur.execute(
                    "SELECT MAX(source_date) FROM search_terms WHERE " + scope.sql,
                    scope.params)
                row = cur.fetchone()
                result["max_source_date"] = str(row[0]) if row and row[0] else None

                # Quality counts are taken over the CLAIMED population — right
                # provenance, right account, identity completeness deliberately
                # NOT required. Taking them inside `canonical_scope` would filter
                # on the very defect being counted, so every one would read zero
                # forever no matter how broken the table was.
                cur.execute(
                    "SELECT COUNT(*) FROM search_terms WHERE "
                    + claimed.and_("search_term IS NULL OR TRIM(search_term) = ''"),
                    claimed.params)
                row = cur.fetchone()
                result["blank_search_term_count"] = int(row[0]) if row else 0

                cur.execute(
                    "SELECT COUNT(*) FROM search_terms WHERE "
                    + claimed.and_("campaign_name IS NULL"), claimed.params)
                row = cur.fetchone()
                result["null_campaign_count"] = int(row[0]) if row else 0

                cur.execute(
                    "SELECT COUNT(*) FROM search_terms WHERE "
                    + scope.and_("spend_usd > 0"), scope.params)
                row = cur.fetchone()
                result["rows_with_spend"] = int(row[0]) if row else 0

                cur.execute(
                    "SELECT COUNT(*) FROM search_terms WHERE "
                    + scope.and_("clicks > 0"), scope.params)
                row = cur.fetchone()
                result["rows_with_clicks"] = int(row[0]) if row else 0

                # Disclosure, never added to any count above: what the old
                # date-only queries would have swept in. Reported so an operator
                # comparing this command against a raw row count is not left
                # wondering where the difference went.
                cur.execute(
                    "SELECT COUNT(*) FROM search_terms WHERE customer_id IS NULL "
                    "AND source_date >= NOW() - INTERVAL '1 day' * %s", (days,))
                row = cur.fetchone()
                result["null_customer_rows_in_window"] = int(row[0]) if row else 0

                # Latest weekly run
                cur.execute(
                    "SELECT id, started_at, status FROM runs "
                    "WHERE run_type = 'weekly' ORDER BY started_at DESC LIMIT 1"
                )
                row = cur.fetchone()
                if row:
                    result["latest_weekly_run"] = {
                        "id": row[0],
                        "started_at": str(row[1]) if row[1] else None,
                        "status": row[2],
                    }

                # Latest daily run
                cur.execute(
                    "SELECT id, started_at, status FROM runs "
                    "WHERE run_type = 'daily' ORDER BY started_at DESC LIMIT 1"
                )
                row = cur.fetchone()
                if row:
                    result["latest_daily_run"] = {
                        "id": row[0],
                        "started_at": str(row[1]) if row[1] else None,
                        "status": row[2],
                    }

                # PR-ADS-156 §8 — CANONICAL sync state. This read
                # `source = 'windsor'`, a source no active path has written
                # since PR-ADS-154, so the verdict was computed from a row that
                # could only ever get older.
                cur.execute(
                    "SELECT status, last_successful_sync_at, error_message "
                    "FROM sync_state "
                    "WHERE source = %s AND dataset = %s "
                    "LIMIT 1",
                    (SEARCH_TERMS_SOURCE, SEARCH_TERMS_DATASET),
                )
                row = cur.fetchone()
                if row:
                    result["sync_status"] = row[0]
                    result["sync_last_success"] = str(row[1]) if row[1] else None
                    result["sync_error"] = row[2]
                result["sync_source"] = SEARCH_TERMS_SOURCE
                result["sync_dataset"] = SEARCH_TERMS_DATASET

                # Historical Windsor/MCP rows are still REPORTED — they are real
                # evidence of how the table was populated — but under a key that
                # says they are legacy, so they can never be mistaken for the
                # current source.
                cur.execute(
                    "SELECT source, status, last_successful_sync_at FROM sync_state "
                    "WHERE source IN ('windsor', 'windsor_mcp') "
                    "  AND dataset = 'search_terms' "
                    "ORDER BY last_successful_sync_at DESC NULLS LAST LIMIT 1"
                )
                row = cur.fetchone()
                if row:
                    result["legacy_sync_state"] = {
                        "source": row[0], "status": row[1],
                        "last_successful_sync_at": str(row[2]) if row[2] else None,
                        "note": "historical only — never current freshness",
                    }

                # Latest CANONICAL sync_batch for search_terms
                cur.execute(
                    "SELECT id, status, row_count, started_at, finished_at, error_message "
                    "FROM sync_batches "
                    "WHERE source = %s AND dataset = %s "
                    "ORDER BY started_at DESC LIMIT 1",
                    (SEARCH_TERMS_SOURCE, SEARCH_TERMS_DATASET),
                )
                row = cur.fetchone()
                if row:
                    result["sync_batch_latest"] = {
                        "id": row[0],
                        "status": row[1],
                        "row_count": row[2],
                        "started_at": str(row[3]) if row[3] else None,
                        "finished_at": str(row[4]) if row[4] else None,
                        "error_message": row[5],
                    }

                if days in (7, 14, 30, 60):
                    result["rows_requested_window"] = int(result[f"rows_{days}d"])
                else:
                    cur.execute(
                        "SELECT COUNT(*) FROM search_terms WHERE "
                        + scope.and_("source_date >= NOW() - INTERVAL '1 day' * %s"),
                        (*scope.params, days),
                    )
                    row = cur.fetchone()
                    result["rows_requested_window"] = int(row[0]) if row else 0

    except Exception as exc:
        log.warning("DB check failed: %s", exc)
        result["error"] = str(exc)

    return result


# ─── File Check ──────────────────────────────────────────────────────────────

def _check_file() -> dict[str, Any]:
    """Check data/ads_search_terms.json for rows and sample keys."""
    result: dict[str, Any] = {
        "exists": False,
        "row_count": 0,
        "sample_keys": [],
        "sample_search_term": None,
    }

    file_path = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / "data" / "ads_search_terms.json"
    if not file_path.exists():
        return result

    result["exists"] = True
    try:
        with open(file_path, "r") as f:
            data = json.load(f)

        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            rows = data.get("data", data.get("rows", []))
        else:
            rows = []

        result["row_count"] = len(rows)
        if rows:
            result["sample_keys"] = sorted(rows[0].keys()) if isinstance(rows[0], dict) else []
            if isinstance(rows[0], dict):
                result["sample_search_term"] = rows[0].get("search_term")
    except Exception as exc:
        result["error"] = str(exc)

    return result


# ─── Live Pull Check ─────────────────────────────────────────────────────────

def _check_live_pull(days: int) -> dict[str, Any]:
    """Attempt a live read-only pull through the DIRECT Google Ads connector.

    Only when --pull-live is set. PR-ADS-156 §8: this used
    `connectors.windsor_pull`, a retired provider, so the one check that could
    have distinguished "the source has nothing" from "the source is broken" was
    asking a source production no longer uses.
    """
    result: dict[str, Any] = {
        "attempted": True,
        "row_count": 0,
        "sample_keys": [],
        "has_search_term_field": None,
        "has_campaign_field": None,
        "has_ad_group_field": None,
        "sample_row": None,
        "date_preset": None,
        "error": None,
    }

    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from connectors.google_ads_source import pull_search_terms

        rows = pull_search_terms(days_back=days)
        result["row_count"] = len(rows) if rows else 0

        if rows:
            sample = rows[0] if isinstance(rows[0], dict) else {}
            result["sample_keys"] = sorted(sample.keys())
            result["has_search_term_field"] = "search_term" in sample
            result["has_campaign_field"] = "campaign" in sample
            result["has_ad_group_field"] = "ad_group" in sample
            # Sanitise sample row for output (no sensitive data)
            result["sample_row"] = {
                k: str(v)[:80] for k, v in list(sample.items())[:8]
            }
        else:
            result["has_search_term_field"] = None

        # Determine preset used
        if days <= 1:
            result["date_preset"] = "last_1d"
        elif days <= 7:
            result["date_preset"] = "last_7d"
        elif days <= 14:
            result["date_preset"] = "last_14d"
        else:
            result["date_preset"] = "last_60d"

    except Exception as exc:
        result["error"] = str(exc)

    return result


# ─── API Check ───────────────────────────────────────────────────────────────

def _check_api(api_url: str, admin_token: str, days: int) -> dict[str, Any]:
    """Check /api/search-terms and /api/search-terms/summary via HTTP."""
    import urllib.request
    import urllib.error

    result: dict[str, Any] = {
        "search_terms_rows": None,
        "search_terms_data_quality": None,
        "summary": None,
        "error": None,
    }

    headers = {"Authorization": f"Bearer {admin_token}"}

    # GET /api/search-terms
    try:
        url = f"{api_url.rstrip('/')}/api/search-terms?days={days}&limit=5"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            result["search_terms_rows"] = len(data.get("rows", []))
            result["search_terms_data_quality"] = data.get("data_quality")
            result["search_terms_pagination"] = data.get("pagination")
    except Exception as exc:
        result["error"] = f"search-terms: {exc}"

    # GET /api/search-terms/summary
    try:
        url = f"{api_url.rstrip('/')}/api/search-terms/summary?days={days}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            result["summary"] = data.get("summary")
    except Exception as exc:
        if result["error"]:
            result["error"] += f"; summary: {exc}"
        else:
            result["error"] = f"summary: {exc}"

    return result


# ─── Main Orchestration ──────────────────────────────────────────────────────

#: A successful canonical batch whose row_count is 0 IS the verified-empty
#: record: the service finishes the batch `success` with `last_source_date` set
#: only when the query itself succeeded.
def _verified_empty(db_info: dict) -> bool:
    batch = db_info.get("sync_batch_latest") or {}
    return bool(batch.get("status") == "success"
                and (batch.get("row_count") or 0) == 0
                and db_info.get("sync_status") in ("success", "fresh"))


def _is_stale(db_info: dict, stale_after_days: int) -> bool:
    """Is the newest stored source_date older than the threshold?

    Answered from the DATA, not from the sync record: a sync row can say
    `success` while the newest fact it wrote is a month old.
    """
    newest = db_info.get("max_source_date")
    if not newest:
        return False
    try:
        newest_date = date.fromisoformat(str(newest)[:10])
    except (ValueError, TypeError):
        return False
    return (date.today() - newest_date) > timedelta(days=stale_after_days)


def run_verification(
    days: int = 60,
    db_only: bool = True,
    pull_live: bool = False,
    api_url: Optional[str] = None,
    admin_token: Optional[str] = None,
    stale_after_days: int = 8,
) -> dict[str, Any]:
    """Run the full Search Terms pipeline verification."""

    effective_db_only = bool(db_only)
    do_live_pull = bool(pull_live and not effective_db_only)
    do_api_check = bool((api_url and admin_token) and not effective_db_only)

    report: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "modes": {
            "db": True,
            "file": True,
            "db_only": effective_db_only,
            "live_pull": do_live_pull,
            "api": do_api_check,
        },
    }

    # Always check file
    report["file"] = _check_file()

    # Always check DB
    report["db"] = _check_db(days)

    # Optional live pull
    live_result = None
    if do_live_pull:
        live_result = _check_live_pull(days)
        report["live_pull"] = live_result

    # Optional API check
    api_result = None
    if do_api_check:
        api_result = _check_api(api_url, admin_token, days)
        report["api"] = api_result

    # Compute verdict
    db_info = report["db"]
    verdict, reason = compute_search_terms_verdict(
        db_available=db_info.get("available", False),
        db_rows_window=db_info.get("rows_requested_window", 0),
        window_days=days,
        sync_status=db_info.get("sync_status"),
        latest_weekly_run=(
            db_info["latest_weekly_run"]["started_at"]
            if db_info.get("latest_weekly_run") else None
        ),
        live_pull_rows=live_result["row_count"] if live_result else None,
        live_pull_has_search_term=(
            live_result.get("has_search_term_field") if live_result else None
        ),
        api_rows=api_result.get("search_terms_rows") if api_result else None,
        file_rows=report["file"].get("row_count"),
        # PR-ADS-156 §8 — the three signals the old verdict could not see.
        verified_empty=_verified_empty(db_info),
        stale=_is_stale(db_info, stale_after_days),
        max_source_date=db_info.get("max_source_date"),
        history_complete=db_info.get("history_complete"),
        scope_available=db_info.get("scope_available", True),
    )

    report["verdict"] = verdict
    report["reason"] = reason

    return report


# ─── CLI ─────────────────────────────────────────────────────────────────────

#: Exit codes. A verification script that always exits 0 is not a check.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_UNAVAILABLE = 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the Search Terms evidence pipeline (read-only)."
    )
    parser.add_argument("--days", type=int, default=60, help="Lookback window in days")
    parser.add_argument(
        "--db-only",
        action="store_true",
        help="Force DB+file-only mode (default when neither --pull-live nor --api-url is set)",
    )
    parser.add_argument("--pull-live", action="store_true",
                        help="Attempt a live read-only pull via the direct Google Ads API")
    parser.add_argument("--stale-after-days", type=int, default=8,
                        help="Newest source_date older than this many days is STALE")
    parser.add_argument("--accept-verified-empty", action="store_true",
                        help="Exit 0 on VERIFIED_EMPTY (a successful query over an "
                             "interval Google Ads reported no eligible rows for)")
    parser.add_argument("--api-url", type=str, default=None, help="App base URL for API checks")
    parser.add_argument("--admin-token", type=str, default=None, help="Admin bearer token for API auth")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print output")
    parser.add_argument("--json", action="store_true", dest="json_output", help="JSON output")

    args = parser.parse_args()

    # PR-ADS-156 §8 — initialise the pool. Run as `python -m`, nothing has
    # called `init_pool`, so every `get_conn()` yields None and this script
    # reports DB_UNAVAILABLE against a perfectly healthy database — turning
    # "we never opened the database" into a finding about the pipeline.
    from db.connection import ensure_database_ready  # noqa: PLC0415

    ready, detail = ensure_database_ready()
    if not ready:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "verdict": Verdict.DB_UNAVAILABLE,
            "reason": f"database unavailable: {detail}",
            "next_action": "Fix database connectivity, then re-run.",
        }
        print(json.dumps(payload, indent=2, default=str))
        return EXIT_UNAVAILABLE

    api_url = args.api_url or os.environ.get("APP_URL")
    admin_token = args.admin_token or os.environ.get("ADMIN_API_TOKEN")
    db_only_mode = bool(args.db_only or (not args.pull_live and not api_url))

    result = run_verification(
        days=args.days,
        db_only=db_only_mode,
        pull_live=args.pull_live,
        api_url=api_url,
        admin_token=admin_token,
        stale_after_days=args.stale_after_days,
    )

    if args.pretty and not args.json_output:
        _print_pretty(result)
    else:
        print(json.dumps(result, indent=2, default=str))

    verdict = result.get("verdict")
    if verdict in HEALTHY_VERDICTS:
        return EXIT_OK
    if verdict in ACCEPTABLE_WITH_FLAG:
        # A verified-empty interval is healthy DATA, but whether it is an
        # acceptable RESULT depends on what the operator expected, so it is not
        # silently green: it exits 0 only when explicitly accepted.
        return EXIT_OK if args.accept_verified_empty else EXIT_FAILED
    # Both are "this command could not answer", not "the answer is bad".
    if verdict in (Verdict.DB_UNAVAILABLE, Verdict.ACCOUNT_NOT_CONFIGURED):
        return EXIT_UNAVAILABLE
    return EXIT_FAILED


def _print_pretty(result: dict[str, Any]):
    """Human-readable pretty print."""
    print("=" * 70)
    print("  SEARCH TERMS PIPELINE VERIFICATION")
    print("=" * 70)
    print(f"  Timestamp: {result['timestamp']}")
    print(f"  Window:    {result['days']} days")
    print(f"  Modes:     {result['modes']}")
    print()

    # DB section
    db = result.get("db", {})
    print("── Database ──────────────────────────────────────────────────────")
    print(f"  Available:         {db.get('available')}")
    print(f"  Rows (7d):         {db.get('rows_7d')}")
    print(f"  Rows (14d):        {db.get('rows_14d')}")
    print(f"  Rows (30d):        {db.get('rows_30d')}")
    print(f"  Rows (60d):        {db.get('rows_60d')}")
    print(f"  Rows (requested):  {db.get('rows_requested_window')} (days={result['days']})")
    print(f"  MAX(source_date):  {db.get('max_source_date')}")
    print(f"  Blank search_term: {db.get('blank_search_term_count')}")
    print(f"  NULL campaign:     {db.get('null_campaign_count')}")
    print(f"  Rows w/ spend>0:   {db.get('rows_with_spend')}")
    print(f"  Rows w/ clicks>0:  {db.get('rows_with_clicks')}")
    print(f"  Sync status:       {db.get('sync_status')}")
    print(f"  Latest weekly run: {db.get('latest_weekly_run')}")
    print(f"  Latest daily run:  {db.get('latest_daily_run')}")
    print(f"  Latest sync batch: {db.get('sync_batch_latest')}")
    print()

    # File section
    f = result.get("file", {})
    print("── File (data/ads_search_terms.json) ──────────────────────────────")
    print(f"  Exists:       {f.get('exists')}")
    print(f"  Row count:    {f.get('row_count')}")
    print(f"  Sample keys:  {f.get('sample_keys')}")
    print(f"  Sample term:  {f.get('sample_search_term')}")
    print()

    # Live pull section
    if "live_pull" in result:
        lp = result["live_pull"]
        print("── Live Windsor Pull ─────────────────────────────────────────────")
        print(f"  Date preset:    {lp.get('date_preset')}")
        print(f"  Row count:      {lp.get('row_count')}")
        print(f"  Sample keys:    {lp.get('sample_keys')}")
        print(f"  Has search_term:{lp.get('has_search_term_field')}")
        print(f"  Has campaign:   {lp.get('has_campaign_field')}")
        print(f"  Has ad_group:   {lp.get('has_ad_group_field')}")
        print(f"  Sample row:     {lp.get('sample_row')}")
        if lp.get("error"):
            print(f"  ERROR:          {lp['error']}")
        print()

    # API section
    if "api" in result:
        api = result["api"]
        print("── API Check ─────────────────────────────────────────────────────")
        print(f"  /api/search-terms rows: {api.get('search_terms_rows')}")
        print(f"  data_quality:           {api.get('search_terms_data_quality')}")
        print(f"  Summary:                {api.get('summary')}")
        if api.get("error"):
            print(f"  ERROR:                  {api['error']}")
        print()

    # Verdict
    print("── VERDICT ───────────────────────────────────────────────────────")
    print(f"  {result['verdict']}")
    print(f"  Reason: {result['reason']}")
    print("=" * 70)


if __name__ == "__main__":
    sys.exit(main())
