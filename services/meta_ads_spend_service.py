"""PR-ADS-171A — the Meta Ads spend sync: fetch, reconcile, replace, price.

There may be several ad accounts (and several credential sets). Everything
here is PER ACCOUNT: each account has its own currency and time zone, read
from Meta, its own window of days, its own coverage ledger, its own
reconciliation and its own FX. One account failing never blocks another, and
no failure deletes or overwrites what an account already has stored.

One chunk of one account's local days goes through :func:`sync_chunk`, whether
the daily incremental run or the backfill asked for it:

1. every page of the campaign-day query is read (bounded in memory);
2. the account-level total per day is read by a second query;
3. each day's campaign rows must sum to the account-level row within one minor
   unit of the account currency, or the chunk FAILS;
4. only then is the stored range replaced and the chunk recorded ``verified``;
5. USD rates for the chunk's spend days are ensured (not needed for USD).

A failed chunk stores no spend, deletes nothing and records a ``failed``
coverage row. A verified chunk whose USD rates are missing stays verified and
the run is ``partial``: the spend is proven, its USD value is not.

Read-only against Meta (GET). Writes only the local database.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_FALLBACK_LOOKBACK_DAYS = 28
#: One chunk is held in memory before it is written. 28 days of a few hundred
#: campaigns is a few thousand rows; more than this is refused, not paged out.
MAX_ROWS_PER_CHUNK = 50_000

#: ISO 4217 currencies with three decimal places. One minor unit is 0.001.
THREE_DECIMAL_CURRENCIES = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})

REPORTING_CURRENCY = "USD"

REASON_NOT_CONFIGURED = "not_configured"
REASON_MISCONFIGURED = "meta_ads_misconfigured"
REASON_IDENTITY_CHANGED = "account_identity_changed"
REASON_RECONCILIATION = "meta_reconciliation_mismatch"
REASON_PERSISTENCE = "meta_persistence_error"
#: A re-read would take a day with verified spend to zero. Refused.
REASON_RESTATEMENT = "meta_restatement_implausible"
REASON_ROW_LIMIT = "meta_chunk_row_limit"
REASON_MALFORMED = "meta_malformed_response"
REASON_TOKEN_REJECTED = "meta_token_rejected"
REASON_VERSION_DEPRECATED = "meta_api_version_deprecated"
#: Meta REFUSED a read of this account (suspended, closed, permission
#: removed). Its stored history is kept and still read.
REASON_NOT_READABLE = "meta_account_not_readable"
REASON_BATCH = "sync_batch_not_opened"
REASON_FX_MISSING = "fx_rates_missing"
REASON_FX_UNSUPPORTED = "fx_currency_not_supported"


class MetaChunkFailed(RuntimeError):
    """A chunk failed for a reason this service names (not the connector)."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


# ── configuration seams ───────────────────────────────────────────────────

def config_status() -> dict:
    """The connector's non-secret configuration status (late import)."""
    from connectors.meta_ads import config_status as _status  # noqa: PLC0415
    return _status()


def _config() -> dict:
    try:
        import yaml  # noqa: PLC0415
        path = Path(__file__).resolve().parents[1] / "config" / "thresholds.yaml"
        with path.open() as handle:
            return yaml.safe_load(handle) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("[meta_ads] config unreadable: %s", exc)
        return {}


def lookback_days() -> int:
    """``sync.daily_incremental.lookback_days.meta_ads`` (fallback 28)."""
    try:
        value = int(_config()["sync"]["daily_incremental"]["lookback_days"]["meta_ads"])
        return value if value >= 1 else _FALLBACK_LOOKBACK_DAYS
    except Exception:  # noqa: BLE001
        return _FALLBACK_LOOKBACK_DAYS


def fx_provider_currencies() -> frozenset:
    """The currencies the FX provider prices (``meta_ads.fx_provider_currencies``).

    An unreadable list is empty: no currency is assumed to be priced.
    """
    raw = (_config().get("meta_ads") or {}).get("fx_provider_currencies") or []
    return frozenset(str(c).strip().upper() for c in raw if str(c).strip())


def reconciliation_tolerance_micros(currency_code: str | None) -> int:
    """One minor unit of the account currency, in micros.

    0.001 for the three-decimal currencies, 0.01 otherwise — including the
    zero-decimal currencies, whose amounts are whole numbers, so any real
    difference there is at least a hundred times this.
    """
    if (currency_code or "").upper() in THREE_DECIMAL_CURRENCIES:
        return 1_000
    return 10_000


def retired_invalid_text(positions: list) -> str:
    """How an unreadable ``META_AD_ACCOUNT_RETIRED`` is reported: how many
    entries, and where — never what they say."""
    return (f"META_AD_ACCOUNT_RETIRED: {len(positions)} entr"
            f"{'y is' if len(positions) == 1 else 'ies are'} not "
            "<account id>:<YYYY-MM-DD> (or declare one account with two days) "
            f"and {'is' if len(positions) == 1 else 'are'} ignored — position "
            + ", ".join(str(int(p)) for p in positions) + " in the list")


def last_due_sync(now: datetime | None = None) -> datetime:
    """The most recent daily sync that should have FINISHED by ``now`` (UTC).

    The daily incremental sync starts at ``sync.daily_incremental``'s
    ``schedule_hour:schedule_minute`` in its ``timezone`` (09:00 Asia/Amman =
    06:00 UTC). A run is "due" once ``meta_ads.sync_grace_hours`` have passed
    since its start, so a page viewed while this morning's run is still
    working is judged against yesterday morning's.
    """
    cfg = _config()
    daily = ((cfg.get("sync") or {}).get("daily_incremental") or {})
    try:
        hour, minute = int(daily.get("schedule_hour", 9)), int(daily.get("schedule_minute", 0))
        grace = float((cfg.get("meta_ads") or {}).get("sync_grace_hours", 3))
    except (TypeError, ValueError):
        hour, minute, grace = 9, 0, 3.0
    tz = zone_or_none(daily.get("timezone") or "Asia/Amman") or timezone.utc
    base = now or datetime.now(tz=timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    cutoff = (base - timedelta(hours=grace)).astimezone(tz)
    due = cutoff.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if due > cutoff:
        due -= timedelta(days=1)
    return due.astimezone(timezone.utc)


# ── the account and its days ──────────────────────────────────────────────

def zone_or_none(name):
    from services.google_ads_spend_service import zone_or_none as _zone  # noqa: PLC0415
    return _zone(name)


def account_local_yesterday(time_zone_name, now: datetime | None = None) -> date | None:
    """Yesterday in the META account's zone. None when the zone is unknown —
    never the server's day and never another platform's zone."""
    tz = zone_or_none(time_zone_name)
    if tz is None:
        return None
    base = now or datetime.now(tz=timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base.astimezone(tz).date() - timedelta(days=1)


def recent_window(time_zone_name, now: datetime | None = None,
                  days: int | None = None) -> tuple[date, date] | None:
    """``[yesterday - (N-1), yesterday]`` in the Meta account zone. Today is
    never fetched: a day in progress is not a day's spend."""
    end = account_local_yesterday(time_zone_name, now)
    if end is None:
        return None
    span = int(days if days is not None else lookback_days())
    return end - timedelta(days=max(span, 1) - 1), end


def refresh_account(account_id: str) -> dict:
    """Fetch one account from Meta and store it. Raises on any failure,
    including a currency or zone that differs from the one on record for an
    account that has stored spend."""
    from connectors import meta_ads  # noqa: PLC0415
    import db.writers as db_writers  # noqa: PLC0415
    return db_writers.upsert_meta_ads_account(meta_ads.fetch_account(account_id))


def stored_account(account_id: str) -> dict | None:
    from db import meta_ads_repository as repo  # noqa: PLC0415
    return repo.fetch_meta_account(account_id).get("account")


# ── reconciliation ────────────────────────────────────────────────────────

def reconcile_days(rows: list, account_days: list, tolerance_micros: int) -> dict:
    """Per day: do the campaign rows sum to the account-level row?

    Pure. A day present on one side only is compared with zero on the other.
    ``{reconciled, mismatches: [{spend_date, campaign_micros, account_micros,
    delta_micros}], days_compared}``.
    """
    by_campaign: dict = {}
    for r in rows or []:
        day = str(r["spend_date"])
        by_campaign[day] = by_campaign.get(day, 0) + int(r["cost_micros"])
    by_account = {str(r["spend_date"]): int(r["cost_micros"])
                  for r in account_days or []}
    mismatches = []
    for day in sorted(set(by_campaign) | set(by_account)):
        campaign, account = by_campaign.get(day, 0), by_account.get(day, 0)
        if abs(campaign - account) > int(tolerance_micros):
            mismatches.append({"spend_date": day, "campaign_micros": campaign,
                               "account_micros": account,
                               "delta_micros": campaign - account})
    return {"reconciled": not mismatches, "mismatches": mismatches,
            "days_compared": len(set(by_campaign) | set(by_account))}


# ── FX ────────────────────────────────────────────────────────────────────

def ensure_usd_rates(account_id: str, currency: str | None,
                     start: date, end: date, spend_dates: list) -> dict:
    """Make the chunk's spend days convertible to USD, if they can be.

    ``{status: ok|missing|unsupported|not_needed, reason, missing_dates}``.
    USD needs no rate. A currency the provider does not publish is
    ``unsupported`` and no rate is requested or invented for it.
    """
    currency = (currency or "").upper()
    if currency == REPORTING_CURRENCY:
        return {"status": "ok", "reason": None, "missing_dates": [],
                "identity": True}
    if not spend_dates:
        return {"status": "not_needed", "reason": None, "missing_dates": []}
    if currency not in fx_provider_currencies():
        return {"status": "unsupported", "reason": REASON_FX_UNSUPPORTED,
                "missing_dates": sorted(str(d) for d in set(spend_dates))}
    from db import meta_ads_repository as repo  # noqa: PLC0415
    from services.fx_service import ensure_fx_rates  # noqa: PLC0415
    first = min(date.fromisoformat(str(d)) for d in spend_dates)
    last = max(date.fromisoformat(str(d)) for d in spend_dates)
    try:
        ensure_fx_rates(first, last, base_currency=currency,
                        quote_currency=REPORTING_CURRENCY, only_missing=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("[meta_ads] FX refresh failed: %s", type(exc).__name__)
    missing = repo.fetch_meta_fx_missing_dates(account_id, start, end)
    if not missing.get("available"):
        return {"status": "missing", "reason": REASON_FX_MISSING,
                "missing_dates": None}
    dates = missing.get("dates") or []
    if dates:
        return {"status": "missing", "reason": REASON_FX_MISSING,
                "missing_dates": dates}
    return {"status": "ok", "reason": None, "missing_dates": []}


# ── one chunk ─────────────────────────────────────────────────────────────

def describe_failure(exc: Exception) -> dict:
    """``{reason, error, http_status, error_code, readable}`` for a failure.

    The text is the exception's own only for the typed errors this code
    raises, which are built without URLs or credentials. Anything else is
    named by class.

    ``readable``: False when Meta REFUSED the read — a 4xx that is not
    throttling and not a retired API version (``meta_account_not_readable``),
    or a rejected token (``meta_token_rejected``); True when Meta answered and
    the failure is ours or the data's; None when the attempt says nothing
    about the account (a throttle, a network failure, a retired version).
    """
    from connectors import meta_ads  # noqa: PLC0415
    import db.writers as db_writers  # noqa: PLC0415
    out = {"reason": "meta_sync_error",
           "error": f"unexpected {type(exc).__name__}",
           "http_status": None, "error_code": None, "readable": None}
    if isinstance(exc, meta_ads.MetaAdsError):
        out.update(reason=exc.reason, error=str(exc), http_status=exc.http_status,
                   error_code=str(exc.code) if exc.code is not None else None)
        if (isinstance(exc, meta_ads.MetaAdsRefused)
                and not isinstance(exc, meta_ads.MetaAdsVersionDeprecated)):
            out.update(reason=REASON_NOT_READABLE, readable=False)
        elif isinstance(exc, meta_ads.MetaAdsAuthError):
            out["readable"] = False
        elif isinstance(exc, (meta_ads.MetaAdsMalformedResponse,
                              meta_ads.MetaAdsPageOverrun)):
            out["readable"] = True
    elif isinstance(exc, MetaChunkFailed):
        out.update(reason=exc.reason, error=str(exc), readable=True)
    elif isinstance(exc, db_writers.MetaRestatementImplausible):
        out.update(reason=REASON_RESTATEMENT, error=str(exc), readable=True)
    elif isinstance(exc, db_writers.MetaAccountIdentityChanged):
        out.update(reason=REASON_IDENTITY_CHANGED, error=str(exc), readable=True)
    elif isinstance(exc, db_writers.MetaAdsPersistenceError):
        out.update(reason=REASON_PERSISTENCE, error=str(exc), readable=True)
    return out


def sync_chunk(account: dict, start: date, end: date, *,
               sync_run_id: str | None = None,
               accept_zero_restatement: bool = False) -> dict:
    """Fetch, reconcile and replace ``[start, end]`` for ``account``.

    Never raises. ``status`` is ``success`` (verified, USD convertible),
    ``partial`` (verified; USD rates missing or the currency not priced) or
    ``failed`` (nothing replaced; a ``failed`` coverage row recorded).

    A response that would take a day with verified spend to zero fails the
    chunk (``meta_restatement_implausible``) and changes nothing — an empty
    200 reconciles with an empty 200, so reconciliation alone cannot tell
    "Meta un-spent the day" from "Meta answered wrongly".
    ``accept_zero_restatement`` is the backfill script's explicit override;
    the daily step never sets it.
    """
    from connectors import meta_ads  # noqa: PLC0415
    import db.writers as db_writers  # noqa: PLC0415

    account_id = account["account_id"]
    currency = account.get("currency_code")
    zone = account.get("time_zone_name")
    stats: dict = {"pages_read": 0}
    base = {"chunk_start": start.isoformat(), "chunk_end": end.isoformat(),
            "account_id": account_id, "currency": currency,
            "account_time_zone": zone, "external_writes_performed": False}
    try:
        rows: list = []
        for page in meta_ads.iter_campaign_daily_spend(
                account_id, start.isoformat(), end.isoformat(), stats):
            rows.extend(page)
            if len(rows) > MAX_ROWS_PER_CHUNK:
                raise MetaChunkFailed(
                    REASON_ROW_LIMIT,
                    f"more than {MAX_ROWS_PER_CHUNK} campaign-day rows in one "
                    "chunk; use a shorter range")
        campaign_pages = stats["pages_read"]
        account_days = meta_ads.fetch_account_daily_spend(
            account_id, start.isoformat(), end.isoformat(), stats)

        for r in list(rows) + list(account_days):
            if r.get("currency_code") and currency and r["currency_code"] != currency:
                raise MetaChunkFailed(
                    REASON_MALFORMED,
                    f"a spend row is in {r['currency_code']}; the account on "
                    f"record is in {currency}")

        verdict = reconcile_days(rows, account_days,
                                 reconciliation_tolerance_micros(currency))
        if not verdict["reconciled"]:
            first = verdict["mismatches"][0]
            raise MetaChunkFailed(
                REASON_RECONCILIATION,
                f"{len(verdict['mismatches'])} day(s) do not reconcile; first "
                f"{first['spend_date']}: campaign rows {first['campaign_micros']} "
                f"micros, account total {first['account_micros']} micros")

        written = db_writers.replace_meta_campaign_spend_chunk(
            account_id, start, end, rows, account_days,
            currency_code=currency, account_time_zone=zone,
            pages_read=stats["pages_read"], http_status=stats.get("http_status"),
            source_query_version=meta_ads.SOURCE_QUERY_VERSION,
            sync_run_id=sync_run_id,
            allow_zero_restatement=bool(accept_zero_restatement))
    except Exception as exc:  # noqa: BLE001
        failure = describe_failure(exc)
        recorded = db_writers.record_meta_spend_coverage_failure(
            account_id, start, end, error_code=failure["reason"],
            error_message=failure["error"],
            http_status=failure["http_status"] or stats.get("http_status"),
            pages_read=stats.get("pages_read"), account_time_zone=zone,
            sync_run_id=sync_run_id)
        log.warning("[meta_ads] chunk %s..%s failed: %s", start, end,
                    failure["reason"])
        return {**base, "status": "failed", "coverage_status": "failed",
                "failed_coverage_recorded": bool(recorded),
                "rows_written": 0, "pages_read": stats.get("pages_read"),
                **failure}

    # A row of zero spend is zero in any currency: only days with spend need
    # a rate.
    spend_dates = sorted({str(r["spend_date"]) for r in rows if r["cost_micros"]})
    fx = ensure_usd_rates(account_id, currency, start, end, spend_dates)
    fx_ok = fx["status"] in ("ok", "not_needed")
    fx_text = None
    if fx["status"] == "unsupported":
        fx_text = (f"{currency} is not priced by the FX provider; the spend is "
                   "stored in its own currency and has no USD value")
    elif not fx_ok:
        days = fx["missing_dates"]
        fx_text = ("the spend is stored; no USD rate for "
                   + (f"{len(days)} spend day(s): {', '.join(days[:5])}"
                      if days else "an unknown number of spend days"))
    return {**base,
            "status": "success" if fx_ok else "partial",
            "reason": None if fx_ok else fx["reason"],
            "error": fx_text, "coverage_status": "verified",
            "rows_written": written["written"],
            "rows_deleted": written["deleted"],
            "cost_micros": written["cost_micros_total"],
            "verified_zero": written["written"] == 0,
            "campaign_pages_read": campaign_pages,
            "pages_read": stats["pages_read"],
            "days_reconciled": verdict["days_compared"],
            "days_restated": written["days_restated"],
            "days_zeroed": written.get("days_zeroed", 0),
            "max_restated_delta_micros": written["max_restated_delta_micros"],
            "names_preserved": written["names_preserved"],
            "fx_status": fx["status"], "fx_missing_dates": fx["missing_dates"]}


# ── every configured account, one after another ───────────────────────────

def prepare_account(account_id: str, now: datetime | None = None) -> dict:
    """Read one account from Meta and work out ITS window. Never raises.

    ``{account_id, account, zone, window, failure}``. When the account cannot
    be read, the zone on record (if any) still dates the failure; with none
    on record there is no window, and no coverage row can be written.
    """
    account, failure = None, None
    try:
        account = refresh_account(account_id)
    except Exception as exc:  # noqa: BLE001
        failure = describe_failure(exc)
    try:
        zone = (account or stored_account(account_id) or {}).get("time_zone_name")
    except Exception:  # noqa: BLE001
        zone = None
    window = recent_window(zone, now)
    if failure is None and window is None:
        failure = {"reason": REASON_MALFORMED,
                   "error": "the account time zone is not a known zone",
                   "http_status": None, "error_code": None, "readable": True}
    return {"account_id": account_id, "account": account, "zone": zone,
            "window": window, "failure": failure}


def window_span(prepared: list) -> tuple:
    """``(earliest start, latest end)`` over the accounts' windows, or
    ``(None, None)`` when no account has one."""
    windows = [p["window"] for p in prepared if p.get("window")]
    if not windows:
        return None, None
    return min(w[0] for w in windows), max(w[1] for w in windows)


def _account_text(entry: dict) -> str:
    """One account's failure in plain words, naming the account id."""
    account_id = entry["account_id"]
    if entry.get("reason") == REASON_NOT_READABLE:
        return (f"account {account_id} is not readable; its stored history is "
                f"kept ({entry.get('error')})")
    return f"account {account_id}: {entry.get('error') or entry.get('reason')}"


def sync_prepared(prepared: list, *, sync_run_id: str | None = None) -> dict:
    """Sync every prepared account and aggregate. Never raises.

    Each account is independent. ``status``: ``success`` when every account is
    verified and convertible; ``failed`` when every account failed;
    ``partial`` otherwise. ``reason`` / ``error`` are the first non-success
    account's; ``errors`` holds one line per non-success account, each naming
    its account id. Each account's outcome is also recorded in
    ``meta_ads_account_sync_state``.
    """
    import db.writers as db_writers  # noqa: PLC0415

    accounts = []
    for p in prepared:
        account_id, window, failure = p["account_id"], p["window"], p["failure"]
        if failure is not None:
            recorded = False
            if window:
                recorded = db_writers.record_meta_spend_coverage_failure(
                    account_id, window[0], window[1],
                    error_code=failure["reason"], error_message=failure["error"],
                    http_status=failure.get("http_status"),
                    account_time_zone=p.get("zone"), sync_run_id=sync_run_id)
            entry = {"account_id": account_id, "status": "failed",
                     "coverage_status": "failed" if window else None,
                     "failed_coverage_recorded": bool(recorded),
                     "chunk_start": window[0].isoformat() if window else None,
                     "chunk_end": window[1].isoformat() if window else None,
                     "account_time_zone": p.get("zone"), "rows_written": 0,
                     **failure}
        else:
            entry = sync_chunk(p["account"], window[0], window[1],
                               sync_run_id=sync_run_id)
            entry.setdefault("readable", True)
            changed = (p["account"] or {}).get("identity_changed")
            if changed:
                entry["identity_changed"] = changed
        db_writers.record_meta_account_sync(
            account_id, entry["status"], reason=entry.get("reason"),
            error=entry.get("error"), http_status=entry.get("http_status"),
            error_code=entry.get("error_code"), readable=entry.get("readable"))
        accounts.append(entry)
    return aggregate(accounts)


def aggregate(accounts: list) -> dict:
    """The step's verdict over per-account results. Pure."""
    statuses = [a["status"] for a in accounts]
    if statuses and all(s == "success" for s in statuses):
        status = "success"
    elif statuses and all(s == "failed" for s in statuses):
        status = "failed"
    else:
        status = "partial" if statuses else "failed"
    bad = [a for a in accounts if a["status"] != "success"]
    verified_ends = [a["chunk_end"] for a in accounts
                     if a.get("coverage_status") == "verified"]
    return {
        "status": status,
        "reason": bad[0].get("reason") if bad else None,
        "error": "; ".join(_account_text(a) for a in bad) or None,
        "errors": [_account_text(a) for a in bad],
        "accounts": accounts,
        "accounts_synced": len(accounts),
        "rows_written": sum(int(a.get("rows_written") or 0) for a in accounts),
        # Proven through the EARLIEST of the accounts' last days, and only
        # when every account was verified.
        "last_source_date": (min(verified_ends)
                             if verified_ends and len(verified_ends) == len(accounts)
                             else None),
        "external_writes_performed": False,
    }


# ── backfill ──────────────────────────────────────────────────────────────

def month_chunks(start: date, end: date) -> list:
    """``[start, end]`` cut at calendar-month ends."""
    chunks, cursor = [], start
    while cursor <= end:
        nxt = (cursor.replace(day=1) + timedelta(days=32)).replace(day=1)
        chunks.append((cursor, min(end, nxt - timedelta(days=1))))
        cursor = nxt
    return chunks


def run_backfill(date_from: date, date_to: date | None = None, *,
                 account_ids: list | None = None, dry_run: bool = False,
                 resume: bool = False, accept_zero_restatement: bool = False,
                 now: datetime | None = None) -> dict:
    """Backfill ``[date_from, date_to]`` in monthly chunks, per account.

    ``account_ids`` defaults to every configured account; an id that is not
    configured is refused. For each account ``date_to`` defaults to, and is
    capped at, yesterday in THAT account's zone. ``resume`` skips a chunk
    already recorded ``verified`` with exactly these bounds. ``dry_run`` plans
    and writes nothing (the account is read from Meta but not stored). An
    account stops at its first failed chunk; the other accounts still run.

    The backfill writes NO ``sync_batches`` row and no ``runs`` row, like the
    Google Ads spend and geo backfills. ``sync_state`` and the dataset's
    freshness belong to the daily step alone: a backfill of one account (or a
    partial one) must not make a failed daily feed read fresh, nor move the
    daily feed's proven-through date. What the backfill did is on record in
    the coverage ledger, chunk by chunk.

    ``accept_zero_restatement`` lets a chunk take days with verified spend to
    zero (see :func:`sync_chunk`). It exists for an operator who has checked
    that Meta really reports nothing; the daily step has no such switch.
    """
    cfg = config_status()
    out = {"status": "failed", "reason": None, "error": None, "accounts": [],
           "dry_run": bool(dry_run), "external_writes_performed": False}
    if not cfg["configured"]:
        reason = (REASON_MISCONFIGURED if cfg["misconfigured"]
                  else REASON_NOT_CONFIGURED)
        return {**out, "status": "failed" if cfg["misconfigured"] else "skipped",
                "reason": reason, "error": cfg.get("reason")}
    wanted = [str(a).strip() for a in (account_ids or cfg["account_ids"])]
    unknown = [a for a in wanted if a not in cfg["account_ids"]]
    if unknown:
        return {**out, "reason": REASON_MISCONFIGURED,
                "error": f"not a configured account: {', '.join(unknown)}"}
    for account_id in wanted:
        out["accounts"].append(_backfill_account(
            account_id, date_from, date_to, dry_run=dry_run, resume=resume,
            accept_zero_restatement=accept_zero_restatement, now=now))
    statuses = [a["status"] for a in out["accounts"]]
    bad = [a for a in out["accounts"] if a["status"] != "success"]
    if not bad:
        return {**out, "status": "success"}
    status = "failed" if all(s == "failed" for s in statuses) else "partial"
    return {**out, "status": status, "reason": bad[0].get("reason"),
            "error": "; ".join(_account_text(a) for a in bad)}


def _backfill_account(account_id: str, date_from: date, date_to: date | None, *,
                      dry_run: bool, resume: bool, accept_zero_restatement: bool,
                      now: datetime | None) -> dict:
    from connectors import meta_ads  # noqa: PLC0415
    from db import meta_ads_repository as repo  # noqa: PLC0415
    import db.writers as db_writers  # noqa: PLC0415

    out = {"account_id": account_id, "status": "failed", "reason": None,
           "error": None, "chunks": []}
    try:
        account = (meta_ads.fetch_account(account_id) if dry_run
                   else refresh_account(account_id))
    except Exception as exc:  # noqa: BLE001
        failure = describe_failure(exc)
        if not dry_run:
            db_writers.record_meta_account_sync(
                account_id, "failed", reason=failure["reason"],
                error=failure["error"], http_status=failure["http_status"],
                error_code=failure["error_code"], readable=failure["readable"])
        return {**out, "reason": failure["reason"], "error": failure["error"]}
    yesterday = account_local_yesterday(account.get("time_zone_name"), now)
    if yesterday is None:
        return {**out, "reason": REASON_MALFORMED,
                "error": "the account time zone is not a known zone"}
    end = min(date_to, yesterday) if date_to else yesterday
    out.update(account_name=account.get("account_name"),
               currency=account.get("currency_code"),
               account_time_zone=account.get("time_zone_name"),
               date_from=date_from.isoformat(), date_to=end.isoformat())
    if date_from > end:
        return {**out, "status": "success", "reason": "nothing_to_backfill"}

    verified = set()
    if resume:
        coverage = repo.fetch_meta_spend_coverage(date_from, end, account_id)
        if not coverage.get("available"):
            return {**out, "reason": REASON_PERSISTENCE,
                    "error": "the coverage ledger could not be read"}
        verified = {(c["chunk_start"], c["chunk_end"])
                    for c in coverage["chunks"] if c["status"] == "verified"}

    statuses = []
    for start, stop in month_chunks(date_from, end):
        entry = {"chunk_start": start.isoformat(), "chunk_end": stop.isoformat()}
        if (entry["chunk_start"], entry["chunk_end"]) in verified:
            out["chunks"].append({**entry, "status": "already_verified"})
            continue
        if dry_run:
            out["chunks"].append({**entry, "status": "planned"})
            continue
        result = sync_chunk(account, start, stop,
                            accept_zero_restatement=accept_zero_restatement)
        out["chunks"].append(result)
        statuses.append(result["status"])
        if result["status"] == "failed":
            break
    if dry_run or not statuses:
        return {**out, "status": "success"}
    for wanted in ("failed", "partial"):
        if wanted in statuses:
            first = next(c for c in out["chunks"] if c.get("status") == wanted)
            return {**out, "status": wanted, "reason": first.get("reason"),
                    "error": first.get("error")}
    return {**out, "status": "success"}


def batch_error_message(reason: str | None, error: str | None) -> str | None:
    """``"<reason>: <text>"`` — the form a reader splits to get the reason."""
    if not reason:
        return (error or None) and str(error)[:1000]
    return f"{reason}: {error or reason}"[:1000]


def finish_batch(batch_id: int, result: dict, last_source_date) -> bool:
    """Close a batch with the EXACT status (success|partial|failed) and the
    database's row count. The proven-through date is passed only on success."""
    import db.writers as db_writers  # noqa: PLC0415
    status = result.get("status")
    if status not in ("success", "partial", "failed"):
        status = "failed"
    written = int(result.get("rows_written") or 0)
    return db_writers.finish_sync_batch(
        batch_id=batch_id, status=status, row_count=written,
        error_message=(None if status == "success" else
                       batch_error_message(result.get("reason"),
                                           result.get("error"))),
        last_source_date=last_source_date if status == "success" else None)
