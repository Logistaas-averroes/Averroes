"""
services/canonical_coverage_repair_service.py

PR-ADS-154B §1 — repair canonical campaign-spend and FX coverage for a window,
resumably, and prove the result.

Why this exists
---------------
The scheduled run refreshes campaign spend and FX over a SEVEN-DAY rolling
window, because Google Ads restates recent spend. That is correct maintenance
behaviour and useless as a bootstrap: on a window like ``current_quarter`` or
``ytd``, seven proven days can never make the coverage ledger complete, so
``campaign_coverage_incomplete`` and ``fx_coverage_incomplete`` are permanent
and geo reconciliation can never be trusted no matter how many times the daily
sync succeeds.

Canonical geo got a dedicated resumable bootstrap in PR-ADS-153F
(``scripts/backfill_canonical_geo.py``). Campaign spend and FX did not. Their
only repair paths were two HTTP admin endpoints, which cannot be driven from a
Render shell as one resumable operation and — the part that matters — report
that they *ran*, not that coverage is now *complete*. A repair whose success
criterion is "no exception was raised" is how a window stays broken while every
signal says the job worked.

What this does NOT do
---------------------
It contains **no ingestion logic of its own**. Spend comes from
``services.google_ads_spend_service.run_google_ads_spend_backfill`` and FX from
``services.fx_service.ensure_fx_rates`` — the same functions the scheduler and
the admin endpoints call. A second copy of either would be a second set of rules
to keep in step, which is the defect class this programme removes. Windsor is
not involved in any path: campaign spend is read from the Google Ads API.

What "success" means here
-------------------------
Not "the backfill ran". Complete coverage, PROVEN by re-reading the durable
ledger afterwards:

  * the spend coverage ledger reports the requested range complete for THIS
    customer, with no unresolved failed chunks, and
  * every canonical spend date in the range has an FX rate.

Anything short of that is a non-zero exit: partial, failed chunks, an unreadable
ledger, or a final check that still shows gaps.

Read-only against Google Ads and the FX provider. Writes ONLY local canonical
tables. Never writes to Google Ads, HubSpot or Mailchimp.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from db import revenue_repository as repo
from services.google_ads_spend_service import (
    analyze_coverage,
    configured_customer_id,
    run_google_ads_spend_backfill,
)

log = logging.getLogger(__name__)

class CoverageRepairInputError(ValueError):
    """The caller asked for a range that is not repairable, e.g. an inverted one."""


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return value.isoformat()
    except Exception:  # noqa: BLE001
        return str(value)


def failed_spend_chunks(customer_id: str | None, start: date, end: date) -> list[dict]:
    """The ledger's own failed ranges intersecting [start, end].

    These are retried at their RECORDED boundaries, which is the only way to
    repair them: the ledger is keyed ``(customer_id, chunk_start, chunk_end)``,
    so a re-fetch under any other chunking writes a different row and leaves the
    failed one standing. Retrying "the window" with a monthly chunk size is
    exactly what does not fix a chunk that failed as a rolling 7-day range.
    """
    ledger = repo.fetch_spend_coverage(start, end, customer_id)
    if not ledger.get("available"):
        return []
    return [
        {"chunk_start": _iso(c.get("chunk_start")), "chunk_end": _iso(c.get("chunk_end"))}
        for c in ledger.get("chunks", [])
        if c.get("status") != "verified" and c.get("chunk_start") and c.get("chunk_end")
    ]


def already_verified_chunk_keys(customer_id: str | None, start: date, end: date,
                                chunk_months: int) -> list[str]:
    """The backfill chunk keys this range has already PROVEN, for ``resume``.

    ``run_google_ads_spend_backfill`` skips a chunk only when ``resume`` is set
    AND a ``load_completed`` callback names it. Without that callback ``resume``
    changes nothing and every chunk is re-fetched — so "repair only what is
    missing" quietly became "re-fetch the whole range, every run", which is both
    the wrong contract and a great deal of Google Ads quota.

    A chunk counts as completed only when the durable ledger shows every one of
    its days verified. Partial coverage re-fetches the whole chunk: the upsert is
    idempotent, so redoing a little proven work is the cheap side of this trade,
    while skipping an unproven day is the expensive one.

    The keys are built with the same iteration and the same ``f"{start}:{end}"``
    shape the backfill uses, because a key that does not match is a key that
    never skips anything.
    """
    from dateutil.relativedelta import relativedelta  # noqa: PLC0415

    ledger = repo.fetch_spend_coverage(start, end, customer_id)
    if not ledger.get("available"):
        return []

    chunks = ledger.get("chunks", [])
    keys: list[str] = []
    cursor = start
    step = relativedelta(months=max(1, chunk_months))
    while cursor <= end:
        chunk_to = min(cursor + step - relativedelta(days=1), end)
        if analyze_coverage(cursor, chunk_to, chunks).get("complete"):
            keys.append(f"{cursor.isoformat()}:{chunk_to.isoformat()}")
        cursor = chunk_to + relativedelta(days=1)
    return keys


def verify_spend_coverage(customer_id: str | None, start: date, end: date) -> dict:
    """Re-read the durable ledger and say whether [start, end] is proven covered."""
    ledger = repo.fetch_spend_coverage(start, end, customer_id)
    if not ledger.get("available"):
        return {"available": False, "complete": False, "missing_ranges": [],
                "failed_chunks": [], "superseded_failed_chunks": []}
    cov = analyze_coverage(start, end, ledger.get("chunks", []))
    return {
        "available": True,
        "complete": bool(cov.get("complete")),
        "missing_ranges": cov.get("missing_ranges") or [],
        "failed_chunks": cov.get("failed_chunks") or [],
        "superseded_failed_chunks": cov.get("superseded_failed_chunks") or [],
    }


def verify_fx_coverage(customer_id: str | None, start: date, end: date,
                       base_currency: str, quote_currency: str) -> dict:
    """Re-read FX coverage over this account's canonical spend dates.

    FX completeness is measured over the spend dates that EXIST, so it is only
    meaningful once spend coverage is complete: with half the range unfetched,
    the FX question is being asked about half the days. The caller checks both,
    and reports spend coverage first for that reason.
    """
    cov = repo.fetch_fx_coverage(start, end, base_currency, quote_currency,
                                 customer_id=customer_id)
    return {
        "available": bool(cov.get("available")),
        "complete": bool(cov.get("available")) and bool(cov.get("complete")),
        "spend_days": int(cov.get("spend_days") or 0),
        "covered_days": int(cov.get("covered_days") or 0),
        "missing_dates": [_iso(d) for d in (cov.get("missing_dates") or [])],
    }


def repair_canonical_spend_and_fx(
    *,
    date_from: date,
    date_to: date,
    resume: bool = True,
    chunk_months: int = 1,
    dry_run: bool = False,
    customer_id: str | None = None,
) -> dict:
    """Repair campaign-spend and FX coverage for [date_from, date_to], then prove it.

    Both boundaries are INCLUSIVE, matching the canonical window contract: every
    coverage query in this codebase filters ``spend_date >= start AND
    spend_date <= end``, and a repair that covered a different range from the one
    the gate measures would report success against a window nobody checks.

    ``resume`` (the default) repairs only what is missing or failed. ``resume=False``
    re-fetches the whole range — slower, more quota, and occasionally what you
    want after a Google Ads restatement.

    Returns a structured result; the caller decides the exit code from
    ``coverage.ok``. Read-only against Google Ads and the FX provider.
    """
    from services.fx_service import (  # noqa: PLC0415
        NATIVE_CURRENCY, REPORTING_CURRENCY, ensure_fx_rates,
    )

    if date_from > date_to:
        raise CoverageRepairInputError(
            f"date_from ({date_from}) must be on or before date_to ({date_to})")

    started_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    scope_customer_id = customer_id or configured_customer_id()
    errors: list[str] = []

    # ── 1. Retry the ledger's own failed ranges, at their recorded boundaries ──
    # Before the general backfill, because these are the chunks a general
    # backfill provably cannot fix.
    #
    # Runs for EVERY non-dry-run repair, resume or restart. Gating it on
    # `resume` was wrong in the one direction that matters: `--restart` is what
    # an operator reaches for when a range looks wrong, and it re-fetches in
    # monthly chunks, which write different ledger keys and therefore leave the
    # original failed 7-day rows standing. The mode most likely to be used on a
    # damaged window was the mode that skipped the only step able to repair it.
    retried: list[dict] = []
    if not dry_run:
        for chunk in failed_spend_chunks(scope_customer_id, date_from, date_to):
            try:
                outcome = run_google_ads_spend_backfill(
                    date_from=chunk["chunk_start"], date_to=chunk["chunk_end"],
                    dry_run=False, chunk_months=chunk_months, resume=False)
                retried.append({**chunk, "status": outcome.get("status")})
                if outcome.get("errors"):
                    errors.extend(f"retry {chunk['chunk_start']}..{chunk['chunk_end']}: {e}"
                                  for e in outcome["errors"])
            except Exception as exc:  # noqa: BLE001
                retried.append({**chunk, "status": "failed"})
                errors.append(
                    f"retry {chunk['chunk_start']}..{chunk['chunk_end']}: {exc}")

    # ── 2. Backfill campaign spend across the whole requested range ───────────
    # On resume, chunks the ledger already proves complete are skipped — which
    # requires handing the backfill a `load_completed` callback, since `resume`
    # alone does nothing. Step 1 has already run, so a chunk containing a
    # just-repaired failure is re-read here and correctly seen as complete.
    completed_keys = (already_verified_chunk_keys(
        scope_customer_id, date_from, date_to, chunk_months) if resume else [])
    spend = run_google_ads_spend_backfill(
        date_from=date_from.isoformat(), date_to=date_to.isoformat(),
        dry_run=dry_run, chunk_months=chunk_months, resume=resume,
        load_completed=(lambda: completed_keys) if resume else None)
    errors.extend(spend.get("errors") or [])

    # ── 3. FX for the same range ──────────────────────────────────────────────
    # After spend, because `only_missing` skips dates already held and the set of
    # dates that matter is defined by the spend rows step 2 just wrote.
    if dry_run:
        fx = {"skipped": "dry_run", "rows_written": 0, "fetched": 0, "failed": []}
    else:
        fx = ensure_fx_rates(date_from, date_to,
                             base_currency=NATIVE_CURRENCY,
                             quote_currency=REPORTING_CURRENCY,
                             # Resuming fetches only the dates not already held;
                             # a deliberate restart re-fetches every one.
                             only_missing=resume)
        for f in fx.get("failed") or []:
            errors.append(f"fx {f.get('rate_date')}: {f.get('error')}")

    # ── 4. Prove it — re-read both ledgers ────────────────────────────────────
    # The whole point. Steps 1-3 report what they ATTEMPTED; this reports what is
    # now true, read back from the durable tables rather than inferred from the
    # absence of an exception.
    spend_coverage = verify_spend_coverage(scope_customer_id, date_from, date_to)
    fx_coverage = verify_fx_coverage(scope_customer_id, date_from, date_to,
                                     NATIVE_CURRENCY, REPORTING_CURRENCY)
    ok = bool(spend_coverage["complete"] and fx_coverage["complete"]) and not dry_run

    finished_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = {
        "status": "success" if ok else ("dry_run" if dry_run else "incomplete"),
        "dry_run": dry_run,
        "resume": resume,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "bounds": "inclusive_start_inclusive_end",
        "customer_id": scope_customer_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "spend_backfill": {
            "status": spend.get("status"),
            "chunks_verified": (spend.get("summary") or {}).get("chunks_verified"),
            "chunks_failed": (spend.get("summary") or {}).get("chunks_failed"),
            "rows_written": (spend.get("summary") or {}).get("rows_written"),
            "retried_failed_chunks": retried,
            # Published so a reader can see the resume actually resumed, rather
            # than inferring it from a low fetch count.
            "chunks_skipped_already_verified": len(completed_keys),
        },
        "fx": {
            "rows_written": fx.get("rows_written"),
            "rates_fetched": fx.get("fetched"),
            "failed": fx.get("failed") or [],
        },
        "coverage": {
            # `ok` is the exit-code input, and it is a claim about the RANGE, not
            # about this run's luck with exceptions.
            "ok": ok,
            "campaign_coverage_available": spend_coverage["available"],
            "campaign_coverage_complete": spend_coverage["complete"],
            "campaign_missing_ranges": spend_coverage["missing_ranges"],
            "campaign_failed_chunks": spend_coverage["failed_chunks"],
            "campaign_superseded_failed_chunks": spend_coverage["superseded_failed_chunks"],
            "fx_coverage_available": fx_coverage["available"],
            "fx_coverage_complete": fx_coverage["complete"],
            "fx_spend_days": fx_coverage["spend_days"],
            "fx_covered_days": fx_coverage["covered_days"],
            "fx_missing_dates": fx_coverage["missing_dates"],
        },
        "errors": errors,
    }
    log.info("[coverage-repair] %s..%s customer=%s ok=%s spend_complete=%s fx_complete=%s",
             date_from, date_to, scope_customer_id, ok,
             spend_coverage["complete"], fx_coverage["complete"])
    return result
