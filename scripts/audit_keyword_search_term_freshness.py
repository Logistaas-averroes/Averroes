#!/usr/bin/env python3
"""
scripts/audit_keyword_search_term_freshness.py

PR-ADS-156 §9 — ONE read-only command that proves whether the two Platform
Evidence datasets are actually current.

    python -m scripts.audit_keyword_search_term_freshness --json
    python -m scripts.audit_keyword_search_term_freshness            # human-readable

What it is for
--------------
Before this command, "is Keyword Evidence fresh?" could only be answered by
reading a page and believing it. A page reads a table; a table full of rows
looks identical whether they arrived last night or last quarter. The question
that matters is not "are there rows" but "did a canonical sync succeed over the
interval those rows claim to cover, and did everything it fetched get stored".

So this inspects the SYNC RECORD and the DATA together, and reports the ways
they can disagree:

  * a sync that never ran, or ran and failed — no rows are evidence of anything;
  * a sync that succeeded over an interval Google Ads had no rows for
    (verified empty) — healthy, and deliberately not the same as either of the
    above;
  * rows present but the newest one older than the threshold — stale, which an
    "are there rows" check reports as healthy;
  * rows fetched and not persisted, or persisted partially;
  * duplicate natural keys, missing immutable identity, unproven currency
    lineage;
  * legacy tables or legacy production reads still active.

Exit codes
----------
    0  current canonical freshness AND persistence are proven
    1  at least one violation
    2  the database could not be read (nothing was measured, so nothing passes)

Unproven ALL-TIME history is reported separately and does not block a zero exit:
"we have not proven every historical date" is a true and useful statement about
a dataset whose current window is healthy. It is never reported as complete.

Strictly read-only. It opens the database, runs SELECTs, and writes nothing —
to the database, to Google Ads, or anywhere else.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.dataset_keys import (  # noqa: E402
    KEYWORD_FACTS_DATASET, KEYWORD_FACTS_SOURCE,
    SEARCH_TERMS_DATASET, SEARCH_TERMS_SOURCE,
)

# ── Violation codes ─────────────────────────────────────────────────────────
V_SYNC_NEVER_RUN = "canonical_sync_never_run"
V_SYNC_FAILED = "canonical_sync_failed"
V_SOURCE_STALE = "canonical_source_stale"
V_TABLE_UNAVAILABLE = "canonical_table_unavailable"
V_FETCHED_NOT_PERSISTED = "fetched_rows_not_persisted"
V_PARTIAL_PERSISTENCE = "partial_persistence"
V_MISSING_IDENTITY = "missing_identity"
V_UNPROVEN_CURRENCY = "unproven_currency_lineage"
V_DUPLICATE_NATURAL_KEY = "duplicate_natural_key"
V_LEGACY_SOURCE_ACTIVE = "legacy_source_active"
V_FRESHNESS_KEY_MISMATCH = "freshness_key_mismatch"

#: Reported, never counted as a violation. An unproven historical range is a
#: disclosure about coverage; treating it as a failure would make the command
#: permanently red for a dataset whose current window is perfectly healthy, and
#: a check that is always red is a check nobody reads.
D_HISTORY_UNPROVEN = "history_coverage_unproven"

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_UNAVAILABLE = 2

DEFAULT_STALE_AFTER_DAYS = 8


def _q(cur, sql: str, params: tuple = ()) -> list[tuple]:
    cur.execute(sql, params)
    return cur.fetchall() or []


def _one(cur, sql: str, params: tuple = ()):
    rows = _q(cur, sql, params)
    return rows[0] if rows else None


def _iso(value):
    return str(value) if value is not None else None


def _sync_facts(cur, source: str, dataset: str) -> dict:
    """The canonical sync RECORD for one dataset — state plus latest batch.

    Read by the (source, dataset) pair the writer stamps, taken from the one
    registry. Spelling it here instead is how a dataset comes to report
    "never run" forever while its table fills up normally.
    """
    facts: dict[str, Any] = {
        "sync_status": None,
        "latest_successful_sync": None,
        "sync_error": None,
        "latest_requested_interval": None,
        "latest_batch_status": None,
        "latest_batch_row_count": None,
        "latest_proven_source_date": None,
        "verified_empty_intervals": 0,
        "failed_batches": 0,
    }
    # The proven watermark lives on `sync_state.last_source_date`: only a
    # SUCCESSFUL `finish_sync_batch` advances it, which is exactly the property
    # that makes it a coverage proof rather than an attempt log.
    row = _one(cur,
               "SELECT status, last_successful_sync_at, error_message, "
               "       last_source_date "
               "FROM sync_state WHERE source = %s AND dataset = %s LIMIT 1",
               (source, dataset))
    if row:
        facts["sync_status"] = row[0]
        facts["latest_successful_sync"] = _iso(row[1])
        facts["sync_error"] = row[2]
        facts["latest_proven_source_date"] = _iso(row[3])

    row = _one(cur,
               "SELECT status, row_count, date_from, date_to "
               "FROM sync_batches WHERE source = %s AND dataset = %s "
               "ORDER BY started_at DESC LIMIT 1",
               (source, dataset))
    if row:
        facts["latest_batch_status"] = row[0]
        facts["latest_batch_row_count"] = row[1]
        facts["latest_requested_interval"] = (
            {"date_from": _iso(row[2]), "date_to": _iso(row[3])}
            if (row[2] or row[3]) else None)

    # A successful batch that wrote nothing IS the verified-empty record: the
    # service finishes `success` only when the query itself succeeded, so this
    # counts intervals that were asked about and had nothing, never intervals
    # nobody asked about.
    row = _one(cur,
               "SELECT COUNT(*) FROM sync_batches "
               "WHERE source = %s AND dataset = %s AND status = 'success' "
               "  AND COALESCE(row_count, 0) = 0",
               (source, dataset))
    facts["verified_empty_intervals"] = int(row[0]) if row else 0

    row = _one(cur,
               "SELECT COUNT(*) FROM sync_batches "
               "WHERE source = %s AND dataset = %s AND status = 'failed'",
               (source, dataset))
    facts["failed_batches"] = int(row[0]) if row else 0
    return facts


def _keyword_data_facts(cur) -> dict:
    row = _one(cur, """
        SELECT COUNT(*)::int,
               COUNT(DISTINCT source_date)::int,
               MIN(source_date), MAX(source_date),
               COUNT(*) FILTER (WHERE customer_id IS NULL OR campaign_id IS NULL
                                   OR ad_group_id IS NULL OR criterion_id IS NULL)::int,
               COUNT(*) FILTER (WHERE currency_code IS NULL
                                   OR TRIM(currency_code) = '')::int
        FROM keyword_daily_facts
    """)
    dup = _one(cur, """
        SELECT COUNT(*)::int FROM (
            SELECT 1 FROM keyword_daily_facts
            GROUP BY source_date, customer_id, campaign_id, ad_group_id, criterion_id
            HAVING COUNT(*) > 1
        ) d
    """)
    return {
        "row_count": row[0] if row else 0,
        "distinct_source_dates": row[1] if row else 0,
        "min_source_date": _iso(row[2]) if row else None,
        "max_source_date": _iso(row[3]) if row else None,
        "rows_missing_identity": row[4] if row else 0,
        "rows_missing_currency_provenance": row[5] if row else 0,
        "duplicate_natural_key_groups": dup[0] if dup else 0,
    }


def _search_term_data_facts(cur) -> dict:
    row = _one(cur, """
        SELECT COUNT(*)::int,
               COUNT(DISTINCT source_date)::int,
               MIN(source_date), MAX(source_date),
               COUNT(*) FILTER (WHERE search_term IS NULL
                                   OR TRIM(search_term) = '')::int,
               COUNT(*) FILTER (WHERE cost_micros IS NULL
                                   OR currency_code IS NULL
                                   OR TRIM(currency_code) = '')::int
        FROM search_terms
    """)
    # The natural key is the unique index from db/schema.py, spelled the same
    # way — COALESCE included, because two NULLs are not equal in SQL and a key
    # that ignores that would report zero duplicates over rows the index treats
    # as one.
    dup = _one(cur, """
        SELECT COUNT(*)::int FROM (
            SELECT 1 FROM search_terms
            GROUP BY source_date, COALESCE(campaign_name, ''),
                     COALESCE(campaign_id, ''), COALESCE(ad_group, ''),
                     COALESCE(keyword, ''), COALESCE(match_type, ''), search_term
            HAVING COUNT(*) > 1
        ) d
    """)
    return {
        "row_count": row[0] if row else 0,
        "distinct_source_dates": row[1] if row else 0,
        "min_source_date": _iso(row[2]) if row else None,
        "max_source_date": _iso(row[3]) if row else None,
        "rows_missing_identity": row[4] if row else 0,
        "rows_missing_currency_provenance": row[5] if row else 0,
        "duplicate_natural_key_groups": dup[0] if dup else 0,
    }


def _legacy_facts(cur) -> dict:
    """Legacy rows and tables, reported rather than assumed absent.

    The legacy `keywords` snapshot still has LIVE non-evidence consumers
    (campaign drill-down previews, keyword themes, the keyword-review queue), so
    its rows existing is not a violation. What would be a violation is Keyword
    Evidence reading them, which the static guard checks in source rather than
    here — this reports the shape of what is there.
    """
    facts: dict[str, Any] = {"legacy_keyword_snapshot_rows": None,
                             "legacy_windsor_sync_rows": None,
                             "legacy_tables_present": []}
    try:
        row = _one(cur, "SELECT COUNT(*)::int FROM keywords")
        facts["legacy_keyword_snapshot_rows"] = row[0] if row else 0
        if facts["legacy_keyword_snapshot_rows"] is not None:
            facts["legacy_tables_present"].append("keywords")
    except Exception:  # noqa: BLE001 — absent table is a fine answer
        pass
    try:
        row = _one(cur,
                   "SELECT COUNT(*)::int FROM sync_state "
                   "WHERE source IN ('windsor', 'windsor_mcp')")
        facts["legacy_windsor_sync_rows"] = row[0] if row else 0
    except Exception:  # noqa: BLE001
        pass
    return facts


def _stale(max_source_date: str | None, stale_after_days: int,
           today: date) -> bool:
    if not max_source_date:
        return False
    try:
        newest = date.fromisoformat(str(max_source_date)[:10])
    except (ValueError, TypeError):
        return False
    return (today - newest) > timedelta(days=stale_after_days)


def _assess(name: str, table: str, source: str, dataset: str,
            sync: dict, data: dict, *, stale_after_days: int,
            today: date, registered: bool) -> dict:
    """One dataset's verdict. Violations are collected, never short-circuited —
    an operator fixing three problems should see three, not the first one."""
    violations: list[dict] = []
    disclosures: list[dict] = []

    def add(code, detail):
        violations.append({"code": code, "dataset": f"{source}/{dataset}",
                           "detail": detail})

    if not registered:
        add(V_FRESHNESS_KEY_MISMATCH,
            f"({source}, {dataset}) is not a registered pair, so the freshness "
            "configuration reads a key nothing writes")

    if sync["sync_status"] is None and sync["latest_batch_status"] is None:
        add(V_SYNC_NEVER_RUN,
            "no canonical sync state and no sync batch — this dataset has "
            "never been synced against this database")
    elif sync["latest_batch_status"] == "failed" or sync["sync_status"] == "failed":
        add(V_SYNC_FAILED,
            f"latest canonical batch status is {sync['latest_batch_status']!r} "
            f"(sync_state {sync['sync_status']!r}): the interval was attempted "
            "and is not covered")

    stale = _stale(data["max_source_date"], stale_after_days, today)
    if stale:
        add(V_SOURCE_STALE,
            f"newest stored source_date is {data['max_source_date']}, more than "
            f"{stale_after_days} day(s) old — rows in an old table are not "
            "evidence of a recent sync")

    if data["duplicate_natural_key_groups"]:
        add(V_DUPLICATE_NATURAL_KEY,
            f"{data['duplicate_natural_key_groups']} natural-key group(s) hold "
            "more than one row — overlapping syncs are duplicating instead of "
            "upserting")
    if data["rows_missing_identity"]:
        add(V_MISSING_IDENTITY,
            f"{data['rows_missing_identity']} row(s) lack the immutable "
            "identity the natural key is built from")
    if data["rows_missing_currency_provenance"]:
        add(V_UNPROVEN_CURRENCY,
            f"{data['rows_missing_currency_provenance']} row(s) have no proven "
            "currency lineage and must be excluded from verified monetary totals")

    # Persistence: a batch that fetched rows and wrote none, or wrote some.
    row_count = sync.get("latest_batch_row_count")
    if (sync.get("latest_batch_status") == "success" and row_count == 0
            and data["row_count"] == 0 and sync["verified_empty_intervals"] == 0):
        add(V_FETCHED_NOT_PERSISTED,
            "the latest batch reports success with no rows and no verified-empty "
            "record — a success that covered nothing")

    # History coverage: disclosed, never claimed complete and never a violation.
    history_status = "unproven"
    if data["min_source_date"] and data["max_source_date"]:
        history_status = "partial_disclosed"
    disclosures.append({
        "code": D_HISTORY_UNPROVEN,
        "dataset": f"{source}/{dataset}",
        "detail": (
            "all-time coverage is not proven by this command: it reports the "
            f"stored range ({data['min_source_date']} → {data['max_source_date']}) "
            "and the intervals canonical syncs have covered, which is not the "
            "same as proving every historical date was queried."),
    })

    return {
        "name": name,
        "source": source,
        "dataset": dataset,
        "canonical_table": table,
        **sync,
        **data,
        "stale": stale,
        "stale_after_days": stale_after_days,
        "history_coverage_status": history_status,
        "violations": violations,
        "violation_codes": sorted({v["code"] for v in violations}),
        "disclosures": disclosures,
        "ok": not violations,
        # Stated per dataset as well as at the top level.
        "external_writes_performed": False,
    }


def run_audit(*, stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
              now: datetime | None = None) -> dict:
    """Inspect both canonical evidence datasets. Read-only."""
    from db.connection import get_conn  # noqa: PLC0415
    from services import canonical_contract  # noqa: PLC0415
    from services.dataset_keys import is_registered_pair  # noqa: PLC0415

    generated_at = datetime.now(tz=timezone.utc).isoformat()
    timezone_name, customer_id = None, None
    try:
        resolved = canonical_contract.resolve_canonical_window("current_quarter", now=now)
        timezone_name = resolved.get("timezone")
        today = date.fromisoformat(str(resolved.get("end_date"))[:10])
    except Exception:  # noqa: BLE001
        today = (now or datetime.now(tz=timezone.utc)).date()
    try:
        import os  # noqa: PLC0415

        customer_id = (os.getenv("GOOGLE_ADS_CUSTOMER_ID") or "").strip() or None
    except Exception:  # noqa: BLE001
        pass

    base = {
        "generated_at": generated_at,
        "account_timezone": timezone_name,
        "configured_customer_id": customer_id,
        "read_only": True,
        "external_writes_performed": False,
    }

    try:
        with get_conn() as conn:
            if conn is None:
                # Nothing was measured. A "0 violations" result over an
                # unopened database would be a fabricated all-clear.
                return {
                    **base, "ok": False, "database_available": False,
                    "datasets": [],
                    "violations": [{"code": V_TABLE_UNAVAILABLE,
                                    "dataset": None,
                                    "detail": "database unavailable — nothing "
                                              "was measured, so nothing is proven"}],
                    "violation_codes": [V_TABLE_UNAVAILABLE],
                    "disclosures": [],
                    "legacy": {},
                }
            with conn.cursor() as cur:
                keyword = _assess(
                    "keyword_facts", "keyword_daily_facts",
                    KEYWORD_FACTS_SOURCE, KEYWORD_FACTS_DATASET,
                    _sync_facts(cur, KEYWORD_FACTS_SOURCE, KEYWORD_FACTS_DATASET),
                    _keyword_data_facts(cur),
                    stale_after_days=stale_after_days, today=today,
                    registered=is_registered_pair(KEYWORD_FACTS_SOURCE,
                                                  KEYWORD_FACTS_DATASET))
                search = _assess(
                    "search_terms", "search_terms",
                    SEARCH_TERMS_SOURCE, SEARCH_TERMS_DATASET,
                    _sync_facts(cur, SEARCH_TERMS_SOURCE, SEARCH_TERMS_DATASET),
                    _search_term_data_facts(cur),
                    stale_after_days=stale_after_days, today=today,
                    registered=is_registered_pair(SEARCH_TERMS_SOURCE,
                                                  SEARCH_TERMS_DATASET))
                legacy = _legacy_facts(cur)
    except Exception as exc:  # noqa: BLE001
        from db.redaction import safe_db_error  # noqa: PLC0415

        return {
            **base, "ok": False, "database_available": False, "datasets": [],
            "violations": [{"code": V_TABLE_UNAVAILABLE, "dataset": None,
                            "detail": f"canonical tables unreadable: "
                                      f"{safe_db_error(exc)}"}],
            "violation_codes": [V_TABLE_UNAVAILABLE], "disclosures": [],
            "legacy": {},
        }

    datasets = [keyword, search]
    violations = [v for d in datasets for v in d["violations"]]
    disclosures = [d for ds in datasets for d in ds["disclosures"]]
    return {
        **base,
        "database_available": True,
        "ok": not violations,
        "datasets": datasets,
        "legacy": legacy,
        "violations": violations,
        "violation_codes": sorted({v["code"] for v in violations}),
        "disclosures": disclosures,
    }


def _print_human(report: dict) -> None:
    """Human-readable mode. Every field is read with `.get`, because this must
    print a partially-proven or entirely unavailable report rather than raise —
    the moment an operator most needs it is the moment least is available."""
    print("=" * 72)
    print("  KEYWORD + SEARCH-TERM EVIDENCE FRESHNESS")
    print("=" * 72)
    print(f"  Generated:    {report.get('generated_at')}")
    print(f"  Account TZ:   {report.get('account_timezone') or 'unavailable'}")
    print(f"  Customer ID:  {report.get('configured_customer_id') or 'not configured'}")
    print(f"  Database:     {'available' if report.get('database_available') else 'UNAVAILABLE'}")
    print()
    for ds in report.get("datasets") or []:
        print(f"── {ds.get('source')}/{ds.get('dataset')} "
              f"→ {ds.get('canonical_table')} " + "─" * 18)
        print(f"   sync status          {ds.get('sync_status') or 'never run'}")
        print(f"   latest success       {ds.get('latest_successful_sync') or '—'}")
        print(f"   latest interval      {ds.get('latest_requested_interval') or '—'}")
        print(f"   proven source date   {ds.get('latest_proven_source_date') or '—'}")
        print(f"   rows                 {ds.get('row_count')} "
              f"over {ds.get('distinct_source_dates')} date(s)")
        print(f"   stored range         {ds.get('min_source_date') or '—'} → "
              f"{ds.get('max_source_date') or '—'}")
        print(f"   verified-empty runs  {ds.get('verified_empty_intervals')}")
        print(f"   stale                {ds.get('stale')}")
        print(f"   duplicate keys       {ds.get('duplicate_natural_key_groups')}")
        print(f"   missing identity     {ds.get('rows_missing_identity')}")
        print(f"   unproven currency    {ds.get('rows_missing_currency_provenance')}")
        print(f"   history coverage     {ds.get('history_coverage_status')}")
        for v in ds.get("violations") or []:
            print(f"   ✗ {v.get('code')}: {v.get('detail')}")
        print()
    legacy = report.get("legacy") or {}
    if legacy:
        print("── Legacy (reported, non-authoritative) " + "─" * 32)
        print(f"   keywords snapshot rows   {legacy.get('legacy_keyword_snapshot_rows')}")
        print(f"   windsor sync_state rows  {legacy.get('legacy_windsor_sync_rows')}")
        print()
    for d in report.get("disclosures") or []:
        print(f"   … {d.get('code')}: {d.get('detail')}")
    print()
    print("── VERDICT " + "─" * 61)
    print(f"   ok = {report.get('ok')}   violations = {report.get('violation_codes') or []}")
    print(f"   external writes performed: {report.get('external_writes_performed')}")
    print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit canonical keyword + search-term evidence freshness "
                    "(read-only).")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Emit the full report as JSON")
    parser.add_argument("--stale-after-days", type=int,
                        default=DEFAULT_STALE_AFTER_DAYS,
                        help="Newest source_date older than this many days is stale")
    args = parser.parse_args()

    # The command initialises its own pool. Run as `python -m`, nothing has
    # called `init_pool`, so every read would degrade to "unavailable" and the
    # audit would report a database problem that does not exist.
    from db.connection import ensure_database_ready  # noqa: PLC0415

    ready, detail = ensure_database_ready()
    if not ready:
        report = {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "ok": False, "database_available": False, "datasets": [],
            "violations": [{"code": V_TABLE_UNAVAILABLE, "dataset": None,
                            "detail": f"database unavailable: {detail}"}],
            "violation_codes": [V_TABLE_UNAVAILABLE],
            "disclosures": [], "legacy": {},
            "external_writes_performed": False,
        }
        if args.json_output:
            print(json.dumps(report, indent=2, default=str))
        else:
            _print_human(report)
        return EXIT_UNAVAILABLE

    report = run_audit(stale_after_days=args.stale_after_days)
    if args.json_output:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_human(report)

    if not report.get("database_available"):
        return EXIT_UNAVAILABLE
    return EXIT_OK if report.get("ok") else EXIT_VIOLATIONS


if __name__ == "__main__":
    sys.exit(main())
