"""
analysis/historical_intelligence.py

PR-ADS-075 — Historical Intelligence Upgrade

Read-only historical trend analysis from local data.

Responsibility:
  Compute historical trend signals by comparing a "current" period to a
  "previous" period of equal length using data already stored in the local
  database.  All functions are pure (no side-effects, no external API calls)
  unless they accept a database connection.

Doctrine:
  - Phase 1 read-only externally confirmed.
  - Six-month governance lock respected.
  - No Google Ads writes.
  - No HubSpot writes.
  - No negative keyword push.
  - No OCT upload.
  - No campaign pause / bid / budget mutation.
  - All outputs are advisory language only.
  - CPQL is returned as None when confirmed_sqls = 0 (never divide by zero).
  - Insufficient data is labeled honestly.

Trend labels used:
  improving          — metric moved in a positive direction
  deteriorating      — metric moved in a negative direction
  stable             — metric within ±5% of previous period
  insufficient_data  — one or both periods have no usable data
  new_activity       — current has data but previous period has none
  no_recent_activity — previous has data but current period has none

Forbidden action labels (must NOT appear here):
  scale, cut, pause, apply, push, block, send, upload, change bid, change budget
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_STABLE_THRESHOLD = 0.05   # ±5% considered stable
_DEFAULT_LIMIT    = 25
_DEFAULT_DAYS     = 30

# Advisory human-review note templates (read-only language only)
_NOTE_DETERIORATING = (
    "Spend increased while confirmed SQLs decreased and junk rate rose. "
    "Warrants human review."
)
_NOTE_IMPROVING = (
    "Quality metrics improved relative to the previous period. "
    "Warrants continued monitoring."
)
_NOTE_STABLE = (
    "Performance is broadly stable compared to the previous period."
)
_NOTE_INSUFFICIENT = (
    "Insufficient historical data to compute a reliable trend. "
    "More periods required before this signal can be trusted."
)
_NOTE_NEW_ACTIVITY = (
    "New activity detected in current period with no prior comparison period available."
)
_NOTE_NO_RECENT = (
    "No recent activity in current period; historical data exists for the previous period."
)


# ── Direction classifiers ─────────────────────────────────────────────────────

def classify_trend_direction(
    current_value: float | None,
    previous_value: float | None,
) -> str:
    """Return a trend label for a single numeric metric.

    Args:
        current_value:  Value for the current period (e.g. last 30 days).
        previous_value: Value for the previous period (e.g. prior 30 days).

    Returns:
        One of: improving | deteriorating | stable | insufficient_data |
                new_activity | no_recent_activity
    """
    if current_value is None and previous_value is None:
        return "insufficient_data"
    if current_value is None:
        return "no_recent_activity"
    if previous_value is None:
        return "new_activity"
    if previous_value == 0 and current_value == 0:
        return "stable"
    if previous_value == 0:
        return "new_activity"
    delta = (current_value - previous_value) / abs(previous_value)
    if abs(delta) <= _STABLE_THRESHOLD:
        return "stable"
    return "improving" if delta > 0 else "deteriorating"


def _classify_spend_direction(cur: float | None, prev: float | None) -> str:
    """Spend direction: higher spend is neutral (not good/bad by itself)."""
    return "up" if (cur or 0) > (prev or 0) else ("down" if (cur or 0) < (prev or 0) else "stable")


def _classify_sqls_direction(cur: int | None, prev: int | None) -> str:
    """SQL direction: more SQLs is better."""
    return "up" if (cur or 0) > (prev or 0) else ("down" if (cur or 0) < (prev or 0) else "stable")


def _classify_junk_rate_direction(cur: float | None, prev: float | None) -> str:
    """Junk rate direction: lower is better."""
    if cur is None or prev is None:
        return "insufficient_data"
    return "up" if cur > prev else ("down" if cur < prev else "stable")


def _classify_cpql_direction(cur: float | None, prev: float | None) -> str:
    """CPQL direction: lower is better ('better' means cost went down)."""
    if cur is None and prev is None:
        return "insufficient_data"
    if cur is None:
        return "no_recent_activity"
    if prev is None:
        return "new_activity"
    return "better" if cur < prev else ("worse" if cur > prev else "stable")


# ── CPQL safety ───────────────────────────────────────────────────────────────

def _safe_cpql(spend: float | None, sqls: int | None) -> float | None:
    """Return CPQL (cost per qualified lead) or None if SQLs = 0.

    Never divides by zero.  Returns None — not 0 — when sqls is 0.
    """
    if not sqls:
        return None
    if spend is None:
        return None
    return round(spend / sqls, 2)


# ── Movement builder ──────────────────────────────────────────────────────────

def _build_movement(
    current: dict[str, Any],
    previous: dict[str, Any],
) -> dict[str, str]:
    """Return movement labels for all tracked metrics."""
    return {
        "spend":     _classify_spend_direction(current.get("spend"), previous.get("spend")),
        "sqls":      _classify_sqls_direction(current.get("confirmed_sqls"), previous.get("confirmed_sqls")),
        "junk_rate": _classify_junk_rate_direction(current.get("junk_rate"), previous.get("junk_rate")),
        "cpql":      _classify_cpql_direction(current.get("cpql"), previous.get("cpql")),
    }


# ── Trend status classifier ───────────────────────────────────────────────────

def _classify_overall_trend(
    current: dict[str, Any],
    previous: dict[str, Any],
    movement: dict[str, str],
) -> tuple[str, str]:
    """Return (trend_status, human_review_note) for a campaign.

    Logic:
      - If both periods lack data → insufficient_data
      - If only current has data  → new_activity
      - If only previous has data → no_recent_activity
      - Otherwise apply weighted signal:
          * spend up + sqls down → deteriorating
          * junk_rate up         → deteriorating signal
          * sqls up              → improving signal
          * junk_rate down       → improving signal
          * otherwise            → stable
    """
    cur_spend = current.get("spend") or 0
    prev_spend = previous.get("spend") or 0
    cur_sqls  = current.get("confirmed_sqls") or 0
    prev_sqls = previous.get("confirmed_sqls") or 0

    if cur_spend == 0 and cur_sqls == 0 and prev_spend == 0 and prev_sqls == 0:
        return "insufficient_data", _NOTE_INSUFFICIENT
    if cur_spend == 0 and cur_sqls == 0:
        return "no_recent_activity", _NOTE_NO_RECENT
    if prev_spend == 0 and prev_sqls == 0:
        return "new_activity", _NOTE_NEW_ACTIVITY

    degrade_signals  = 0
    improve_signals  = 0

    # Spend up + SQLs down is a combined deterioration signal
    if movement.get("spend") == "up" and movement.get("sqls") == "down":
        degrade_signals += 2

    # Junk rate up is deterioration
    if movement.get("junk_rate") == "up":
        degrade_signals += 1

    # CPQL worse is deterioration
    if movement.get("cpql") == "worse":
        degrade_signals += 1

    # SQLs up is improvement
    if movement.get("sqls") == "up":
        improve_signals += 1

    # Junk rate down is improvement
    if movement.get("junk_rate") == "down":
        improve_signals += 1

    # CPQL better is improvement
    if movement.get("cpql") == "better":
        improve_signals += 1

    if degrade_signals > improve_signals and degrade_signals >= 2:
        note = _build_deteriorating_note(movement)
        return "deteriorating", note
    if improve_signals > degrade_signals:
        return "improving", _NOTE_IMPROVING
    return "stable", _NOTE_STABLE


def _build_deteriorating_note(movement: dict[str, str]) -> str:
    """Compose an advisory note describing observed deterioration signals."""
    parts = []
    if movement.get("spend") == "up":
        parts.append("spend increased")
    if movement.get("sqls") == "down":
        parts.append("confirmed SQLs decreased")
    if movement.get("junk_rate") == "up":
        parts.append("junk rate rose")
    if movement.get("cpql") == "worse":
        parts.append("CPQL worsened")
    if not parts:
        return _NOTE_DETERIORATING
    base = ", ".join(parts).capitalize() + "."
    return f"{base} Warrants human review."


# ── Campaign trend computation ────────────────────────────────────────────────

def compute_campaign_trends(
    rows: list[dict],
    *,
    current_days: int = _DEFAULT_DAYS,
    previous_days: int = _DEFAULT_DAYS,
) -> dict[str, Any]:
    """Compute campaign-level historical trend signals from pre-fetched rows.

    Args:
        rows:          List of dicts, each with at minimum:
                         campaign_name, run_date (str or date),
                         spend_usd, confirmed_sqls, junk_rate_pct, cpql_usd
        current_days:  Length of the current comparison window in days.
        previous_days: Length of the previous comparison window in days.

    Returns:
        {
            "entity": "campaigns",
            "current_days": int,
            "previous_days": int,
            "status": "ok" | "insufficient_data",
            "summary": {"improving": int, "deteriorating": int, ...},
            "rows": [...]
        }

    Read-only.  No external calls.  No writes.
    """
    today = date.today()
    current_cutoff  = today - timedelta(days=current_days)
    previous_cutoff = today - timedelta(days=current_days + previous_days)

    # Partition rows by period
    current_rows:  dict[str, list] = {}
    previous_rows: dict[str, list] = {}

    for row in rows:
        rd = row.get("run_date")
        if rd is None:
            continue
        if isinstance(rd, str):
            try:
                rd = date.fromisoformat(rd)
            except ValueError:
                continue

        cname = (row.get("campaign_name") or "").strip().lower()
        if not cname:
            continue

        if rd >= current_cutoff:
            current_rows.setdefault(cname, []).append(row)
        elif rd >= previous_cutoff:
            previous_rows.setdefault(cname, []).append(row)

    all_names = set(current_rows) | set(previous_rows)
    if not all_names:
        return {
            "entity": "campaigns",
            "current_days": current_days,
            "previous_days": previous_days,
            "status": "insufficient_data",
            "message": "Historical intelligence requires at least two comparable periods.",
            "summary": {
                "improving": 0,
                "deteriorating": 0,
                "stable": 0,
                "insufficient_data": 0,
                "new_activity": 0,
                "no_recent_activity": 0,
            },
            "rows": [],
        }

    result_rows: list[dict] = []
    summary: dict[str, int] = {
        "improving": 0,
        "deteriorating": 0,
        "stable": 0,
        "insufficient_data": 0,
        "new_activity": 0,
        "no_recent_activity": 0,
    }

    for cname in sorted(all_names):
        cur_agg  = _aggregate_campaign_rows(current_rows.get(cname,  []))
        prev_agg = _aggregate_campaign_rows(previous_rows.get(cname, []))
        movement = _build_movement(cur_agg, prev_agg)
        trend_status, note = _classify_overall_trend(cur_agg, prev_agg, movement)
        summary[trend_status] = summary.get(trend_status, 0) + 1

        result_rows.append({
            "campaign_name":     cname,
            "current":           cur_agg,
            "previous":          prev_agg,
            "movement":          movement,
            "trend_status":      trend_status,
            "human_review_note": note,
        })

    # Sort: deteriorating first, then stable, then improving
    _SORT_ORDER = {
        "deteriorating":      0,
        "insufficient_data":  1,
        "no_recent_activity": 2,
        "stable":             3,
        "new_activity":       4,
        "improving":          5,
    }
    result_rows.sort(key=lambda r: _SORT_ORDER.get(r["trend_status"], 99))

    return {
        "entity": "campaigns",
        "current_days": current_days,
        "previous_days": previous_days,
        "status": "ok",
        "summary": summary,
        "rows": result_rows,
    }


def _aggregate_campaign_rows(rows: list[dict]) -> dict[str, Any]:
    """Aggregate a list of campaign snapshot rows into period-level totals."""
    if not rows:
        return {
            "spend":           None,
            "confirmed_sqls":  None,
            "junk_rate":       None,
            "cpql":            None,
            "lead_count":      None,
        }

    total_spend       = sum(float(r.get("spend_usd") or 0) for r in rows)
    total_sqls        = sum(int(r.get("confirmed_sqls") or 0) for r in rows)
    total_leads       = sum(int(r.get("total_leads") or 0) for r in rows)
    junk_rates        = [float(r["junk_rate_pct"]) for r in rows if r.get("junk_rate_pct") is not None]
    avg_junk_rate     = round(sum(junk_rates) / len(junk_rates), 2) if junk_rates else None
    cpql              = _safe_cpql(total_spend, total_sqls)

    return {
        "spend":           round(total_spend, 2),
        "confirmed_sqls":  total_sqls,
        "junk_rate":       avg_junk_rate,
        "cpql":            cpql,
        "lead_count":      total_leads,
    }


# ── Geo trend computation ─────────────────────────────────────────────────────

def compute_geo_trends(
    rows: list[dict],
    *,
    current_days: int = _DEFAULT_DAYS,
    previous_days: int = _DEFAULT_DAYS,
) -> dict[str, Any]:
    """Compute geo-level historical trend signals from pre-fetched rows.

    Args:
        rows:  List of dicts with at minimum:
                 country, run_date, spend_usd, confirmed_sqls (optional),
                 junk_rate_pct (optional)
        current_days:  Current window in days.
        previous_days: Previous window in days.

    Returns same shape as compute_campaign_trends but entity="geo".

    Read-only.  No external calls.  No writes.
    """
    today = date.today()
    current_cutoff  = today - timedelta(days=current_days)
    previous_cutoff = today - timedelta(days=current_days + previous_days)

    current_geo:  dict[str, list] = {}
    previous_geo: dict[str, list] = {}

    for row in rows:
        rd = row.get("run_date")
        if rd is None:
            continue
        if isinstance(rd, str):
            try:
                rd = date.fromisoformat(rd)
            except ValueError:
                continue

        country = (row.get("country") or "(unknown)").strip()
        campaign = (row.get("campaign_name") or "").strip().lower()
        key = f"{country}|{campaign}" if campaign else country

        if rd >= current_cutoff:
            current_geo.setdefault(key, []).append(row)
        elif rd >= previous_cutoff:
            previous_geo.setdefault(key, []).append(row)

    all_keys = set(current_geo) | set(previous_geo)
    if not all_keys:
        return {
            "entity": "geo",
            "current_days": current_days,
            "previous_days": previous_days,
            "status": "insufficient_data",
            "message": "Historical intelligence requires at least two comparable periods.",
            "summary": {
                "improving": 0,
                "deteriorating": 0,
                "stable": 0,
                "insufficient_data": 0,
                "new_activity": 0,
                "no_recent_activity": 0,
            },
            "rows": [],
        }

    result_rows: list[dict] = []
    summary: dict[str, int] = {
        "improving": 0,
        "deteriorating": 0,
        "stable": 0,
        "insufficient_data": 0,
        "new_activity": 0,
        "no_recent_activity": 0,
    }

    for key in sorted(all_keys):
        cur_agg  = _aggregate_geo_rows(current_geo.get(key,  []))
        prev_agg = _aggregate_geo_rows(previous_geo.get(key, []))
        movement = _build_movement(cur_agg, prev_agg)
        trend_status, note = _classify_overall_trend(cur_agg, prev_agg, movement)
        summary[trend_status] = summary.get(trend_status, 0) + 1

        parts = key.split("|", 1)
        country  = parts[0]
        campaign = parts[1] if len(parts) > 1 else None

        result_rows.append({
            "country":           country,
            "campaign_name":     campaign,
            "current":           cur_agg,
            "previous":          prev_agg,
            "movement":          movement,
            "trend_status":      trend_status,
            "human_review_note": note,
        })

    _SORT_ORDER = {
        "deteriorating": 0, "insufficient_data": 1,
        "no_recent_activity": 2, "stable": 3, "new_activity": 4, "improving": 5,
    }
    result_rows.sort(key=lambda r: _SORT_ORDER.get(r["trend_status"], 99))

    return {
        "entity": "geo",
        "current_days": current_days,
        "previous_days": previous_days,
        "status": "ok",
        "summary": summary,
        "rows": result_rows,
    }


def _aggregate_geo_rows(rows: list[dict]) -> dict[str, Any]:
    """Aggregate a list of geo rows into period-level totals."""
    if not rows:
        return {
            "spend":          None,
            "confirmed_sqls": None,
            "junk_rate":      None,
            "cpql":           None,
            "lead_count":     None,
        }
    total_spend = sum(float(r.get("spend_usd") or 0) for r in rows)
    # Geo table doesn't have confirmed_sqls — use lead counts if available
    return {
        "spend":          round(total_spend, 2),
        "confirmed_sqls": None,
        "junk_rate":      None,
        "cpql":           None,
        "lead_count":     None,
    }


# ── Quality movement (generic) ────────────────────────────────────────────────

def compute_quality_movement(
    rows: list[dict],
    *,
    entity_key: str,
) -> dict[str, Any]:
    """Compute quality movement for an arbitrary entity grouped by entity_key.

    Args:
        rows:       List of dicts.  Must contain the entity_key field plus
                    run_date, confirmed_sqls, junk_rate_pct, spend_usd.
        entity_key: Field name to group by (e.g. "campaign_name", "country").

    Returns:
        {
            "entity_key": str,
            "groups": [
                {
                    "key": str,
                    "current": {...},
                    "previous": {...},
                    "movement": {...},
                    "trend_status": str,
                    "human_review_note": str,
                }
            ]
        }

    Read-only.  No external calls.  No writes.
    """
    today = date.today()
    current_cutoff  = today - timedelta(days=_DEFAULT_DAYS)
    previous_cutoff = today - timedelta(days=_DEFAULT_DAYS * 2)

    current_groups:  dict[str, list] = {}
    previous_groups: dict[str, list] = {}

    for row in rows:
        rd = row.get("run_date")
        if rd is None:
            continue
        if isinstance(rd, str):
            try:
                rd = date.fromisoformat(rd)
            except ValueError:
                continue

        key = str(row.get(entity_key) or "(unknown)").strip().lower()

        if rd >= current_cutoff:
            current_groups.setdefault(key, []).append(row)
        elif rd >= previous_cutoff:
            previous_groups.setdefault(key, []).append(row)

    all_keys = set(current_groups) | set(previous_groups)
    groups = []

    for k in sorted(all_keys):
        cur_agg  = _aggregate_campaign_rows(current_groups.get(k,  []))
        prev_agg = _aggregate_campaign_rows(previous_groups.get(k, []))
        movement = _build_movement(cur_agg, prev_agg)
        trend_status, note = _classify_overall_trend(cur_agg, prev_agg, movement)
        groups.append({
            "key":               k,
            "current":           cur_agg,
            "previous":          prev_agg,
            "movement":          movement,
            "trend_status":      trend_status,
            "human_review_note": note,
        })

    return {"entity_key": entity_key, "groups": groups}


# ── DB-backed loader (called by api/server.py) ────────────────────────────────

def load_campaign_trend_rows(
    conn,
    current_days: int,
    previous_days: int,
) -> list[dict]:
    """Query campaigns table for the combined current + previous period.

    Returns a list of dicts for use with compute_campaign_trends().
    Returns an empty list when the DB is unavailable or has no data.

    Read-only SELECT only.  No mutations.
    """
    if conn is None:
        return []
    total_days = current_days + previous_days
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    LOWER(campaign_name) AS campaign_name,
                    run_date,
                    COALESCE(spend_usd, 0)        AS spend_usd,
                    COALESCE(confirmed_sqls, 0)   AS confirmed_sqls,
                    COALESCE(total_leads, 0)      AS total_leads,
                    junk_rate_pct,
                    cpql_usd
                FROM campaigns
                WHERE run_date >= NOW() - INTERVAL '1 day' * %s
                ORDER BY run_date DESC
                """,
                (total_days,),
            )
            cols  = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        log.error("[historical_intelligence] campaign query failed: %s", exc)
        return []


def load_geo_trend_rows(
    conn,
    current_days: int,
    previous_days: int,
) -> list[dict]:
    """Query geo table for the combined current + previous period.

    Returns a list of dicts for use with compute_geo_trends().
    Returns an empty list when the DB is unavailable or has no data.

    Read-only SELECT only.  No mutations.
    """
    if conn is None:
        return []
    total_days = current_days + previous_days
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(NULLIF(BTRIM(country), ''), '(unknown)') AS country,
                    LOWER(campaign_name) AS campaign_name,
                    run_date,
                    COALESCE(spend_usd, 0)  AS spend_usd,
                    COALESCE(clicks, 0)     AS clicks
                FROM geo
                WHERE run_date >= NOW() - INTERVAL '1 day' * %s
                ORDER BY run_date DESC
                """,
                (total_days,),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        log.error("[historical_intelligence] geo query failed: %s", exc)
        return []
