"""PR-ADS-171A — read-only queries over the Meta Ads spend tables.

SELECTs only. Every function returns ``available: False`` when the database
could not be read, which is a different answer from an empty result: a caller
must never turn an unreadable table into a zero.

USD: a row in a USD account converts at exactly 1 (an identity, not a rate
lookup). Any other currency converts at the ``fx_rates`` row for the row's own
``spend_date``; a day with spend and no such row has no USD value. A row of
zero spend is zero in any currency and needs no rate.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from db.connection import get_conn

logger = logging.getLogger(__name__)

REPORTING_CURRENCY = "USD"

#: The per-day rate of a spend row: 1 for a USD account, else the stored rate.
_RATE = f"(CASE WHEN s.currency_code = '{REPORTING_CURRENCY}' THEN 1 ELSE fx.rate END)"
#: A spend row that has no USD value. A row of zero spend needs no rate.
_UNPRICED = f"({_RATE} IS NULL AND s.cost_micros <> 0)"
_FX_JOIN = """
    LEFT JOIN fx_rates fx
      ON fx.rate_date = s.spend_date
     AND fx.base_currency = s.currency_code
     AND fx.quote_currency = %s
"""


def _iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


_ACCOUNT_COLUMNS = ("account_id, account_name, currency_code, time_zone_name, "
                    "timezone_offset_hours_utc, account_status, first_seen_at, "
                    "fetched_at")


def _account(r) -> dict:
    return {"account_id": r[0], "account_name": r[1], "currency_code": r[2],
            "time_zone_name": r[3],
            "timezone_offset_hours_utc": float(r[4]) if r[4] is not None else None,
            "account_status": r[5], "first_seen_at": r[6], "fetched_at": r[7]}


def fetch_meta_account(account_id: str) -> dict:
    """``{available, account}`` — one stored account row, or account None."""
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "account": None}
            with conn.cursor() as cur:
                cur.execute(f"SELECT {_ACCOUNT_COLUMNS} FROM meta_ads_account "
                            "WHERE account_id = %s", (str(account_id),))
                row = cur.fetchone()
        return {"available": True, "account": _account(row) if row else None}
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_meta_account failed: %s", exc)
        return {"available": False, "account": None}


def fetch_meta_accounts() -> dict:
    """``{available, accounts}`` — EVERY account ever read, configured or not,
    each with its coverage extent and whether it holds any spend row.

    ``covered_from`` / ``covered_through`` are the first and last day inside a
    VERIFIED chunk (None when the account has none); ``last_spend_date`` is the
    last stored day with spend above zero. They describe the
    ledger's extent only; whether a window is covered is decided by the
    coverage functions over the chunks themselves.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "accounts": []}
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {", ".join("a." + c.strip()
                                      for c in _ACCOUNT_COLUMNS.split(","))},
                           c.covered_from, c.covered_through,
                           EXISTS (SELECT 1 FROM meta_ads_campaign_daily_spend s
                                   WHERE s.account_id = a.account_id),
                           (SELECT MAX(s.spend_date)
                            FROM meta_ads_campaign_daily_spend s
                            WHERE s.account_id = a.account_id
                              AND s.cost_micros > 0)
                    FROM meta_ads_account a
                    LEFT JOIN (
                        SELECT account_id, MIN(chunk_start) AS covered_from,
                               MAX(chunk_end) AS covered_through
                        FROM meta_ads_spend_coverage WHERE status = 'verified'
                        GROUP BY account_id) c ON c.account_id = a.account_id
                    ORDER BY a.first_seen_at, a.account_id
                    """)
                accounts = [{**_account(r), "covered_from": _iso(r[8]),
                             "covered_through": _iso(r[9]),
                             "has_spend_rows": bool(r[10]),
                             "last_spend_date": _iso(r[11])}
                            for r in cur.fetchall()]
        return {"available": True, "accounts": accounts}
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_meta_accounts failed: %s", exc)
        return {"available": False, "accounts": []}


def fetch_meta_account_sync_states() -> dict:
    """``{available, states: {account_id: {...}}}`` — the latest sync attempt
    of every account that has had one."""
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "states": {}}
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT account_id, last_attempt_at, last_status, last_reason, "
                    "last_error, last_http_status, last_error_code, "
                    "last_success_at, not_readable_since "
                    "FROM meta_ads_account_sync_state")
                states = {r[0]: {
                    "last_attempt_at": r[1], "last_status": r[2],
                    "last_reason": r[3], "last_error": r[4],
                    "last_http_status": r[5], "last_error_code": r[6],
                    "last_success_at": r[7], "not_readable_since": r[8]}
                    for r in cur.fetchall()}
        return {"available": True, "states": states}
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_meta_account_sync_states failed: %s", exc)
        return {"available": False, "states": {}}


def fetch_meta_campaign_spend(start: date | None, end: date,
                              account_id: str) -> dict:
    """Per-campaign Meta spend over ``[start, end]`` for ONE account.

    Sums raw ``cost_micros``. ``spend_usd`` uses each row's own day's rate
    (1 for USD) and is None for a campaign with any unpriced day;
    ``total_spend_usd`` is None unless every spend day is priced.
    ``currency_code`` is None when no row carries one or the rows disagree —
    never a default.
    """
    empty = {"available": False, "rows": []}
    try:
        with get_conn() as conn:
            if conn is None:
                return empty
            with conn.cursor() as cur:
                where = ("WHERE s.account_id = %s AND s.spend_date <= %s "
                         "AND (%s::date IS NULL OR s.spend_date >= %s)")
                args = (REPORTING_CURRENCY, account_id, end, start, start)
                cur.execute(
                    f"""
                    SELECT s.campaign_id,
                           (ARRAY_AGG(s.campaign_name ORDER BY s.spend_date DESC)
                              FILTER (WHERE s.campaign_name IS NOT NULL))[1],
                           SUM(s.cost_micros)::bigint,
                           SUM((s.cost_micros / 1000000.0) * {_RATE}),
                           COUNT(DISTINCT s.spend_date)
                             FILTER (WHERE {_UNPRICED})
                    FROM meta_ads_campaign_daily_spend s {_FX_JOIN} {where}
                    GROUP BY s.campaign_id ORDER BY s.campaign_id
                    """, args)
                rows, total, total_usd = [], 0, 0.0
                for cid, name, micros, usd, missing in cur.fetchall():
                    micros = int(micros or 0)
                    total += micros
                    priced = not int(missing or 0)
                    usd = float(usd or 0.0) if priced else None
                    if usd is not None:
                        total_usd += usd
                    rows.append({"campaign_id": cid, "campaign_name": name,
                                 "cost_micros": micros,
                                 "spend": micros / 1_000_000,
                                 "spend_usd": usd, "fx_complete": priced})
                cur.execute(
                    f"""
                    SELECT COUNT(DISTINCT s.spend_date),
                           COUNT(DISTINCT s.spend_date)
                             FILTER (WHERE {_UNPRICED}),
                           MIN(s.spend_date), MAX(s.spend_date),
                           COUNT(DISTINCT s.currency_code),
                           MIN(s.currency_code),
                           COUNT(*) FILTER (WHERE s.currency_code IS NULL)
                    FROM meta_ads_campaign_daily_spend s {_FX_JOIN} {where}
                    """, args)
                (spend_days, missing_days, first, last, currencies, currency,
                 unlabelled) = cur.fetchone()
        missing_days = int(missing_days or 0)
        one_currency = int(currencies or 0) == 1 and not int(unlabelled or 0)
        return {
            "available": True, "rows": rows, "account_id": account_id,
            "total_cost_micros": total, "total_spend": total / 1_000_000,
            "total_spend_usd": (round(total_usd, 6) if missing_days == 0
                                else None),
            "campaign_count": len(rows),
            "currency_code": currency if one_currency else None,
            "currency_count": int(currencies or 0),
            "reporting_currency": REPORTING_CURRENCY,
            "spend_days": int(spend_days or 0),
            "fx_missing_days": missing_days,
            "fx_complete": missing_days == 0,
            "first_spend_date": _iso(first), "last_spend_date": _iso(last),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_meta_campaign_spend failed: %s", exc)
        return empty


def fetch_meta_fx_missing_dates(account_id: str, start: date, end: date) -> dict:
    """``{available, dates}`` — the spend days in range that have no USD rate."""
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "dates": []}
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT DISTINCT s.spend_date
                    FROM meta_ads_campaign_daily_spend s {_FX_JOIN}
                    WHERE s.account_id = %s AND s.spend_date BETWEEN %s AND %s
                      AND {_UNPRICED}
                    ORDER BY s.spend_date
                    """, (REPORTING_CURRENCY, account_id, start, end))
                return {"available": True,
                        "dates": [_iso(r[0]) for r in cur.fetchall()]}
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_meta_fx_missing_dates failed: %s", exc)
        return {"available": False, "dates": []}


def fetch_meta_spend_coverage(start: date | None, end: date,
                              account_id: str) -> dict:
    """The coverage chunks of ONE account that intersect ``[start, end]``.

    Shaped like ``revenue_repository.fetch_spend_coverage`` so the shared
    coverage functions read it unchanged, with ``fetched_at`` for the settle
    rule.
    """
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, "chunks": []}
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT chunk_start, chunk_end, status, rows_written,
                           cost_micros_total, account_id, fetched_at,
                           account_time_zone, error_code, error_message,
                           days_restated, max_restated_delta_micros
                    FROM meta_ads_spend_coverage
                    WHERE account_id = %s
                      AND chunk_end >= COALESCE(%s::date, chunk_end)
                      AND chunk_start <= %s
                    ORDER BY chunk_start, chunk_end
                    """, (account_id, start, end))
                chunks = [{
                    "chunk_start": _iso(r[0]), "chunk_end": _iso(r[1]),
                    "status": r[2], "rows_written": r[3],
                    "cost_micros_total": int(r[4] or 0), "account_id": r[5],
                    "fetched_at": r[6], "account_time_zone": r[7],
                    "error_code": r[8], "error_message": r[9],
                    "days_restated": r[10],
                    "max_restated_delta_micros": r[11],
                } for r in cur.fetchall()]
        return {"available": True, "chunks": chunks}
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_meta_spend_coverage failed: %s", exc)
        return {"available": False, "chunks": []}


def fetch_meta_spend_coverage_recency(account_id: str) -> dict:
    """The newest VERIFIED chunk: ``{available, chunk_end, fetched_at}``.

    Newest by ``chunk_end``, not by write time, so refilling an old month does
    not read as a fresh daily feed.
    """
    empty = {"chunk_end": None, "fetched_at": None}
    try:
        with get_conn() as conn:
            if conn is None:
                return {"available": False, **empty}
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT chunk_end, fetched_at FROM meta_ads_spend_coverage "
                    "WHERE status = 'verified' AND account_id = %s "
                    "ORDER BY chunk_end DESC, fetched_at DESC LIMIT 1",
                    (account_id,))
                row = cur.fetchone()
        if row is None:
            return {"available": True, **empty}
        return {"available": True, "chunk_end": _iso(row[0]),
                "fetched_at": row[1]}
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_meta_spend_coverage_recency failed: %s", exc)
        return {"available": False, **empty}
