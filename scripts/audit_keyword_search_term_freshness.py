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

Coverage, not the newest row (PR-ADS-156-F1 §1)
----------------------------------------------
The first version of this command derived staleness from ``MAX(source_date)`` —
the newest row in the table. That is the wrong quantity in both directions, and
each direction is a way to certify something untrue:

  * a dataset can be perfectly current and hold no recent rows. Google Ads had
    nothing to report; the interval was queried and came back empty. Judged by
    the newest row, a healthy account with a quiet fortnight looks stale.
  * a dataset can be badly stale and hold recent-looking rows, because rows
    persist and syncs do not. An old successful zero-row batch leaves no row at
    all, so the newest row belongs to whatever ran before it — and the dataset
    reports the freshness of a sync that stopped happening.

So three quantities are published and validated SEPARATELY:

  ``coverage_through``  the furthest date a successful canonical batch has
                        proven — ``MAX(date_to)`` over successful batches. THIS
                        is what freshness is measured from.
  ``data_last_seen``    the newest persisted source row date. Nullable, and
                        never a freshness signal on its own.
  ``verified_empty``    durable, explicit proof that the queried interval
                        returned zero canonical rows — read from the batch
                        column, never inferred from ``success AND row_count=0``.

A stale ``coverage_through`` fails even when the table is legitimately empty,
and a current ``coverage_through`` passes even when the newest row is older.

Current certification vs historical disclosure (PR-ADS-156-F1 §4)
-----------------------------------------------------------------
The identity, currency and duplication checks used to scan the entire evidence
table. That makes quarantined Windsor-era rows — rows nobody will ever repair,
which the evidence services already exclude — permanently block the new
canonical pipeline. A check that can never go green is a check nobody reads.

The report is therefore split. Rows from the canonical Google Ads API source
INSIDE the certified interval are held to the full standard and their defects
are blocking. Everything else is counted, labelled by its source, and reported
as disclosure: visible, never relabelled canonical, and never the reason current
freshness fails. Nothing historical is rewritten, backfilled or deleted.

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

from analysis.legacy_source_guard import scan_legacy_sources  # noqa: E402
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
V_UNPROVEN_EMPTY = "unproven_empty_interval"
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

#: Likewise: legacy rows outside the certified interval, or inside it but
#: stamped with another provenance. Counted and labelled, never blocking, and
#: never relabelled canonical.
D_LEGACY_ROWS = "legacy_rows_present"

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_UNAVAILABLE = 2

DEFAULT_STALE_AFTER_DAYS = 8

#: The one provenance value a row must carry to be certified as canonical
#: evidence. It is the same string the evidence services already quarantine
#: against, so this command and the pages agree on what "canonical" means.
CANONICAL_PROVENANCE = "google_ads_api"


def _q(cur, sql: str, params: tuple = ()) -> list[tuple]:
    cur.execute(sql, params)
    return cur.fetchall() or []


def _one(cur, sql: str, params: tuple = ()):
    rows = _q(cur, sql, params)
    return rows[0] if rows else None


def _iso(value):
    return str(value) if value is not None else None


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _sync_facts(cur, source: str, dataset: str) -> dict:
    """The canonical sync RECORD for one dataset — state, coverage and counters.

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
        "latest_batch_fetched_count": None,
        "latest_batch_prepared_count": None,
        "latest_batch_rejected_count": None,
        "latest_batch_verified_empty": False,
        "latest_proven_source_date": None,
        "coverage_through": None,
        "certified_interval": None,
        "verified_empty": False,
        "verified_empty_intervals": 0,
        "unproven_empty_intervals": 0,
        "failed_batches": 0,
    }
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

    # The LATEST attempt, whatever became of it. Its durable counters are what
    # make the persistence violations reachable (§3) instead of guessed at.
    row = _one(cur,
               "SELECT status, row_count, date_from, date_to, "
               "       fetched_count, prepared_count, rejected_count, "
               "       COALESCE(verified_empty, FALSE) "
               "FROM sync_batches WHERE source = %s AND dataset = %s "
               "ORDER BY started_at DESC, id DESC LIMIT 1",
               (source, dataset))
    if row:
        facts["latest_batch_status"] = row[0]
        facts["latest_batch_row_count"] = row[1]
        facts["latest_requested_interval"] = (
            {"date_from": _iso(row[2]), "date_to": _iso(row[3])}
            if (row[2] or row[3]) else None)
        facts["latest_batch_fetched_count"] = row[4]
        facts["latest_batch_prepared_count"] = row[5]
        facts["latest_batch_rejected_count"] = row[6]
        facts["latest_batch_verified_empty"] = bool(row[7])

    # §1 — coverage_through is MAX(date_to) over SUCCESSFUL batches, not the
    # date_to of whichever batch ran most recently. A backfill repairing an old
    # month runs last and covers an older range; taking its date_to would
    # RETRACT proven coverage the daily runs had already established.
    row = _one(cur,
               "SELECT MAX(date_to) FROM sync_batches "
               "WHERE source = %s AND dataset = %s AND status = 'success' "
               "  AND date_to IS NOT NULL",
               (source, dataset))
    facts["coverage_through"] = _iso(row[0]) if row and row[0] else None

    # The interval this report certifies: the range of the most recent
    # SUCCESSFUL batch. Rows inside it are held to the full standard; rows
    # outside it are historical disclosure (§4).
    row = _one(cur,
               "SELECT date_from, date_to, COALESCE(verified_empty, FALSE) "
               "FROM sync_batches WHERE source = %s AND dataset = %s "
               "  AND status = 'success' AND date_to IS NOT NULL "
               "ORDER BY date_to DESC, started_at DESC, id DESC LIMIT 1",
               (source, dataset))
    if row:
        facts["certified_interval"] = {"date_from": _iso(row[0]),
                                       "date_to": _iso(row[1])}
        facts["verified_empty"] = bool(row[2])

    # §2 — counted from the DURABLE marker only. `status = 'success' AND
    # row_count = 0` is the shape historical batches share without sharing the
    # meaning: some of them were recorded while the evidence pipeline was
    # unavailable. Those are counted separately, as what they are — successful
    # zero-row batches nobody proved anything about.
    row = _one(cur,
               "SELECT COUNT(*) FILTER (WHERE COALESCE(verified_empty, FALSE))::int, "
               "       COUNT(*) FILTER (WHERE NOT COALESCE(verified_empty, FALSE))::int "
               "FROM sync_batches "
               "WHERE source = %s AND dataset = %s AND status = 'success' "
               "  AND COALESCE(row_count, 0) = 0",
               (source, dataset))
    facts["verified_empty_intervals"] = int(row[0]) if row else 0
    facts["unproven_empty_intervals"] = int(row[1]) if row else 0

    row = _one(cur,
               "SELECT COUNT(*) FROM sync_batches "
               "WHERE source = %s AND dataset = %s AND status = 'failed'",
               (source, dataset))
    facts["failed_batches"] = int(row[0]) if row else 0
    return facts


# ── Data facts, split into certified-current and historical (§4) ────────────
_KEYWORD_MEASURES = """
    SELECT COUNT(*)::int,
           COUNT(DISTINCT source_date)::int,
           MIN(source_date), MAX(source_date),
           COUNT(*) FILTER (WHERE customer_id IS NULL OR TRIM(customer_id) = ''
                               OR campaign_id IS NULL OR TRIM(campaign_id) = ''
                               OR ad_group_id IS NULL OR TRIM(ad_group_id) = ''
                               OR criterion_id IS NULL OR TRIM(criterion_id) = '')::int,
           COUNT(*) FILTER (WHERE cost_micros IS NULL
                               OR currency_code IS NULL
                               OR TRIM(currency_code) = '')::int
    FROM keyword_daily_facts WHERE {scope}
"""

_KEYWORD_DUPLICATES = """
    SELECT COUNT(*)::int FROM (
        SELECT 1 FROM keyword_daily_facts WHERE {scope}
        GROUP BY source_date, customer_id, campaign_id, ad_group_id, criterion_id
        HAVING COUNT(*) > 1
    ) d
"""

# §5 — canonical search-term identity is the ACCOUNT, the CAMPAIGN, the AD GROUP
# and the term itself. A non-empty `search_term` alone was never proof that a
# row describes a knowable event: the natural key is built from these fields, so
# a row missing one cannot be attributed or deterministically upserted either.
_SEARCH_TERM_MEASURES = """
    SELECT COUNT(*)::int,
           COUNT(DISTINCT source_date)::int,
           MIN(source_date), MAX(source_date),
           COUNT(*) FILTER (WHERE search_term IS NULL OR TRIM(search_term) = ''
                               OR campaign_id IS NULL OR TRIM(campaign_id) = ''
                               OR ad_group IS NULL OR TRIM(ad_group) = ''
                               OR customer_id IS NULL OR TRIM(customer_id) = '')::int,
           COUNT(*) FILTER (WHERE cost_micros IS NULL
                               OR currency_code IS NULL
                               OR TRIM(currency_code) = '')::int
    FROM search_terms WHERE {scope}
"""

# The natural key is the unique index from db/schema.py, spelled the same way —
# COALESCE included, because two NULLs are not equal in SQL and a key that
# ignores that would report zero duplicates over rows the index treats as one.
_SEARCH_TERM_DUPLICATES = """
    SELECT COUNT(*)::int FROM (
        SELECT 1 FROM search_terms WHERE {scope}
        GROUP BY source_date, COALESCE(campaign_name, ''),
                 COALESCE(campaign_id, ''), COALESCE(ad_group, ''),
                 COALESCE(keyword, ''), COALESCE(match_type, ''), search_term
        HAVING COUNT(*) > 1
    ) d
"""

# COALESCE, not a bare comparison: `source_system` is nullable, and
# `NOT (NULL = 'google_ads_api' AND …)` evaluates to NULL, so an unlabelled row
# would fall out of BOTH the certified and the historical scope and be counted
# nowhere. A row that appears in no bucket is the quietest way to lose evidence.
_CANONICAL_SCOPE = ("COALESCE(source_system, '') = %s AND source_date >= %s "
                    "AND source_date <= %s")


def _measure(cur, measures_sql: str, dup_sql: str, scope: str,
             params: tuple) -> dict:
    row = _one(cur, measures_sql.format(scope=scope), params)
    dup = _one(cur, dup_sql.format(scope=scope), params)
    return {
        "row_count": row[0] if row else 0,
        "distinct_source_dates": row[1] if row else 0,
        "min_source_date": _iso(row[2]) if row else None,
        "max_source_date": _iso(row[3]) if row else None,
        "rows_missing_identity": row[4] if row else 0,
        "rows_missing_currency_provenance": row[5] if row else 0,
        "duplicate_natural_key_groups": dup[0] if dup else 0,
    }


def _by_source_system(cur, table: str) -> list[dict]:
    """Row counts per provenance label — the disclosure that says WHICH legacy
    system the historical rows came from, rather than lumping them together."""
    rows = _q(cur, f"SELECT COALESCE(source_system, 'unknown'), COUNT(*)::int "
                   f"FROM {table} GROUP BY 1 ORDER BY 2 DESC")
    return [{"source_system": r[0], "rows": r[1]} for r in rows]


def _data_facts(cur, table: str, measures_sql: str, dup_sql: str,
                cert_from: date | None, cert_to: date | None) -> dict:
    """Everything the table can say about itself, split into what this run
    CERTIFIES and what it merely DISCLOSES.

    Certified: canonical-provenance rows inside the certified interval. Their
    defects are blocking, because they are the rows a current sync just claimed
    to have produced.

    Disclosed: everything else — rows outside the interval, and rows inside it
    stamped with another provenance. Counted and labelled, never relabelled, and
    never the reason current freshness fails. Repairing or deleting them is a
    different piece of work and is deliberately not attempted here.
    """
    if cert_from and cert_to:
        params = (CANONICAL_PROVENANCE, cert_from, cert_to)
        current = _measure(cur, measures_sql, dup_sql, _CANONICAL_SCOPE, params)
        historical = _measure(cur, measures_sql, dup_sql,
                              f"NOT ({_CANONICAL_SCOPE})", params)
        inside = _one(cur, f"SELECT COUNT(*)::int FROM {table} "
                           f"WHERE source_date >= %s AND source_date <= %s "
                           f"  AND COALESCE(source_system, '') <> %s",
                      (cert_from, cert_to, CANONICAL_PROVENANCE))
        historical["rows_inside_interval_non_canonical"] = inside[0] if inside else 0
    else:
        # Nothing has been certified, so nothing is certified. Every row is
        # historical until a successful canonical batch says otherwise — which
        # is a truthful "unknown", not a silent pass.
        current = _measure(cur, measures_sql, dup_sql, "FALSE", ())
        historical = _measure(cur, measures_sql, dup_sql, "TRUE", ())
        historical["rows_inside_interval_non_canonical"] = 0

    historical["by_source_system"] = _by_source_system(cur, table)
    total = _one(cur, f"SELECT COUNT(*)::int, MIN(source_date), MAX(source_date) "
                      f"FROM {table}")
    return {
        # Whole-table shape, for context only.
        "row_count": total[0] if total else 0,
        "min_source_date": _iso(total[1]) if total else None,
        "max_source_date": _iso(total[2]) if total else None,
        # §1 — the newest stored row. Reported, and NOT a freshness signal.
        "data_last_seen": _iso(total[2]) if total else None,
        "current": current,
        "historical": historical,
    }


def _keyword_data_facts(cur, cert_from, cert_to) -> dict:
    return _data_facts(cur, "keyword_daily_facts", _KEYWORD_MEASURES,
                       _KEYWORD_DUPLICATES, cert_from, cert_to)


def _search_term_data_facts(cur, cert_from, cert_to) -> dict:
    return _data_facts(cur, "search_terms", _SEARCH_TERM_MEASURES,
                       _SEARCH_TERM_DUPLICATES, cert_from, cert_to)


def _legacy_facts(cur) -> dict:
    """Legacy rows and tables, reported rather than assumed absent.

    The legacy `keywords` snapshot still has LIVE non-evidence consumers
    (campaign drill-down previews, keyword themes, the keyword-review queue), so
    its rows existing is not a violation. What WOULD be a violation is a
    production path reading them as evidence, and that is checked in source by
    the shared guard rather than here — this reports the shape of what is there.
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


def _stale(coverage_through: str | None, stale_after_days: int,
           today: date) -> bool:
    """Whether PROVEN COVERAGE has fallen behind.

    §1 — the input is ``coverage_through``, never the newest stored row. An
    empty interval that was genuinely queried is current; a table of old rows
    with no recent successful batch is not, however many rows it holds.

    ``None`` (never covered) is NOT reported as stale here: that is
    ``canonical_sync_never_run``, a different and more specific statement, and
    reporting both would describe one problem twice.
    """
    covered = _as_date(coverage_through)
    if covered is None:
        return False
    return (today - covered) > timedelta(days=stale_after_days)


def _assess(name: str, table: str, source: str, dataset: str,
            sync: dict, data: dict, *, stale_after_days: int,
            today: date, registered: bool) -> dict:
    """One dataset's verdict. Violations are collected, never short-circuited —
    an operator fixing three problems should see three, not the first one."""
    violations: list[dict] = []
    disclosures: list[dict] = []
    current = data.get("current") or {}
    historical = data.get("historical") or {}

    def add(code, detail):
        violations.append({"code": code, "dataset": f"{source}/{dataset}",
                           "detail": detail})

    if not registered:
        add(V_FRESHNESS_KEY_MISMATCH,
            f"({source}, {dataset}) is not a registered pair, so the freshness "
            "configuration reads a key nothing writes")

    if sync.get("sync_status") is None and sync.get("latest_batch_status") is None:
        add(V_SYNC_NEVER_RUN,
            "no canonical sync state and no sync batch — this dataset has "
            "never been synced against this database")
    elif (sync.get("latest_batch_status") == "failed"
          or sync.get("sync_status") == "failed"):
        add(V_SYNC_FAILED,
            f"latest canonical batch status is {sync.get('latest_batch_status')!r} "
            f"(sync_state {sync.get('sync_status')!r}): the interval was attempted "
            "and is not covered")
    elif sync.get("coverage_through") is None:
        # A batch exists but none of them succeeded with a date range, so
        # nothing has been proven covered — distinct from "never run".
        add(V_SYNC_NEVER_RUN,
            "no successful canonical batch carries a covered interval — this "
            "dataset has attempts but no proven coverage")

    stale = _stale(sync.get("coverage_through"), stale_after_days, today)
    if stale:
        add(V_SOURCE_STALE,
            f"proven coverage runs only through {sync.get('coverage_through')}, "
            f"more than {stale_after_days} day(s) old (newest stored row: "
            f"{data.get('data_last_seen') or 'none'}) — rows in an old table are "
            "not evidence of a recent sync, and an old verified-empty interval "
            "is not evidence of a current one")

    # ── Persistence, from the DURABLE counters on the latest batch (§3) ──────
    fetched = sync.get("latest_batch_fetched_count")
    prepared = sync.get("latest_batch_prepared_count")
    rejected = sync.get("latest_batch_rejected_count")
    written = sync.get("latest_batch_row_count")

    if fetched is not None and fetched > 0 and (written or 0) == 0:
        add(V_FETCHED_NOT_PERSISTED,
            f"the latest batch fetched {fetched} row(s) and wrote 0 — the pull "
            "succeeded and the persistence did not, which leaves the interval "
            "recorded but not stored")
    elif (fetched is not None and prepared is not None and written is not None
          and prepared != written):
        add(V_PARTIAL_PERSISTENCE,
            f"the latest batch prepared {prepared} row(s) and wrote {written} — "
            "a partial write is not a covered interval")
    if rejected:
        add(V_PARTIAL_PERSISTENCE,
            f"the latest batch rejected {rejected} fetched row(s) — rows the "
            "source returned that the database does not hold are a gap in the "
            "evidence, whatever the reason")

    # §2 — a successful zero-row batch WITHOUT the durable marker proves
    # nothing. It is the exact shape of the historical batches recorded while
    # the evidence pipeline was unavailable, and inferring emptiness from it is
    # the false green this release closes.
    # `fetched` must be absent or zero: a batch that fetched rows and wrote none
    # is a PERSISTENCE failure, already reported above. Emitting both would
    # describe one problem twice and send an operator looking for an empty
    # account when the account was not empty at all.
    if (sync.get("latest_batch_status") == "success"
            and (written or 0) == 0
            and not fetched
            and not sync.get("latest_batch_verified_empty")
            and current.get("row_count", 0) == 0):
        add(V_UNPROVEN_EMPTY,
            "the latest batch succeeded with no rows written and no durable "
            "verified-empty marker — 'success with row_count 0' is not proof "
            "that the source returned nothing")

    # ── Certified-interval data quality (§4: current rows only) ─────────────
    if current.get("duplicate_natural_key_groups"):
        add(V_DUPLICATE_NATURAL_KEY,
            f"{current['duplicate_natural_key_groups']} natural-key group(s) in "
            "the certified interval hold more than one row — overlapping syncs "
            "are duplicating instead of upserting")
    if current.get("rows_missing_identity"):
        add(V_MISSING_IDENTITY,
            f"{current['rows_missing_identity']} canonical row(s) in the "
            "certified interval lack the immutable identity the natural key is "
            "built from (account, campaign, ad group, term)")
    if current.get("rows_missing_currency_provenance"):
        add(V_UNPROVEN_CURRENCY,
            f"{current['rows_missing_currency_provenance']} canonical row(s) in "
            "the certified interval have no proven currency lineage and must be "
            "excluded from verified monetary totals")

    # ── Disclosures ─────────────────────────────────────────────────────────
    history_status = "unproven"
    if data.get("min_source_date") and data.get("max_source_date"):
        history_status = "partial_disclosed"
    disclosures.append({
        "code": D_HISTORY_UNPROVEN,
        "dataset": f"{source}/{dataset}",
        "detail": (
            "all-time coverage is not proven by this command: it reports the "
            f"stored range ({data.get('min_source_date')} → "
            f"{data.get('max_source_date')}) and the intervals canonical syncs "
            "have covered, which is not the same as proving every historical "
            "date was queried."),
    })
    if historical.get("row_count"):
        labels = ", ".join(
            f"{entry['source_system']}={entry['rows']}"
            for entry in (historical.get("by_source_system") or []))
        disclosures.append({
            "code": D_LEGACY_ROWS,
            "dataset": f"{source}/{dataset}",
            "detail": (
                f"{historical['row_count']} row(s) fall outside the certified "
                f"interval or carry a non-canonical provenance ({labels or 'unlabelled'}); "
                f"{historical.get('rows_missing_identity', 0)} of them lack full "
                f"identity and {historical.get('rows_missing_currency_provenance', 0)} "
                "lack currency lineage. Reported as history: they are not "
                "relabelled canonical, not repaired here, and do not fail "
                "current freshness."),
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


def _legacy_source_violations() -> list[dict]:
    """§3 — production code still reading a retired evidence source.

    Executed through the SHARED helper, which is the same function the
    regression suite asserts over. Before it existed, this violation code was
    declared here and raised nowhere: in JSON that reads as a check that ran and
    passed, which is the most expensive kind of nothing.
    """
    return [{"code": V_LEGACY_SOURCE_ACTIVE, "dataset": None,
             "detail": f"{finding['detail']} ({finding['reason']}) — not in the "
                       "documented legacy-access allowlist"}
            for finding in scan_legacy_sources()]


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

    # The static scan needs no database, so it runs regardless — an unreadable
    # database is not a reason to stop reporting a legacy read that is right
    # there in the source.
    legacy_violations = _legacy_source_violations()

    base = {
        "generated_at": generated_at,
        "account_timezone": timezone_name,
        "configured_customer_id": customer_id,
        "read_only": True,
        "external_writes_performed": False,
    }

    def _unavailable(detail: str) -> dict:
        violations = [{"code": V_TABLE_UNAVAILABLE, "dataset": None,
                       "detail": detail}, *legacy_violations]
        return {
            **base, "ok": False, "database_available": False, "datasets": [],
            "violations": violations,
            "violation_codes": sorted({v["code"] for v in violations}),
            "disclosures": [], "legacy": {},
            "legacy_source_findings": legacy_violations,
        }

    try:
        with get_conn() as conn:
            if conn is None:
                # Nothing was measured. A "0 violations" result over an
                # unopened database would be a fabricated all-clear.
                return _unavailable("database unavailable — nothing was "
                                    "measured, so nothing is proven")
            with conn.cursor() as cur:
                kw_sync = _sync_facts(cur, KEYWORD_FACTS_SOURCE, KEYWORD_FACTS_DATASET)
                st_sync = _sync_facts(cur, SEARCH_TERMS_SOURCE, SEARCH_TERMS_DATASET)
                kw_from, kw_to = _certified_bounds(kw_sync)
                st_from, st_to = _certified_bounds(st_sync)
                keyword = _assess(
                    "keyword_facts", "keyword_daily_facts",
                    KEYWORD_FACTS_SOURCE, KEYWORD_FACTS_DATASET,
                    kw_sync, _keyword_data_facts(cur, kw_from, kw_to),
                    stale_after_days=stale_after_days, today=today,
                    registered=is_registered_pair(KEYWORD_FACTS_SOURCE,
                                                  KEYWORD_FACTS_DATASET))
                search = _assess(
                    "search_terms", "search_terms",
                    SEARCH_TERMS_SOURCE, SEARCH_TERMS_DATASET,
                    st_sync, _search_term_data_facts(cur, st_from, st_to),
                    stale_after_days=stale_after_days, today=today,
                    registered=is_registered_pair(SEARCH_TERMS_SOURCE,
                                                  SEARCH_TERMS_DATASET))
                legacy = _legacy_facts(cur)
    except Exception as exc:  # noqa: BLE001
        from db.redaction import safe_db_error  # noqa: PLC0415

        return _unavailable(f"canonical tables unreadable: {safe_db_error(exc)}")

    datasets = [keyword, search]
    violations = [v for d in datasets for v in d["violations"]] + legacy_violations
    disclosures = [d for ds in datasets for d in ds["disclosures"]]
    return {
        **base,
        "database_available": True,
        "ok": not violations,
        "datasets": datasets,
        "legacy": legacy,
        "legacy_source_findings": legacy_violations,
        "violations": violations,
        "violation_codes": sorted({v["code"] for v in violations}),
        "disclosures": disclosures,
    }


def _certified_bounds(sync: dict) -> tuple[date | None, date | None]:
    interval = sync.get("certified_interval") or {}
    return _as_date(interval.get("date_from")), _as_date(interval.get("date_to"))


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
        current = ds.get("current") or {}
        historical = ds.get("historical") or {}
        print(f"── {ds.get('source')}/{ds.get('dataset')} "
              f"→ {ds.get('canonical_table')} " + "─" * 18)
        print(f"   sync status          {ds.get('sync_status') or 'never run'}")
        print(f"   latest success       {ds.get('latest_successful_sync') or '—'}")
        print(f"   latest interval      {ds.get('latest_requested_interval') or '—'}")
        print(f"   coverage through     {ds.get('coverage_through') or '—'}   "
              f"(freshness is measured from THIS)")
        print(f"   data last seen       {ds.get('data_last_seen') or 'none'}   "
              f"(newest stored row — never a freshness signal)")
        print(f"   verified empty       {ds.get('verified_empty')}")
        print(f"   certified interval   {ds.get('certified_interval') or '—'}")
        print(f"   latest batch counts  fetched={ds.get('latest_batch_fetched_count')} "
              f"prepared={ds.get('latest_batch_prepared_count')} "
              f"written={ds.get('latest_batch_row_count')} "
              f"rejected={ds.get('latest_batch_rejected_count')}")
        print(f"   certified rows       {current.get('row_count')} "
              f"over {current.get('distinct_source_dates')} date(s)")
        print(f"   historical rows      {historical.get('row_count')} "
              f"(disclosed, non-blocking)")
        print(f"   verified-empty runs  {ds.get('verified_empty_intervals')} "
              f"(unproven zero-row: {ds.get('unproven_empty_intervals')})")
        print(f"   stale                {ds.get('stale')}")
        print(f"   duplicate keys       {current.get('duplicate_natural_key_groups')}")
        print(f"   missing identity     {current.get('rows_missing_identity')}")
        print(f"   unproven currency    {current.get('rows_missing_currency_provenance')}")
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
    for v in report.get("legacy_source_findings") or []:
        print(f"   ✗ {v.get('code')}: {v.get('detail')}")
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
                        help="Proven coverage older than this many days is stale")
    args = parser.parse_args()

    # The command initialises its own pool. Run as `python -m`, nothing has
    # called `init_pool`, so every read would degrade to "unavailable" and the
    # audit would report a database problem that does not exist.
    from db.connection import ensure_database_ready  # noqa: PLC0415

    ready, detail = ensure_database_ready()
    if not ready:
        legacy_violations = _legacy_source_violations()
        violations = [{"code": V_TABLE_UNAVAILABLE, "dataset": None,
                       "detail": f"database unavailable: {detail}"},
                      *legacy_violations]
        report = {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "ok": False, "database_available": False, "datasets": [],
            "violations": violations,
            "violation_codes": sorted({v["code"] for v in violations}),
            "disclosures": [], "legacy": {},
            "legacy_source_findings": legacy_violations,
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
