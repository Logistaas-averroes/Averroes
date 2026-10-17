"""
services/deal_company_association_sync_service.py

PR-ADS-161D2 — keep the deal→company association evidence current for every
canonical won deal.

Pipeline::

    canonical won-deal population (local ledger, read-only)
      → HubSpot deal→company associations, one deal at a time (read-only)
      → per deal: a success replaces its association set; a failure records the
        failed attempt and preserves the last proven set
      → run outcome recorded as success / partial / failed

Why the WHOLE won population every run
--------------------------------------
Associating a company to a deal is not guaranteed to move the deal's
``hs_lastmodifieddate``, so a watermark-driven scan could miss exactly the change
this dataset exists to capture. The won population is small and bounded, and
re-reading all of it is what lets a run PROVE it covered every deal. The legacy
GCLID deal subset is never the population — it is a proxy for paid deals, not
the set of won deals.

This service holds no customer-count logic and no UI logic. It records evidence;
``analysis.closed_won_truth`` decides what that evidence proves.

Governance
----------
Reads HubSpot read-only (GET only). Writes ONLY local PostgreSQL, and nothing at
all in ``dry_run``. No Google Ads call of any kind.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

#: Upper bound on HubSpot lookups in one run. Reaching it ends the run as
#: PARTIAL — disclosed, never a truncated success.
DEFAULT_MAX_LOOKUPS = 5000

STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _company_shape(companies) -> str:
    """How many DISTINCT companies a successful lookup saw — zero, one or
    several. A count of evidence shapes, not a customer count."""
    distinct = {str(c.get("company_id")) for c in companies or []}
    if not distinct:
        return "zero_companies"
    return "one_company" if len(distinct) == 1 else "multiple_companies"


def sync_deal_company_associations(*, batch_id=None, mode: str = "incremental",
                                   deal_ids=None,
                                   max_lookups: int = DEFAULT_MAX_LOOKUPS,
                                   stop_on_failure: bool = False,
                                   dry_run: bool = False,
                                   record_run: bool = True,
                                   now_fn=_now) -> dict:
    """Observe the company associations of the won deals.

    Args:
        deal_ids: the deals to observe. ``None`` = the full current won
            population read from the canonical ledger.
        stop_on_failure: stop at the first lookup or persistence failure (the
            historical backfill does, so its checkpoint sits before the deal
            that failed).
        dry_run: read HubSpot, write NOTHING — not even the run record.
        record_run: record the run outcome in the sync state.

    Returns counts — ``population``, ``attempted``, ``succeeded``, ``failed``,
    ``applied``, ``paths_written``, ``skipped_stale``, ``write_failures``,
    ``remaining`` — plus ``status``, ``complete``, ``shapes``,
    ``failure_reasons``, ``failed_deal_ids`` and ``error``.
    ``complete`` is True only when every deal in the population was observed
    successfully AND (unless dry-run) persisted. It is recorded as a proven
    complete SCAN only when the population was the full won population
    (``deal_ids is None``).
    """
    import connectors.hubspot_pull as hubspot  # noqa: PLC0415
    import db.deal_company_association_repository as repo  # noqa: PLC0415

    # The run's start orders every run-state field, so it comes from the
    # DATABASE clock — a host running ahead must not stamp a start that later
    # runs can never supersede. A dry run records nothing and uses its own.
    if dry_run:
        started_at = now_fn()
    else:
        clock = repo.db_now()
        if not clock.get("available"):
            return {"status": STATUS_FAILED, "mode": mode, "dry_run": dry_run,
                    "full_population": deal_ids is None, "population": None,
                    "attempted": 0, "succeeded": 0, "failed": 0, "applied": 0,
                    "paths_written": 0, "skipped_stale": 0,
                    "write_failures": 0, "remaining": None, "complete": False,
                    "truncated": False, "stopped_on_failure": False,
                    "shapes": {}, "failure_reasons": {}, "failed_deal_ids": [],
                    "error": "database_clock_unavailable",
                    "hubspot_writes_performed": False,
                    "google_ads_calls_performed": False}
        started_at = clock["now"]
    # Only a run over the FULL current won population may prove a complete scan;
    # a resume pass over the remaining deals proves only its own subset.
    full_population = deal_ids is None
    result = {"status": STATUS_FAILED, "mode": mode, "dry_run": dry_run,
              "full_population": full_population,
              "started_at": started_at.isoformat(), "population": None,
              "attempted": 0, "succeeded": 0, "failed": 0, "applied": 0,
              "paths_written": 0, "skipped_stale": 0, "write_failures": 0,
              "remaining": None, "complete": False, "truncated": False,
              "stopped_on_failure": False, "shapes": {}, "failure_reasons": {},
              "failed_deal_ids": [], "error": None,
              "hubspot_writes_performed": False,
              "google_ads_calls_performed": False}

    if deal_ids is None:
        pop = repo.fetch_won_deal_population()
        if not pop.get("available"):
            result["error"] = (f"won_population_unreadable:"
                               f"{pop.get('error') or pop.get('reason')}")
            return _finish(result, repo, batch_id, started_at, dry_run,
                           record_run)
        deal_ids = [d["deal_id"] for d in pop.get("deals") or []]
    deal_ids = sorted({str(d) for d in deal_ids if d not in (None, "")})
    result["population"] = len(deal_ids)

    first_error = None
    for deal_id in deal_ids:
        if result["attempted"] >= max_lookups:
            # The cap ends the run. The remaining deals were not attempted, and
            # writing failures for them would invent evidence of failures that
            # never happened.
            result["truncated"] = True
            break
        # Observations are ordered by the DATABASE clock (see repo.db_now);
        # a dry run writes nothing and orders nothing, so it uses its own.
        if dry_run:
            observed_at = now_fn()
        else:
            clock = repo.db_now()
            if not clock.get("available"):
                result["write_failures"] += 1
                first_error = first_error or "database_clock_unavailable"
                if stop_on_failure:
                    result["stopped_on_failure"] = True
                    break
                continue
            observed_at = clock["now"]
        result["attempted"] += 1
        try:
            read = hubspot.fetch_deal_company_associations(deal_id)
            companies = read.get("companies") or []
            if read.get("complete") is not True:
                raise hubspot.DealCompanyAssociationLookupError(
                    "incomplete_read")
        except Exception as exc:  # noqa: BLE001 — every failure is a failure
            reason = getattr(exc, "reason", None) or type(exc).__name__
            result["failed"] += 1
            result["failure_reasons"][reason] = (
                result["failure_reasons"].get(reason, 0) + 1)
            if len(result["failed_deal_ids"]) < 50:
                result["failed_deal_ids"].append(deal_id)
            first_error = first_error or f"lookup_failed:{reason}"
            log.warning("[deal-company] lookup failed for %s: %s",
                        deal_id, reason)
            if not dry_run:
                write = repo.record_observation_failure(
                    deal_id, observation_started_at=observed_at,
                    reason=reason, batch_id=batch_id)
                if not write.get("available"):
                    result["write_failures"] += 1
                    first_error = first_error or "failure_record_write_failed"
            if stop_on_failure:
                result["stopped_on_failure"] = True
                break
            continue

        shape = _company_shape(companies)
        result["shapes"][shape] = result["shapes"].get(shape, 0) + 1
        if dry_run:
            result["succeeded"] += 1
            continue
        write = repo.record_observation_success(
            deal_id, companies, observation_started_at=observed_at,
            batch_id=batch_id)
        if not write.get("available"):
            # A lookup that succeeded but did not land is not a success.
            result["write_failures"] += 1
            first_error = first_error or (
                f"observation_write_failed:{write.get('error')}")
            log.error("[deal-company] write failed for %s: %s",
                      deal_id, write.get("error"))
            if stop_on_failure:
                result["stopped_on_failure"] = True
                break
            continue
        result["succeeded"] += 1
        if write.get("applied"):
            result["applied"] += 1
            result["paths_written"] += int(write.get("paths_written") or 0)
        else:
            # A newer observation already stands. Not a failure: the deal IS
            # covered, by evidence at least as recent as this read.
            result["skipped_stale"] += 1

    result["remaining"] = result["population"] - result["succeeded"]
    result["complete"] = (result["succeeded"] == result["population"]
                          and not result["failed"]
                          and not result["write_failures"])
    if result["complete"]:
        result["status"] = STATUS_SUCCESS
    elif result["succeeded"] == 0 and result["population"] > 0:
        result["status"] = STATUS_FAILED
    else:
        result["status"] = STATUS_PARTIAL
    errors = [e for e in (
        first_error,
        "lookup_cap_reached" if result["truncated"] else None) if e]
    result["error"] = "; ".join(errors) if errors else None
    return _finish(result, repo, batch_id, started_at, dry_run, record_run,
                   scanned_deal_ids=deal_ids)


def _finish(result, repo, batch_id, started_at, dry_run, record_run, *,
            scanned_deal_ids=None) -> dict:
    if dry_run or not record_run:
        return result
    state = repo.record_sync_run(
        mode=result["mode"], status=result["status"], started_at=started_at,
        batch_id=batch_id, population=result["population"] or 0,
        attempted=result["attempted"], succeeded=result["succeeded"],
        failed=result["failed"], write_failures=result["write_failures"],
        complete=bool(result["complete"] and result["full_population"]),
        full_population=result["full_population"],
        # The exact set this run observed — what a complete scan certifies.
        scanned_deal_ids=scanned_deal_ids if result["complete"] else None,
        error=result["error"])
    if not state.get("available"):
        # Coverage that could not be recorded cannot be claimed.
        result["status"] = STATUS_FAILED
        result["complete"] = False
        result["error"] = "; ".join(e for e in (
            result["error"], f"sync_state_write_failed:{state.get('error')}")
            if e)
    log.info("[deal-company] mode=%s status=%s population=%s attempted=%d "
             "succeeded=%d failed=%d write_failures=%d", result["mode"],
             result["status"], result["population"], result["attempted"],
             result["succeeded"], result["failed"], result["write_failures"])
    return result


def record_failed_run(error: str, *, mode: str = "incremental") -> dict:
    """Record a FULL-population run that failed before it could look at any
    deal — no batch, an exception in the step. Without this, the last recorded
    full run would still read ``success`` and an exact customer total would
    keep publishing over a pipeline that had stopped."""
    import db.deal_company_association_repository as repo  # noqa: PLC0415

    clock = repo.db_now()
    if not clock.get("available"):
        return {"available": False, "error": "database_clock_unavailable"}
    return repo.record_sync_run(mode=mode, status=STATUS_FAILED,
                                started_at=clock["now"], full_population=True,
                                error=str(error)[:1000])


__all__ = ["DEFAULT_MAX_LOOKUPS", "sync_deal_company_associations",
           "record_failed_run"]
