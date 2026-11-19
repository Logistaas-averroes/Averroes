"""
services/hubspot_name_backfill_service.py
PR-ADS-166A — load names from HubSpot for records the platform already holds.

Two bounded, read-only steps of the daily incremental sync:

``run_contact_name_backfill``
    The contact funnel sync is modified-since, so it brings ``firstname`` /
    ``lastname`` only for contacts that change. The ~21,700 contacts already
    stored would read "name not loaded" until somebody edited them. This reads
    their names by id, newest contact first, a capped number per run.

``run_company_name_sync``
    ``hubspot_deal_company_association`` holds the company ids customers rest
    on, and no names. This reads the ``name`` of those companies into
    ``hubspot_company_name``.

Truth rules, the same for both
------------------------------
* **Read-only against HubSpot.** One batch read per 100 ids; nothing written
  back. Local writes only.
* **Answered is not the same as named.** An id HubSpot returned is stamped as
  read, whatever name came back — including none. An id HubSpot did NOT return
  (deleted, merged, invisible) is not "no name": no name is written, it is
  stamped as asked-and-not-returned, counted as ``not_returned``, and asked
  again only after ``not_returned_retry_days``. Removal of contacts belongs
  to PR-ADS-163C, not here.
* **A refusal stops the run.** A 4xx other than 429 (a missing scope, a bad
  token) will be refused for every batch, so no further batch is asked and
  the HTTP status is in the step's error text.
* **A failed batch advances nothing.** Its ids are neither stamped nor
  written; they are read again on the next run.
* **An empty answer never blanks a stored name** (COALESCE in the writers).
* **Status is one of three.** ``success`` — every requested batch was
  answered and written (zero batches, because nothing was due, is a success).
  ``partial`` — some batches answered and some did not, or an answered batch
  could not be written. ``failed`` — the queue could not be read, or no batch
  at all was answered. Neither non-green outcome is ever reported as
  ``success``, and ``partial`` is never folded into ``failed``.
* **Memory.** Ids are selected in SQL with ``LIMIT``; one batch of 100 is held
  at a time.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

#: Fallbacks when config/thresholds.yaml cannot be read — the config's values.
DEFAULT_MAX_CONTACTS_PER_RUN = 3000
DEFAULT_MAX_COMPANIES_PER_RUN = 1000
DEFAULT_COMPANY_REREAD_DAYS = 7.0
DEFAULT_NOT_RETURNED_RETRY_DAYS = 7.0

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "thresholds.yaml"


def load_config() -> dict:
    """``name_backfill`` from config/thresholds.yaml; a missing, non-numeric
    or out-of-range value falls back to its default."""
    raw: dict = {}
    try:
        import yaml  # noqa: PLC0415

        with _CONFIG_PATH.open(encoding="utf-8") as fh:
            raw = (yaml.safe_load(fh) or {}).get("name_backfill") or {}
    except Exception as exc:  # noqa: BLE001
        log.debug("name_backfill config unavailable, using defaults: %s", exc)

    def _number(key, default, *, low, high, cast):
        value = raw.get(key) if isinstance(raw, dict) else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        if not low <= value <= high:
            return default
        return cast(value)

    return {
        "max_contacts_per_run": _number(
            "max_contacts_per_run", DEFAULT_MAX_CONTACTS_PER_RUN,
            low=0, high=50_000, cast=int),
        "max_companies_per_run": _number(
            "max_companies_per_run", DEFAULT_MAX_COMPANIES_PER_RUN,
            low=0, high=50_000, cast=int),
        "company_reread_days": _number(
            "company_reread_days", DEFAULT_COMPANY_REREAD_DAYS,
            low=0.5, high=365, cast=float),
        "not_returned_retry_days": _number(
            "not_returned_retry_days", DEFAULT_NOT_RETURNED_RETRY_DAYS,
            low=0.5, high=365, cast=float),
    }


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _chunks(ids: list, size: int):
    for i in range(0, len(ids), size):
        yield ids[i:i + size]


def _status(requested: int, answered: int, write_failures: int) -> str:
    if requested == 0:
        return STATUS_SUCCESS            # nothing was due: nothing can have failed
    if answered == 0:
        return STATUS_FAILED             # nothing at all was learned
    if answered < requested or write_failures:
        return STATUS_PARTIAL
    return STATUS_SUCCESS


def _failed(error: str, **extra) -> dict:
    # Unknown, never zero: a step that could not start read nothing it can
    # vouch for.
    return {"status": STATUS_FAILED, "error": error[:500],
            "hubspot_writes_performed": False, "selected": None,
            "batches_requested": None, "batches_answered": None,
            "batches_failed": None, "returned": None, "not_returned": None,
            "written": None, **extra}


def _run(*, ids: list, read_batch, write_batch, batch_size: int, what: str,
         sleep) -> dict:
    """Read ``ids`` in batches and write each ANSWERED batch before the next.

    ``write_batch(found, missing)`` records the answer: ``found`` for the ids
    HubSpot returned, ``missing`` for the asked ids it did not.
    """
    from connectors.hubspot_pull import HubSpotNameReadRefused  # noqa: PLC0415

    requested = answered = failed = write_failures = 0
    returned = not_returned = written = with_name = 0
    errors: list = []
    refused_status = None
    for index, batch in enumerate(_chunks(ids, batch_size)):
        if index:
            sleep()
        requested += 1
        try:
            found = read_batch(batch)
        except HubSpotNameReadRefused as exc:
            # Refused, not unlucky: the next batch would be refused too.
            failed += 1
            refused_status = exc.status
            errors.append(str(exc)[:200])
            log.warning("[name_backfill] %s batch %d refused: %s — no further "
                        "batch is asked this run", what, index + 1, exc)
            break
        except Exception as exc:  # noqa: BLE001 — a failed read, never "empty"
            failed += 1
            errors.append(str(exc)[:200])
            log.warning("[name_backfill] %s batch %d failed: %s", what,
                        index + 1, exc)
            continue
        result = write_batch(found, [i for i in batch if i not in found])
        if not result.get("ok"):
            # The read answered but nothing durable says so: these ids stay
            # unread and are tried again on the next run.
            write_failures += 1
            errors.append(f"write failed: {result.get('error')}"[:200])
            continue
        answered += 1
        returned += len(found)
        not_returned += len(batch) - len(found)
        written += int(result.get("persisted") or 0)
        with_name += sum(1 for v in found.values() if _has_name(v))
    return {
        "status": _status(requested, answered, write_failures),
        "error": "; ".join(dict.fromkeys(errors))[:500] or None,
        "hubspot_writes_performed": False,
        "selected": len(ids),
        "batches_requested": requested, "batches_answered": answered,
        "batches_failed": failed, "write_failures": write_failures,
        "refused_http_status": refused_status,
        "returned": returned, "not_returned": not_returned,
        "with_name": with_name, "without_name": returned - with_name,
        "written": written,
    }


def _has_name(value) -> bool:
    if isinstance(value, dict):
        return bool(value.get("first_name") or value.get("last_name"))
    return bool(value)


def _pacing():
    from connectors import hubspot_pull as hp  # noqa: PLC0415
    time.sleep(max(0.0, float(hp.NAME_READ_PACING_SECONDS)))


def run_contact_name_backfill(*, now: datetime | None = None, client=None,
                              limit: int | None = None,
                              batch_size: int | None = None) -> dict:
    """Read names for stored contacts never read. Never raises."""
    try:
        from connectors import hubspot_pull as hp  # noqa: PLC0415
        from db import hubspot_name_repository as repo  # noqa: PLC0415
        from db import writers as db_writers  # noqa: PLC0415

        now = now or _utcnow()
        size = min(int(batch_size or hp.NAME_BATCH_LIMIT), hp.NAME_BATCH_LIMIT)

        config = load_config()
        cap = config["max_contacts_per_run"] if limit is None else int(limit)
        queue = repo.fetch_contacts_due_for_names(
            cap, reask_before=now - timedelta(
                days=config["not_returned_retry_days"]))
        if not queue.get("available"):
            return _failed("the name queue could not be read",
                           progress=contact_name_progress())
        ids = queue["contact_ids"]

        def read_batch(batch):
            return hp.read_contact_names(batch, client=client)

        def write_batch(found, missing):
            result = db_writers.apply_contact_names(
                [{"contact_id": cid, **names} for cid, names in found.items()],
                read_at=now)
            if not result.get("ok"):
                return result
            marked = db_writers.mark_contact_names_not_returned(
                missing, asked_at=now)
            return result if marked.get("ok") else marked

        out = _run(ids=ids, read_batch=read_batch, write_batch=write_batch,
                   batch_size=size, what="contact", sleep=_pacing)
        out["max_per_run"] = cap
        out["progress"] = contact_name_progress()
        return out
    except Exception as exc:  # noqa: BLE001
        log.error("[name_backfill] contact names failed: %s", exc, exc_info=True)
        return _failed(str(exc), progress=None)


def run_company_name_sync(*, now: datetime | None = None, client=None,
                          limit: int | None = None,
                          batch_size: int | None = None) -> dict:
    """Read names for the companies on deal→company associations. Never raises."""
    try:
        from connectors import hubspot_pull as hp  # noqa: PLC0415
        from db import hubspot_name_repository as repo  # noqa: PLC0415
        from db import writers as db_writers  # noqa: PLC0415

        now = now or _utcnow()
        config = load_config()
        cap = config["max_companies_per_run"] if limit is None else int(limit)
        size = min(int(batch_size or hp.NAME_BATCH_LIMIT), hp.NAME_BATCH_LIMIT)
        reread_before = now - timedelta(days=config["company_reread_days"])

        queue = repo.fetch_company_ids_needing_names(
            cap, reread_before=reread_before)
        if not queue.get("available"):
            return _failed("the company queue could not be read")
        ids = queue["company_ids"]

        def read_batch(batch):
            return hp.read_company_names(batch, client=client)

        def write_batch(found, missing):
            result = db_writers.upsert_company_names(
                [{"company_id": cid, "name": name}
                 for cid, name in found.items()], read_at=now)
            if not result.get("ok"):
                return result
            marked = db_writers.mark_company_names_not_returned(
                missing, asked_at=now)
            return result if marked.get("ok") else marked

        out = _run(ids=ids, read_batch=read_batch, write_batch=write_batch,
                   batch_size=size, what="company", sleep=_pacing)
        out["max_per_run"] = cap
        return out
    except Exception as exc:  # noqa: BLE001
        log.error("[name_backfill] company names failed: %s", exc, exc_info=True)
        return _failed(str(exc))


def contact_name_progress() -> dict:
    """``{available, read, not_returned, never_asked, total, complete}``.

    ``complete`` is True only when the counts are known and NO stored contact
    is still unasked. A contact HubSpot was asked for and did not return is
    not "still loading" — it is counted on its own. Unknown counts are
    ``None``, never zero, and never "complete".
    """
    keys = ("read", "not_returned", "never_asked", "total")
    try:
        from db import hubspot_name_repository as repo  # noqa: PLC0415
        p = repo.fetch_contact_name_progress()
    except Exception as exc:  # noqa: BLE001
        log.error("[name_backfill] progress unreadable: %s", exc)
        p = {"available": False}
    known = bool(p.get("available")) and all(p.get(k) is not None for k in keys)
    out = {"available": known}
    out.update({k: (p.get(k) if known else None) for k in keys})
    out["complete"] = (p["never_asked"] == 0) if known else None
    return out
