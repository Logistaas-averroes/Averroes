"""
services/search_term_sync_service.py

PR-ADS-156 §4 — THE canonical durable search-term synchronisation service.

Why this module exists
----------------------
Search terms were persisted by three schedulers, each of which had grown its own
copy of the same six steps: pull a window from the connector, open a
``google_ads_api/search_terms`` sync batch, call ``write_search_terms``, decide
whether the result counted as success, choose a watermark, finish the batch.

Three copies meant three sets of rules, and they had already drifted:

* the **daily** run pulled a 2-day window, the **weekly** run 60 days, the
  **monthly** run 30 — three different recovery windows is fine, three different
  *definitions of success* is not;
* the weekly path recorded a zero-row pull as ``success`` while writing the
  error message "evidence pipeline unavailable", so a legitimately empty
  interval and a broken one produced the same durable record;
* only the monthly path treated a non-empty pull that wrote nothing as fatal;
* none of them reported how many rows were prepared or rejected, so
  "we wrote 0" could not be told apart from "there was nothing to write".

Everything scheduled now calls :func:`sync_search_terms` (or
:func:`sync_recent_search_terms`). Different triggers may ask for different
windows; none of them re-implements what a successful sync means.

The verified-empty distinction
------------------------------
A Google Ads query that succeeds and returns no rows is a **measurement**: it
proves the interval was asked about and had no eligible query data. A query that
fails returns no rows too. Collapsing the two is what let an outage look like a
quiet week. A successful empty interval is reported as ``verified_empty=True``
and advances the coverage watermark; a failure never does either.

Row absence is therefore never treated as proof of failure, and failure is never
treated as proof of absence.

Read-only against Google Ads. The only writes are to the local
``search_terms`` table and to the sync-tracking tables.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta

from services.dataset_keys import SEARCH_TERMS_DATASET, SEARCH_TERMS_SOURCE

logger = logging.getLogger(__name__)

#: Rolling recovery window, in inclusive account-local days, used when a caller
#: does not name an explicit range. Fourteen days by default so a fortnight of
#: missed daily runs is recovered without a manual backfill, and so a late
#: Google Ads restatement inside that window updates the existing natural-key
#: row rather than being lost. Overlapping runs are idempotent by construction —
#: the writer upserts on the natural key — so a wider window costs time, never
#: correctness.
DEFAULT_LOOKBACK_DAYS = max(
    14, int(os.getenv("SEARCH_TERM_SYNC_LOOKBACK_DAYS", "14") or 14))

#: Why a fetched row could not be stored durably. Reported, never silently
#: dropped: a row the pull returned and the database does not hold is a gap in
#: the evidence, whatever the reason.
REJECT_BLANK_SEARCH_TERM = "blank_search_term"
REJECT_UNPARSEABLE_DATE = "unparseable_source_date"

_DATE_KEYS = ("source_date", "date")


def _account_today(now: datetime | None = None) -> date:
    """Today in the ACCOUNT's calendar, which is the calendar Google Ads reports
    against. Resolved through the one canonical helper rather than ``date.today``
    so a window here cannot mean a different day from a window anywhere else."""
    try:
        from services import canonical_contract  # noqa: PLC0415

        resolved = canonical_contract.resolve_canonical_window("current_quarter", now=now)
        end = resolved.get("end_date")
        if end:
            return date.fromisoformat(str(end)[:10])
    except Exception as exc:  # noqa: BLE001
        logger.debug("account-local today unavailable (%s); using UTC date", exc)
    return (now or datetime.utcnow()).date()


def _parse_source_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10].strip())
    except (ValueError, TypeError):
        return None


def classify_rows(rows: list | None) -> tuple[list, dict]:
    """Split fetched rows into what can be stored durably and what cannot.

    Applies the writer's OWN two rejection rules ahead of the write, because
    ``write_search_terms`` returns a single integer: without this the service
    could report "wrote 12 of 12" while the connector had handed it 15 rows and
    three had been dropped inside the writer. A count that describes only the
    rows that survived is not a measurement of the pull.
    """
    prepared, rejected = [], {}
    for row in rows or []:
        term = (row.get("search_term") or "").strip()
        if not term:
            rejected[REJECT_BLANK_SEARCH_TERM] = rejected.get(REJECT_BLANK_SEARCH_TERM, 0) + 1
            continue
        source_date = None
        for key in _DATE_KEYS:
            source_date = _parse_source_date(row.get(key))
            if source_date is not None:
                break
        if source_date is None:
            rejected[REJECT_UNPARSEABLE_DATE] = rejected.get(REJECT_UNPARSEABLE_DATE, 0) + 1
            continue
        prepared.append(row)
    return prepared, rejected


def _max_source_date(rows: list) -> date | None:
    dates = []
    for row in rows or []:
        for key in _DATE_KEYS:
            parsed = _parse_source_date(row.get(key))
            if parsed is not None:
                dates.append(parsed)
                break
    return max(dates) if dates else None


def _result(**kwargs) -> dict:
    """One result shape, whatever happened — so a caller never has to test for
    the presence of a key before reading a count."""
    base = {
        "ok": False,
        "source": SEARCH_TERMS_SOURCE,
        "dataset": SEARCH_TERMS_DATASET,
        "batch_id": None,
        "date_from": None,
        "date_to": None,
        "fetched": 0,
        "prepared": 0,
        "written": 0,
        "rejected": 0,
        "rejected_reasons": {},
        "skipped": 0,
        "verified_empty": False,
        "latest_source_date": None,
        "db_unavailable": False,
        "error": None,
        # Stated on every result, successful or not. This service reads Google
        # Ads and writes only local tables.
        "external_writes_performed": False,
    }
    base.update(kwargs)
    return base


def sync_search_terms(date_from: date, date_to: date, sync_type: str, *,
                      run_id: int | None = None,
                      include_rows: bool = False) -> dict:
    """Pull ``[date_from, date_to]`` from the direct Google Ads API and upsert
    durable search terms. Creates and finishes a
    ``google_ads_api/search_terms`` sync batch.

    Fails closed. A pull that raises, a database that cannot open a batch, rows
    fetched but none written, a partial write, or a row the writer could not
    store all produce ``ok=False`` and a ``failed`` batch — which is what keeps
    the coverage watermark from advancing over an interval nobody proved.

    A successful pull returning zero rows produces ``ok=True`` with
    ``verified_empty=True``: the interval WAS queried, and had no eligible query
    data. That is a fact about the account, not a fault in the pipeline.

    ``include_rows`` returns the prepared rows under ``rows``. It exists so a
    caller that needs the same window for analysis — the daily junk-term check —
    can work from the rows this pull already fetched instead of asking Google
    Ads for them a second time. Off by default: a large row list should travel
    only where someone asked for it.
    """
    import db.writers as w  # noqa: PLC0415

    span = {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()}
    if date_from > date_to:
        return _result(error=f"invalid range: {date_from} > {date_to}", **span)

    batch_id = w.start_sync_batch(
        source=SEARCH_TERMS_SOURCE, dataset=SEARCH_TERMS_DATASET,
        sync_type=sync_type, date_from=date_from, date_to=date_to, run_id=run_id)
    if not batch_id:
        # No durable tracking record means the run cannot be proven later, so
        # Google Ads is not called at all: an untracked pull is indistinguishable
        # from one that never happened.
        msg = "sync-batch creation failed — tracking unavailable (database down?)"
        logger.error("search-term sync (%s → %s): %s", date_from, date_to, msg)
        return _result(db_unavailable=True, error=msg, **span)

    try:
        from connectors.google_ads_source import pull_search_terms_range  # noqa: PLC0415

        rows = pull_search_terms_range(date_from.isoformat(), date_to.isoformat())
    except Exception as exc:  # noqa: BLE001
        logger.error("search-term pull failed (%s → %s): %s", date_from, date_to, exc)
        w.finish_sync_batch(batch_id=batch_id, status="failed", row_count=0,
                            error_message=f"search-term pull failed: {exc}"[:1000])
        # NOT verified_empty. The pull returned no rows because it failed, and
        # the whole point of that flag is to mean the opposite.
        return _result(batch_id=batch_id, error=str(exc), **span)

    fetched = len(rows or [])
    prepared_rows, rejected_reasons = classify_rows(rows)
    prepared = len(prepared_rows)
    rejected = sum(rejected_reasons.values())

    if fetched == 0:
        # Verified empty: asked, answered, nothing there. The watermark advances
        # because the interval is now proven — that is the difference between
        # this and a failure, and it is the only reason the flag exists.
        w.finish_sync_batch(batch_id=batch_id, status="success", row_count=0,
                            last_source_date=date_to)
        logger.info("search-term sync (%s → %s): verified empty (0 rows)",
                    date_from, date_to)
        return _result(ok=True, batch_id=batch_id, verified_empty=True,
                       latest_source_date=None,
                       **({"rows": []} if include_rows else {}), **span)

    written = w.write_search_terms(run_id, prepared_rows, sync_batch_id=batch_id)
    written = int(written or 0)

    error = None
    if prepared and written == 0:
        error = (f"pulled {fetched} search-term row(s) but wrote 0 — "
                 "persistence failed")
    elif written != prepared:
        error = f"wrote {written} of {prepared} prepared row(s) — partial persistence"
    elif rejected:
        error = (f"{rejected} fetched row(s) could not be stored "
                 f"({', '.join(f'{k}={v}' for k, v in sorted(rejected_reasons.items()))})")

    ok = error is None
    latest = _max_source_date(prepared_rows) if ok else None
    w.finish_sync_batch(
        batch_id=batch_id,
        status="success" if ok else "failed",
        row_count=written,
        # A failed sync must never advance the proven-coverage watermark: the
        # interval was attempted, not covered.
        last_source_date=(latest or date_to) if ok else None,
        error_message=None if ok else error[:1000])

    if ok:
        logger.info("search-term sync (%s → %s): wrote %d row(s), latest %s",
                    date_from, date_to, written, latest)
    else:
        logger.error("search-term sync (%s → %s): %s", date_from, date_to, error)

    return _result(ok=ok, batch_id=batch_id, fetched=fetched, prepared=prepared,
                   written=written, rejected=rejected,
                   rejected_reasons=rejected_reasons, skipped=rejected,
                   verified_empty=False, error=error,
                   latest_source_date=latest.isoformat() if latest else None,
                   **({"rows": prepared_rows} if include_rows else {}), **span)


def sync_recent_search_terms(sync_type: str = "daily", *,
                             days: int = DEFAULT_LOOKBACK_DAYS,
                             now: datetime | None = None,
                             run_id: int | None = None,
                             include_rows: bool = False) -> dict:
    """Rolling recovery window: today plus the previous ``days - 1`` account-local
    dates.

    A missed run is recovered by the next one rather than leaving a permanent
    hole, because the window is wider than the interval between runs and the
    write is an upsert on the natural key. Re-running it changes nothing but the
    updated timestamps.
    """
    days = max(1, int(days or DEFAULT_LOOKBACK_DAYS))
    end = _account_today(now)
    start = end - timedelta(days=days - 1)
    return sync_search_terms(start, end, sync_type, run_id=run_id,
                             include_rows=include_rows)


__all__ = [
    "DEFAULT_LOOKBACK_DAYS",
    "REJECT_BLANK_SEARCH_TERM", "REJECT_UNPARSEABLE_DATE",
    "classify_rows", "sync_search_terms", "sync_recent_search_terms",
]
