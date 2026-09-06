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

# PR-ADS-156-F1 §7: THE account-local date helper, the same object Campaign and
# Keyword Evidence resolve their window boundaries with. Imported directly so
# there is no second implementation to drift, and no code path in this module
# that can decide a different day is "today".
from analysis.account_time import account_today
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
#:
#: PR-ADS-156-F1 §5 — the last four are canonical IDENTITY. A non-empty string
#: in the `search_term` column was never proof that a row describes a knowable
#: event: without the account, the campaign and the ad group, "shipping to
#: france" is a phrase, not an observation anyone can act on or reconcile. The
#: natural key is built from these fields, so a row missing one is also a row
#: that cannot be upserted deterministically.
REJECT_BLANK_SEARCH_TERM = "blank_search_term"
REJECT_UNPARSEABLE_DATE = "unparseable_source_date"
REJECT_MISSING_CUSTOMER_ID = "missing_customer_identity"
REJECT_MISSING_CAMPAIGN_ID = "missing_campaign_identity"
REJECT_MISSING_AD_GROUP = "missing_ad_group_identity"
REJECT_MISSING_PROVENANCE = "missing_canonical_provenance"

#: The one provenance label a NEW canonical row may carry. Rows stamped
#: anything else came from somewhere that is not the canonical Google Ads API
#: path, and this service is the canonical path.
CANONICAL_PROVENANCE = SEARCH_TERMS_SOURCE

_DATE_KEYS = ("source_date", "date")


def _account_today(now: datetime | None = None) -> date:
    """Today in the ACCOUNT's calendar, which is the calendar Google Ads reports
    against.

    PR-ADS-156-F1 §7 — this is a thin alias for the shared
    :func:`analysis.account_time.account_today`, the helper Campaign and Keyword
    Evidence already use. It previously resolved a canonical window and, on ANY
    exception, quietly returned the UTC date instead. Between 23:00 and 00:00
    UTC in British Summer Time those are different days, so a transient failure
    could shift the requested interval by one date and record the wrong day as
    covered — silently, because the fallback logged at DEBUG. There is no
    fallback now: keyword and search-term intervals resolve through one function
    or not at all.
    """
    return account_today(now)


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


def _text(row: dict, *keys) -> str:
    """First non-blank value among ``keys``, trimmed. '' when none is present."""
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def classify_rows(rows: list | None) -> tuple[list, dict]:
    """Split fetched rows into what can be stored durably as CANONICAL evidence
    and what cannot.

    Applies the writer's own rejection rules ahead of the write, because
    ``write_search_terms`` returns a single integer: without this the service
    could report "wrote 12 of 12" while the connector had handed it 15 rows and
    three had been dropped inside the writer. A count that describes only the
    rows that survived is not a measurement of the pull.

    PR-ADS-156-F1 §5 — and it checks IDENTITY, not just presence of a string.
    A search term with no account, no campaign and no ad group is a phrase
    someone typed, not an observation: it cannot be attributed, reconciled
    against spend, or upserted deterministically, because the natural key is
    built from those same fields. Rejections are counted by reason and reported
    in ``rejected_count``; nothing is dropped quietly.
    """
    prepared, rejected = [], {}

    def reject(reason):
        rejected[reason] = rejected.get(reason, 0) + 1

    for row in rows or []:
        if not _text(row, "search_term", "term"):
            reject(REJECT_BLANK_SEARCH_TERM)
            continue

        source_date = None
        for key in _DATE_KEYS:
            source_date = _parse_source_date(row.get(key))
            if source_date is not None:
                break
        if source_date is None:
            reject(REJECT_UNPARSEABLE_DATE)
            continue

        if not _text(row, "customer_id"):
            reject(REJECT_MISSING_CUSTOMER_ID)
            continue
        if not _text(row, "campaign_id"):
            reject(REJECT_MISSING_CAMPAIGN_ID)
            continue
        # Ad-group identity from the fields the CURRENT schema stores. The
        # `search_terms` table keys on the ad-group NAME (there is no
        # `ad_group_id` column), so the name is what has to be there; the
        # connector's id is accepted as equivalent proof when it is present.
        if not _text(row, "ad_group", "ad_group_name", "ad_group_id"):
            reject(REJECT_MISSING_AD_GROUP)
            continue
        if _text(row, "source", "source_system") != CANONICAL_PROVENANCE:
            reject(REJECT_MISSING_PROVENANCE)
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


#: PR-ADS-156-F2 §2. The batch row is where coverage and verified-empty proof
#: LIVE. If the final update does not land, the pull may well have happened and
#: the rows may well be stored, but nothing durable says so — and every reader
#: downstream (freshness, the audit, `evidence_status`) works from the batch, not
#: from the caller's memory of the return value.
BATCH_FINALIZATION_FAILED = "batch_finalization_failed"

_FINALIZATION_ERROR = (
    "rows may be stored, but the sync batch could not be finalized: coverage "
    "and verified-empty proof were NOT durably recorded, so this interval is "
    "not certified"
)


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
        # PR-ADS-156-F2 §2 — whether the batch row itself was finalized, and
        # whether rows nevertheless reached the table. The two are separate
        # facts: an operator needs to know that data may exist even though this
        # run cannot certify it.
        "batch_finalized": False,
        "rows_possibly_written": 0,
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
        # NOT verified_empty, and the counters stay NULL rather than zero: a
        # pull that failed measured nothing, and a recorded zero would read
        # downstream as "we looked and there was nothing".
        w.finish_sync_batch(batch_id=batch_id, status="failed", row_count=0,
                            error_message=f"search-term pull failed: {exc}"[:1000],
                            verified_empty=False)
        return _result(batch_id=batch_id, error=str(exc), **span)

    fetched = len(rows or [])
    prepared_rows, rejected_reasons = classify_rows(rows)
    prepared = len(prepared_rows)
    rejected = sum(rejected_reasons.values())

    if fetched == 0:
        # Verified empty: asked, answered, nothing there. The watermark advances
        # because the interval is now proven — that is the difference between
        # this and a failure, and it is the only reason the flag exists.
        finalized = w.finish_sync_batch(
            batch_id=batch_id, status="success", row_count=0,
            last_source_date=date_to,
            # PR-ADS-156-F1 §2: recorded DURABLY, not left to be inferred later
            # from `success AND row_count = 0`. Historical batches share that
            # shape without sharing the meaning.
            verified_empty=True, fetched_count=0,
            prepared_count=0, rejected_count=0)
        if not finalized:
            # PR-ADS-156-F2 §2: verified-empty that was not written down is not
            # verified anything. The claim only means something because it is
            # durable; returning ok=True here would report an interval as proven
            # on the strength of a value that exists nowhere but this variable.
            logger.error("search-term sync (%s → %s): verified-empty result "
                         "could not be finalized", date_from, date_to)
            return _result(batch_id=batch_id, verified_empty=False,
                           error=f"{BATCH_FINALIZATION_FAILED}: the interval "
                                 "returned no rows, but that proof was not "
                                 "durably recorded",
                           **({"rows": []} if include_rows else {}), **span)
        logger.info("search-term sync (%s → %s): verified empty (0 rows)",
                    date_from, date_to)
        return _result(ok=True, batch_id=batch_id, verified_empty=True,
                       latest_source_date=None, batch_finalized=True,
                       **({"rows": []} if include_rows else {}), **span)

    # PR-ADS-156-F4: the REQUESTED interval is passed explicitly, because it is
    # what this run is about to certify. The writer reconciles residual
    # account-less twins across exactly that span — including identities Google
    # Ads did not return this time, which per-row supersession can never reach.
    # Deriving the bounds from the returned rows instead would shrink the swept
    # interval by precisely the dates whose identities went missing.
    written = w.write_search_terms(run_id, prepared_rows, sync_batch_id=batch_id,
                                   interval_start=date_from, interval_end=date_to)
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
    finalized = w.finish_sync_batch(
        batch_id=batch_id,
        status="success" if ok else "failed",
        row_count=written,
        # A failed sync must never advance the proven-coverage watermark: the
        # interval was attempted, not covered.
        last_source_date=(latest or date_to) if ok else None,
        error_message=None if ok else error[:1000],
        # A pull that returned rows is never verified-empty, whatever happened
        # to them afterwards. The counters are recorded either way — they are
        # what makes "wrote 0" distinguishable from "there was nothing".
        verified_empty=False, fetched_count=fetched,
        prepared_count=prepared, rejected_count=rejected)

    if ok and not finalized:
        # PR-ADS-156-F2 §2 — the rows very probably reached the table; the
        # CERTIFICATE did not. Reporting success here is the subtlest false
        # green of all, because the data would be fine and only the proof of it
        # missing, so nothing else would ever notice. Both facts are returned:
        # `written` stays 0 (this run certifies nothing) while
        # `rows_possibly_written` says what may be there.
        logger.error("search-term sync (%s → %s): wrote %d row(s) but the batch "
                     "could not be finalized — %s",
                     date_from, date_to, written, _FINALIZATION_ERROR)
        return _result(batch_id=batch_id, fetched=fetched, prepared=prepared,
                       written=0, rows_possibly_written=written,
                       rejected=rejected, rejected_reasons=rejected_reasons,
                       skipped=rejected, verified_empty=False,
                       error=f"{BATCH_FINALIZATION_FAILED}: {_FINALIZATION_ERROR}",
                       latest_source_date=None,
                       **({"rows": prepared_rows} if include_rows else {}), **span)

    if ok:
        logger.info("search-term sync (%s → %s): wrote %d row(s), latest %s",
                    date_from, date_to, written, latest)
    else:
        logger.error("search-term sync (%s → %s): %s", date_from, date_to, error)

    return _result(ok=ok, batch_id=batch_id, fetched=fetched, prepared=prepared,
                   written=written, rejected=rejected,
                   rejected_reasons=rejected_reasons, skipped=rejected,
                   verified_empty=False, error=error,
                   batch_finalized=bool(finalized),
                   rows_possibly_written=written,
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
    "DEFAULT_LOOKBACK_DAYS", "BATCH_FINALIZATION_FAILED", "CANONICAL_PROVENANCE",
    "REJECT_BLANK_SEARCH_TERM", "REJECT_UNPARSEABLE_DATE",
    "REJECT_MISSING_CUSTOMER_ID", "REJECT_MISSING_CAMPAIGN_ID",
    "REJECT_MISSING_AD_GROUP", "REJECT_MISSING_PROVENANCE",
    "classify_rows", "sync_search_terms", "sync_recent_search_terms",
]
