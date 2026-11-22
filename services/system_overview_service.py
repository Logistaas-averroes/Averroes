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

   * Meta Ads spend (PR-ADS-171A) — the same ``analyze_coverage`` over each
     Meta ad account's coverage ledger, for the last :data:`SPEND_WINDOW_DAYS`
     complete days of THAT account's zone; one line, every account listed.
     Before that it says whether Meta is set up at all: ``not_connected`` (no
     credentials — neutral, not a fault), ``misconfigured``,
     ``token_rejected`` or ``not_readable``.

   Every source carries one ``status``; only ``current`` is healthy.
3. **CRM data to fix** — the block the certification stored, passed through.
4. **Removed contacts** — the latest check for contacts HubSpot no longer has
   (``hubspot_contact_reconciliation_runs``, PR-ADS-163C), as recorded: its
   status and its own reason text, when it ran, how many contacts it checked,
   would remove and removed, the backlog, held contacts, search disagreements
   and pending strikes. Facts only; no verdict is derived from them here.

Read-only: SELECTs on the local database. No HubSpot, Google Ads or Meta call.
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
#: PR-ADS-169A — a run in progress. Not a pass, and not yesterday's verdict.
STATE_RUNNING = "running"

#: Row statuses written by services.platform_certification_service.
_ROW_RUNNING, _ROW_FINISHED, _ROW_INTERRUPTED = "running", "finished", "interrupted"
_INTERRUPTED_ERROR = "heartbeat stopped; presumed interrupted"

_STATE_BY_EXIT = {0: STATE_CERTIFIED, 1: STATE_VIOLATION, 2: STATE_NOT_CERTIFIED}
_HOLDS = "holds"

#: A source's one status. Only ``current`` is healthy.
SOURCE_CURRENT = "current"          # the platform's own verdict says current
SOURCE_LATE = "late"                # proven, but older than its limit
SOURCE_NOT_PROVEN = "not_proven"    # readable, but currency is not proven
SOURCE_UNAVAILABLE = "unavailable"  # the verdict could not be read
# PR-ADS-171A — statuses only the Meta Ads row (key `meta_ads_spend`) carries.
#: Meta is not set up: no credentials. Not a fault; nothing is claimed.
SOURCE_NOT_CONNECTED = "not_connected"
#: Meta is half set up (one credential, a bad account id, no API version).
SOURCE_MISCONFIGURED = "misconfigured"
#: Meta refused the access token on the latest sync.
SOURCE_TOKEN_REJECTED = "token_rejected"
#: Meta refuses to let an account be read (suspended, closed, permission
#: removed). Its stored history is kept.
SOURCE_NOT_READABLE = "not_readable"
#: A per-account status only: stored, declared retired by the owner (and the
#: ledger bears the declaration out). Its history is kept.
META_ACCOUNT_HISTORY_KEPT = "history_kept"
#: A stored account that is neither configured nor validly declared retired:
#: every period past its last verified day is withheld until one is done.
META_ACCOUNT_UNRESOLVED = "account_unresolved"
#: Spend is recorded for every day, but some of it has no US-dollar value.
SOURCE_USD_UNAVAILABLE = "usd_unavailable"


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
                    "SELECT started_at, finished_at, exit_code, verdict, summary, "
                    "status, heartbeat_at, clock_timestamp() "
                    "FROM platform_certification_runs "
                    "ORDER BY started_at DESC, id DESC LIMIT 1")
                r = cur.fetchone()
                # PR-ADS-169A: the CRM data to fix the last FINISHED run
                # recorded, shown (with its date) while a run is running or
                # after one was interrupted — those carry none of their own.
                cur.execute(
                    "SELECT finished_at, summary->'crm_data_to_fix' "
                    "FROM platform_certification_runs WHERE status = 'finished' "
                    "ORDER BY started_at DESC, id DESC LIMIT 1")
                last = cur.fetchone()
        if r is None:
            return {"available": True, "row": None}
        return {"available": True, "row": {
            "started_at": r[0], "finished_at": r[1], "exit_code": r[2],
            "verdict": r[3], "summary": r[4] or {}, "status": r[5],
            "heartbeat_at": r[6], "db_now": r[7]},
            "last_finished": ({"finished_at": last[0], "crm_data_to_fix": last[1]}
                              if last else None)}
    except Exception as exc:  # noqa: BLE001
        log.warning("[system_overview] certification unreadable: %s", exc)
        return {"available": False, "row": None}


def certification_block(read: dict, now: datetime) -> dict:
    """Pure: the certification card. Fail closed on anything unexpected."""
    base = {"state": STATE_UNAVAILABLE, "verdict": None, "checked_at": None,
            "age_hours": None, "audits": [], "crm_data_to_fix": None,
            "could_not_run": False,
            "max_age_hours": CERTIFICATION_MAX_AGE_HOURS}
    base.update(run_status=None, interrupted=False, running_audit=None,
                crm_data_to_fix_from=None)
    if not read.get("available"):
        return base
    row = read.get("row")
    if not row:
        return {**base, "state": STATE_NOT_CHECKED}
    last = read.get("last_finished") or {}
    if row.get("status") in (_ROW_RUNNING, _ROW_INTERRUPTED) and \
            isinstance(last.get("crm_data_to_fix"), dict):
        base.update(crm_data_to_fix=last["crm_data_to_fix"],
                    crm_data_to_fix_from=_iso(last.get("finished_at")))
    summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
    status = row.get("status")
    # PR-ADS-169A — a run row is written before the audits start. Rows from
    # before that column existed are finished runs (the column's default).
    if status == _ROW_RUNNING:
        beat = _utc(row.get("heartbeat_at") or row.get("started_at"))
        clock = _utc(row.get("db_now")) or now
        from services.platform_certification_service import (  # noqa: PLC0415
            interrupted_after_minutes,
        )
        if beat is not None and timedelta(0) <= clock - beat <= timedelta(
                minutes=interrupted_after_minutes()):
            return {**base, "state": STATE_RUNNING, "verdict": row.get("verdict"),
                    "run_status": status, "checked_at": _iso(row.get("started_at")),
                    "age_hours": _age_hours(row.get("started_at"), now),
                    "running_audit": summary.get("current_audit")}
        # Its heartbeat stopped: the server running it died. Read it as the
        # sweep will record it — not certified, nothing checked.
        return {**base, "state": STATE_NOT_CERTIFIED, "verdict": row.get("verdict"),
                "run_status": _ROW_INTERRUPTED, "interrupted": True,
                "could_not_run": True,
                "checked_at": _iso(row.get("heartbeat_at") or row.get("started_at")),
                "age_hours": _age_hours(row.get("heartbeat_at")
                                        or row.get("started_at"), now)}
    if status not in (None, _ROW_FINISHED, _ROW_INTERRUPTED):
        # An unrecognised status is not evidence of anything (fail closed).
        return {**base, "state": STATE_NOT_CERTIFIED, "run_status": status,
                "verdict": row.get("verdict"), "could_not_run": True}
    if status == _ROW_INTERRUPTED:
        base.update(run_status=status, interrupted=True)
    else:
        base.update(run_status=status)
    age = _age_hours(row.get("finished_at"), now)
    audits = [{"key": a.get("key"), "outcome": a.get("outcome")}
              for a in summary.get("audits") or [] if isinstance(a, dict)]
    out = {**base, "verdict": row.get("verdict"),
           "checked_at": _iso(row.get("finished_at")), "age_hours": age,
           "audits": audits,
           "crm_data_to_fix": (summary.get("crm_data_to_fix")
                               if summary.get("crm_data_to_fix") is not None
                               else base["crm_data_to_fix"]),
           # The run itself failed (no audit ran): nothing was checked.
           "could_not_run": bool(summary.get("error")) or bool(base["interrupted"])}
    if age is None or age > CERTIFICATION_MAX_AGE_HOURS or age < 0:
        # The recorded verdict is kept for context, but it is not today's.
        return {**out, "state": STATE_NOT_CHECKED}
    state = _STATE_BY_EXIT.get(row.get("exit_code"), STATE_NOT_CERTIFIED)
    if out["interrupted"]:
        state = STATE_NOT_CERTIFIED
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


def _meta_account_line(account_id, row, state, role, now, *, declared=None,
                       broken_set=None) -> dict:
    """One Meta ad account, judged in its own zone. No call to Meta.

    ``role``: ``syncing`` (in a complete credential set), ``broken_set``
    (listed in a set that is incomplete), or ``unlisted`` (stored, in no set).

    A syncing account is judged against the last day the most recent DUE daily
    sync could have settled in the account's zone
    (``meta_ads_spend_service.last_due_sync``) — not against the moment the
    page is viewed. The Los Angeles account's yesterday ends at 08:00 UTC, two
    hours after the sync: judged at page-view time it would read "late" for
    most of every day after a clean sync.
    """
    from db import meta_ads_repository as repo  # noqa: PLC0415
    from services import meta_ads_spend_service as meta  # noqa: PLC0415
    from services.google_ads_spend_service import analyze_coverage  # noqa: PLC0415

    row, state = row or {}, state or {}
    history = row.get("covered_through")
    out = {"account_id": account_id, "account_name": row.get("account_name"),
           "currency": row.get("currency_code"),
           "time_zone": row.get("time_zone_name"),
           "configured": role != "unlisted", "syncing": role == "syncing",
           "status": SOURCE_UNAVAILABLE, "reason": None, "reason_detail": None,
           "complete_through": None, "history_through": history,
           "window_start": None, "window_end": None, "missing_ranges": [],
           "unresolved_failed_chunks": None, "usd_missing_days": None,
           "declared_last_spend_day": declared,
           "not_readable_since": _iso(state.get("not_readable_since")),
           "http_status": None, "error_code": None,
           "last_success_at": _iso(state.get("last_success_at")),
           "age_hours": _age_hours(state.get("last_success_at"), now)}
    kept = (f"history through {history} is kept" if history
            else "no history is stored")
    name = f"{row.get('account_name') or 'Account'} ({account_id})"
    if role == "broken_set":
        return {**out, "status": SOURCE_MISCONFIGURED,
                "reason": (broken_set or {}).get("reason"),
                "summary": f"{name}: credential set {(broken_set or {}).get('set')} "
                           f"is incomplete ({(broken_set or {}).get('reason')}), "
                           f"so it is not updated; {kept}; later periods are "
                           "withheld"}
    if role == "unlisted":
        last_spend = row.get("last_spend_date")
        borne_out = bool(declared and history and history >= declared
                         and not (last_spend and last_spend > declared))
        if borne_out:
            return {**out, "status": META_ACCOUNT_HISTORY_KEPT,
                    "summary": f"{name}: declared retired by the owner (last "
                               f"spend day {declared}); {kept}"}
        why = ("it is not declared retired" if not declared else
               f"its declared last spend day {declared} is not borne out by "
               "what is stored")
        return {**out, "status": META_ACCOUNT_UNRESOLVED,
                "reason": "account_not_configured_not_retired",
                "summary": f"{name}: not in the configuration, and {why}; "
                           f"{kept}; later periods are withheld"}
    failed = state.get("last_status") == "failed"
    if failed:
        out.update(reason=state.get("last_reason"),
                   reason_detail=state.get("last_error"),
                   http_status=state.get("last_http_status"),
                   error_code=state.get("last_error_code"))
    since = str(out["not_readable_since"] or "")[:10] or "an unknown date"
    codes = f"(HTTP {out['http_status']}, code {out['error_code']})"
    if failed and out["reason"] == meta.REASON_TOKEN_REJECTED:
        return {**out, "status": SOURCE_TOKEN_REJECTED,
                "summary": f"{name}: Meta rejected the access token since "
                           f"{since} {codes}; {kept}"}
    if failed and out["reason"] == meta.REASON_NOT_READABLE:
        return {**out, "status": SOURCE_NOT_READABLE,
                "summary": f"{name}: account not readable since {since} "
                           f"{codes}; {kept}"}
    end = meta.account_local_yesterday(row.get("time_zone_name"),
                                       meta.last_due_sync(now))
    if end is None:
        # Never read, or its zone is not a zone: "the last complete days" has
        # no meaning, so nothing is proven.
        return {**out, "status": SOURCE_NOT_PROVEN,
                "reason": out["reason"] or "no_account_on_record",
                "summary": f"{name}: no successful update yet"
                           + (f" — {out['reason_detail']}"
                              if out["reason_detail"] else "")}
    # `end` is the day the account is JUDGED against. If a later day is
    # already held (the page is viewed after the account's day ended and a
    # sync has since read it), that later day is the one reported.
    zone = row.get("time_zone_name")
    viewed = meta.account_local_yesterday(zone, now)
    verdict = None
    candidates = [end]
    while viewed and candidates[0] < viewed:
        candidates.insert(0, candidates[0] + timedelta(days=1))
    for candidate in candidates:           # the latest day first, down to `end`
        start = candidate - timedelta(days=SPEND_WINDOW_DAYS - 1)
        coverage = repo.fetch_meta_spend_coverage(start, candidate, account_id)
        if not coverage.get("available"):
            return {**out, "summary": f"{name}: could not be read"}
        verdict = analyze_coverage(start, candidate, coverage.get("chunks") or [])
        out.update(window_start=start.isoformat(), window_end=candidate.isoformat())
        if verdict.get("complete") is True:
            end = candidate
            break
    if verdict.get("complete") is True:
        unpriced = repo.fetch_meta_fx_missing_dates(account_id, start, end)
        if not unpriced.get("available"):
            return {**out, "summary": f"{name}: could not be read"}
        if unpriced.get("dates"):
            # The spend is recorded; its US-dollar value is not. Not "complete".
            days = unpriced["dates"]
            return {**out, "status": SOURCE_USD_UNAVAILABLE,
                    "reason": (state.get("last_reason")
                               if str(state.get("last_reason") or "").startswith("fx_")
                               else meta.REASON_FX_MISSING),
                    "usd_missing_days": len(days),
                    "summary": f"{name}: spend recorded through {end.isoformat()} "
                               f"({zone}); no US-dollar value for {len(days)} "
                               f"day(s) in {row.get('currency_code')}"}
        return {**out, "status": SOURCE_CURRENT, "reason": None,
                "reason_detail": None, "complete_through": end.isoformat(),
                "summary": f"{name}: complete through {end.isoformat()} ({zone})"}
    ranges = list(verdict.get("missing_ranges") or [])
    words = ", ".join(r["start"] if r["start"] == r["end"]
                      else f"{r['start']} to {r['end']}" for r in ranges)
    return {**out, "status": SOURCE_LATE, "missing_ranges": ranges,
            "unresolved_failed_chunks": len(verdict.get("failed_chunks") or []),
            "summary": f"{name}: late — "
                       + (f"no spend recorded for {words} ({zone})" if words
                          else "a recent spend update failed")
                       + (f" — {out['reason_detail']}"
                          if out["reason_detail"] else "")}


def _meta_spend_source(now: datetime) -> dict:
    """The Meta Ads spend line (PR-ADS-171A). No call to Meta.

    One line for every ad account, with each account in ``accounts``. The
    line is ``current`` only when every account that syncs is complete in US
    dollars through the last day its latest due sync could settle, AND no
    credential set is broken, AND no stored account is left unresolved
    (neither configured nor validly declared retired). Otherwise it carries
    the most serious account status; a broken configuration is shown whatever
    the other accounts say.
    """
    from db import meta_ads_repository as repo  # noqa: PLC0415
    from services import meta_ads_spend_service as meta  # noqa: PLC0415

    out = {"key": "meta_ads_spend", "label": "Meta Ads spend",
           "status": SOURCE_UNAVAILABLE, "reason": None, "reason_detail": None,
           "window_start": None, "window_end": None,
           "complete_through": None, "missing_ranges": [],
           "unresolved_failed_chunks": None,
           "last_success_at": None, "age_hours": None,
           "accounts": [], "misconfigured_sets": [], "retired_invalid": []}
    cfg = meta.config_status()
    out["retired_invalid"] = list(cfg.get("retired_invalid") or [])
    if cfg.get("misconfigured"):
        detail = cfg.get("reason")
        if cfg.get("duplicate_account_ids"):
            detail = (f"{detail}: account "
                      f"{', '.join(cfg['duplicate_account_ids'])} is listed in "
                      "two credential sets")
        return {**out, "status": SOURCE_MISCONFIGURED, "reason": cfg.get("reason"),
                "reason_detail": detail,
                "misconfigured_sets": cfg.get("misconfigured_sets") or []}
    syncing = list(cfg.get("account_ids") or [])
    listed = list(cfg.get("listed_account_ids") or [])
    declared = {d["account_id"]: d["last_spend_day"] for d in cfg.get("retired") or []}
    read = repo.fetch_meta_accounts()
    states = repo.fetch_meta_account_sync_states()
    if not read.get("available") or not states.get("available"):
        return out if listed else {**out, "status": SOURCE_NOT_CONNECTED}
    stored = {a["account_id"]: a for a in read.get("accounts") or []}
    states = states.get("states") or {}
    broken_by_account = {a: s for s in cfg.get("misconfigured_sets") or []
                         for a in s.get("account_ids") or []}
    lines = []
    for account_id in listed + [a for a in stored if a not in listed]:
        role = ("syncing" if account_id in syncing else
                "broken_set" if account_id in listed else "unlisted")
        lines.append(_meta_account_line(
            account_id, stored.get(account_id), states.get(account_id), role,
            now, declared=declared.get(account_id),
            broken_set=broken_by_account.get(account_id)))
    out["accounts"] = lines
    broken = cfg.get("misconfigured_sets") or []
    out["misconfigured_sets"] = broken
    unresolved = [line for line in lines
                  if line["status"] == META_ACCOUNT_UNRESOLVED]
    live = [line for line in lines if line["syncing"]]
    successes = [line["last_success_at"] for line in live]
    if successes and all(successes):
        oldest = min(successes)
        out.update(last_success_at=oldest, age_hours=_age_hours(oldest, now))

    # A broken configuration is shown whatever the other accounts say.
    if broken or out["retired_invalid"] or cfg.get("unparsed_account_entries"):
        if broken:
            reason, detail = broken[0]["reason"], (
                f"credential set {broken[0]['set']}: {broken[0]['reason']}")
        else:
            reason = "retired_declaration_invalid"
            detail = ("META_AD_ACCOUNT_RETIRED has an entry that is not "
                      "<account id>:<YYYY-MM-DD>: "
                      + ", ".join(out["retired_invalid"]))
        return {**out, "status": SOURCE_MISCONFIGURED, "reason": reason,
                "reason_detail": detail}
    if not live:
        if unresolved:
            return {**out, "status": META_ACCOUNT_UNRESOLVED,
                    "reason": unresolved[0]["reason"]}
        return {**out, "status": SOURCE_NOT_CONNECTED}

    statuses = [line["status"] for line in live]
    for status in (SOURCE_UNAVAILABLE, SOURCE_TOKEN_REJECTED, SOURCE_NOT_READABLE,
                   SOURCE_LATE, SOURCE_NOT_PROVEN, SOURCE_USD_UNAVAILABLE):
        if status in statuses:
            break
    else:
        status = META_ACCOUNT_UNRESOLVED if unresolved else SOURCE_CURRENT
    if status == META_ACCOUNT_UNRESOLVED:
        return {**out, "status": status, "reason": unresolved[0]["reason"]}
    if status == SOURCE_CURRENT:
        ends = [line["complete_through"] for line in live]
        starts = [line["window_start"] for line in live]
        return {**out, "status": status, "complete_through": min(ends),
                "window_start": min(starts), "window_end": min(ends)}
    worst = next(line for line in live if line["status"] == status)
    return {**out, "status": status, "reason": worst.get("reason"),
            "reason_detail": worst.get("reason_detail"),
            "missing_ranges": worst.get("missing_ranges") or [],
            "unresolved_failed_chunks": worst.get("unresolved_failed_chunks"),
            "window_start": worst.get("window_start"),
            "window_end": worst.get("window_end")}


_SOURCES = (("hubspot_contacts", "HubSpot contacts", _contacts_source),
            ("hubspot_deals", "HubSpot deals", _deals_source),
            ("google_ads_spend", "Google Ads spend", _spend_source),
            ("meta_ads_spend", "Meta Ads spend", _meta_spend_source))


# ── removed contacts (PR-ADS-163C) ────────────────────────────────────────

def contact_reconciliation_block() -> dict:
    """The latest check for contacts HubSpot no longer has, as recorded.

    Plain facts from ``hubspot_contact_reconciliation_runs``: no verdict is
    derived here and no page publishes a figure from it. ``available`` False
    means the record could not be read; ``status`` None with ``available``
    True means no check has ever run.
    """
    from db import crm_funnel_repository as repo  # noqa: PLC0415
    out = {"available": False, "status": None, "reason": None,
           "started_at": None, "finished_at": None, "contacts_checked": None,
           "batches_unknown": None, "would_remove": None, "removed": None,
           "backlog_qualified": None, "held_open_incident": None,
           "search_disagreements": None, "pending_strikes": None}
    read = repo.fetch_latest_contact_reconciliation()
    if not read.get("available"):
        return out
    out.update(available=True, pending_strikes=read.get("pending_strikes"))
    row = read.get("row")
    if not row:
        return out
    out.update(status=row.get("status"),
               started_at=_iso(row.get("started_at")),
               finished_at=_iso(row.get("finished_at")),
               contacts_checked=row.get("contacts_checked"),
               batches_unknown=row.get("batches_unknown"),
               removed=row.get("removed"),
               # Why a run that is not `success` is not: the run's own text.
               reason=row.get("error"),
               would_remove=row.get("would_remove"),
               backlog_qualified=row.get("backlog_qualified"),
               held_open_incident=row.get("held_open_incident"),
               search_disagreements=row.get("search_disagreements"))
    return out


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
    try:
        reconciliation = contact_reconciliation_block()
    except Exception as exc:  # noqa: BLE001
        log.warning("[system_overview] contact reconciliation unreadable: %s", exc)
        reconciliation = {"available": False}
    return {"generated_at": now.isoformat(),
            "certification": certification_block(read_latest_certification(), now),
            "sources": sources,
            # PR-ADS-163C: the latest removed-contact check, facts only.
            "contact_reconciliation": reconciliation,
            "external_calls_performed": False}
