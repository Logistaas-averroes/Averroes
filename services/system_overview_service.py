"""PR-ADS-162C — the System page: the state of the platform in plain facts.

One read for an operator who is not an engineer:

1. **Certification** — the latest daily platform certification verdict
   (``platform_certification_runs``, PR-ADS-161J), as recorded. No row, or a
   row older than :data:`CERTIFICATION_MAX_AGE_HOURS`, is *not checked* —
   never a pass. A row that claims a pass without every audit holding is not
   a pass either.
2. **Sources** — whether each source is current, by the verdict the platform
   already uses to publish from it. This module owns no freshness rule:

   * HubSpot contacts — ``cohort.read_freshness`` (the canonical assessor);
   * HubSpot deals — ``canonical_revenue_service.ledger_freshness`` (the
     coverage gate and the 36h staleness limit every closed-won figure obeys);
   * Google Ads spend — ``analyze_coverage`` over the durable coverage ledger
     (the function every revenue page's spend coverage is judged by), asked
     for the last :data:`SPEND_WINDOW_DAYS` complete account days.

   Every source carries one ``status``; only ``current`` is healthy.
3. **CRM data to fix** — the block the certification stored, passed through.

Read-only: SELECTs on the local database. No HubSpot or Google Ads call.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

#: The certification runs daily; a verdict older than this is not "today's".
CERTIFICATION_MAX_AGE_HOURS = 26
#: Spend coverage is asked for this many complete account days, ending
#: yesterday — the span one daily spend sync re-fetches.
SPEND_WINDOW_DAYS = 7

STATE_CERTIFIED = "certified"
STATE_VIOLATION = "violation"
STATE_NOT_CERTIFIED = "not_certified"
STATE_NOT_CHECKED = "not_checked"
STATE_UNAVAILABLE = "unavailable"

_STATE_BY_EXIT = {0: STATE_CERTIFIED, 1: STATE_VIOLATION, 2: STATE_NOT_CERTIFIED}
_HOLDS = "holds"

#: A source's one status. Only ``current`` is healthy.
SOURCE_CURRENT = "current"          # the platform's own verdict says current
SOURCE_LATE = "late"                # proven, but older than its limit
SOURCE_NOT_PROVEN = "not_proven"    # readable, but currency is not proven
SOURCE_UNAVAILABLE = "unavailable"  # the verdict could not be read


def _utc(value):
    if value is None:
        return None
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _age_hours(instant, now) -> float | None:
    instant = _utc(instant)
    if instant is None:
        return None
    return round((now - instant).total_seconds() / 3600.0, 1)


def _iso(value):
    value = _utc(value)
    return value.isoformat() if value else None


# ── certification ─────────────────────────────────────────────────────────

def read_latest_certification() -> dict:
    """``{available, row}`` — the newest recorded verdict, or row None."""
    from db.connection import get_conn  # noqa: PLC0415
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "row": None}
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT started_at, finished_at, exit_code, verdict, summary "
                    "FROM platform_certification_runs "
                    "ORDER BY started_at DESC, id DESC LIMIT 1")
                r = cur.fetchone()
        if r is None:
            return {"available": True, "row": None}
        return {"available": True, "row": {
            "started_at": r[0], "finished_at": r[1], "exit_code": r[2],
            "verdict": r[3], "summary": r[4] or {}}}
    except Exception as exc:  # noqa: BLE001
        log.warning("[system_overview] certification unreadable: %s", exc)
        return {"available": False, "row": None}


def certification_block(read: dict, now: datetime) -> dict:
    """Pure: the certification card. Fail closed on anything unexpected."""
    base = {"state": STATE_UNAVAILABLE, "verdict": None, "checked_at": None,
            "age_hours": None, "audits": [], "crm_data_to_fix": None,
            "could_not_run": False,
            "max_age_hours": CERTIFICATION_MAX_AGE_HOURS}
    if not read.get("available"):
        return base
    row = read.get("row")
    if not row:
        return {**base, "state": STATE_NOT_CHECKED}
    age = _age_hours(row.get("finished_at"), now)
    summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
    audits = [{"key": a.get("key"), "outcome": a.get("outcome")}
              for a in summary.get("audits") or [] if isinstance(a, dict)]
    out = {**base, "verdict": row.get("verdict"),
           "checked_at": _iso(row.get("finished_at")), "age_hours": age,
           "audits": audits,
           "crm_data_to_fix": summary.get("crm_data_to_fix"),
           # The run itself failed (no audit ran): nothing was checked.
           "could_not_run": bool(summary.get("error"))}
    if age is None or age > CERTIFICATION_MAX_AGE_HOURS or age < 0:
        # The recorded verdict is kept for context, but it is not today's.
        return {**out, "state": STATE_NOT_CHECKED}
    state = _STATE_BY_EXIT.get(row.get("exit_code"), STATE_NOT_CERTIFIED)
    if state == STATE_CERTIFIED and not (
            audits and all(a["outcome"] == _HOLDS for a in audits)):
        # A pass is every audit holding. A row that says 0 without that
        # evidence beside it is not one this page will repeat.
        state = STATE_NOT_CERTIFIED
    return {**out, "state": state}


# ── sources ───────────────────────────────────────────────────────────────

def _contacts_source(now: datetime) -> dict:
    from services import marketing_outcome_cohort_service as cohort  # noqa: PLC0415
    verdict = cohort.read_freshness(now)
    fresh, reason = verdict.get("fresh"), verdict.get("reason")
    if fresh is True:
        status = SOURCE_CURRENT
    elif fresh is None:
        status = SOURCE_UNAVAILABLE
    elif reason == "source_stale":
        status = SOURCE_LATE
    else:
        status = SOURCE_NOT_PROVEN
    return {"key": "hubspot_contacts", "label": "HubSpot contacts",
            "status": status,
            # The canonical assessor's verdict, unmodified.
            "fresh": fresh, "reason": reason,
            "last_success_at": _iso(verdict.get("last_successful_incremental_at")),
            "age_hours": verdict.get("age_hours"),
            "limit_hours": verdict.get("max_age_hours")}


def _deals_source(now: datetime) -> dict:
    from services import canonical_revenue_service as reader  # noqa: PLC0415
    v = reader.ledger_freshness(now)
    codes = list(v.get("violation_codes") or [])
    if not v.get("sync_state_available"):
        status = SOURCE_UNAVAILABLE
    elif v.get("coverage_proven") is not True:
        status = SOURCE_NOT_PROVEN
    elif v.get("stale") is False:
        status = SOURCE_CURRENT
    else:                       # True, or anything that is not a proven False
        status = SOURCE_LATE
    return {"key": "hubspot_deals", "label": "HubSpot deals",
            "status": status,
            # NULL unless coverage is proven: never a failed attempt's time.
            "last_success_at": _iso(v.get("latest_successful_incremental_at")),
            "age_hours": v.get("age_hours"),
            "limit_hours": v.get("max_staleness_hours"),
            "reasons": codes,
            "history_copied": v.get("bootstrap_complete")}


def _spend_source(now: datetime) -> dict:
    from analysis.account_time import account_today  # noqa: PLC0415
    from db import revenue_repository as repo  # noqa: PLC0415
    from services.google_ads_spend_service import (  # noqa: PLC0415
        analyze_coverage, configured_customer_id,
    )
    end = account_today(now) - timedelta(days=1)
    start = end - timedelta(days=SPEND_WINDOW_DAYS - 1)
    out = {"key": "google_ads_spend", "label": "Google Ads spend",
           "status": SOURCE_UNAVAILABLE,
           "window_start": start.isoformat(), "window_end": end.isoformat(),
           "complete_through": None, "missing_ranges": [],
           "unresolved_failed_chunks": None,
           "last_success_at": None, "age_hours": None}
    customer_id = configured_customer_id()
    coverage = repo.fetch_spend_coverage(start, end, customer_id)
    if not coverage.get("available"):
        return out
    verdict = analyze_coverage(start, end, coverage.get("chunks") or [])
    recency = repo.fetch_spend_coverage_recency(customer_id)
    if recency.get("available"):
        out["last_success_at"] = _iso(recency.get("updated_at"))
        out["age_hours"] = _age_hours(recency.get("updated_at"), now)
    complete = verdict.get("complete") is True
    return {**out,
            "status": SOURCE_CURRENT if complete else SOURCE_LATE,
            "complete_through": end.isoformat() if complete else None,
            "missing_ranges": list(verdict.get("missing_ranges") or []),
            "unresolved_failed_chunks": len(verdict.get("failed_chunks") or [])}


_SOURCES = (("hubspot_contacts", "HubSpot contacts", _contacts_source),
            ("hubspot_deals", "HubSpot deals", _deals_source),
            ("google_ads_spend", "Google Ads spend", _spend_source))


def build_system_overview(now: datetime | None = None) -> dict:
    """The ``/api/system/overview`` payload. Never raises."""
    now = now or datetime.now(tz=timezone.utc)
    sources = []
    for key, label, name in ((k, l, f.__name__) for k, l, f in _SOURCES):
        try:
            sources.append(globals()[name](now))
        except Exception as exc:  # noqa: BLE001
            log.warning("[system_overview] %s failed: %s", name, exc)
            sources.append({"key": key, "label": label,
                            "status": SOURCE_UNAVAILABLE})
    return {"generated_at": now.isoformat(),
            "certification": certification_block(read_latest_certification(), now),
            "sources": sources,
            "external_calls_performed": False}
