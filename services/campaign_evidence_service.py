"""
Campaign Evidence — genuine selected-window aggregation (PR-ADS-143).

Builds the Campaign Evidence table from DURABLE source-level tables for the
selected evidence window (7d/14d/30d/60d/180d/all_time), never the overlapping
`campaigns` scheduler snapshot:

  - Spend: ``db.revenue_repository.fetch_canonical_campaign_spend(start, end)`` —
    the SAME canonical google_ads_campaign_daily_spend truth that Revenue by
    Source and the Revenue Decision Mart use, so per-window spend reconciles
    exactly. Native GBP always; FX-safe USD (None when FX coverage is incomplete —
    never native relabelled as USD, never a fabricated 0).
  - Lead outcomes: ``fetch_lead_quality(start, end)`` — the durable `leads` table
    bounded on the HubSpot business-event date (contact_created_at, the SAME grain
    as spend_date), deduplicated per contact, paid-search only, pseudo/email
    campaigns excluded. Confirmed SQL = status 'qualified'; confirmed junk =
    'junk'. Junk rate uses the APPROVED denominator unchanged (verdicted =
    qualified + in_progress + junk + wrong_fit; excludes unknown).

``all_time`` means NO lower date bound → genuine cumulative totals (it is NOT the
latest scheduler snapshot). The campaign universe is the UNION of canonical
campaigns with spend and mapped campaigns with HubSpot lead outcomes — a campaign
is never dropped merely because one side has no record.

Path B (PR-ADS-143 audit): the SCALE/HOLD/FIX/CUT verdict doctrine is NOT valid
for arbitrary windows — it bakes a fixed 30-day design (min_confirmed_sqls_30d,
analysis_window_days: 30) plus a hardcoded $200 dollar floor, and emits ACTION
recommendations calibrated per fixed run-period. So this page presents a factual,
window-safe ``outcome_status`` computed from the selected-window totals only —
never a recomputed or snapshot verdict.

Read-only. No writes to Google Ads or HubSpot.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from analysis.evidence_windows import resolve_evidence_window

logger = logging.getLogger(__name__)

# Reconciliation tolerance shared with the canonical spend services (so it can
# never silently drift from the Revenue-side contract).
try:  # pragma: no cover - import guard
    from services.google_ads_spend_service import SPEND_VARIANCE_TOLERANCE
except Exception:  # noqa: BLE001
    SPEND_VARIANCE_TOLERANCE = 0.02

# ── Factual outcome-status vocabulary (window-safe; never an action verdict) ──
STATUS_SQL_PRODUCER = "SQL producer"
STATUS_JUNK_HEAVY = "Junk-heavy"
STATUS_SPEND_NO_SQL = "Spend without SQL proof"
STATUS_MAPPING_REVIEW = "Mapping review"
STATUS_NO_EVIDENCE = "No outcome evidence"
STATUS_DATA_UNAVAILABLE = "Data unavailable"

# Threshold fallbacks (overridden by config/thresholds.yaml when present). The
# junk-heavy cut mirrors the retired FIX junk threshold; the small-sample guard
# suppresses noisy rates on tiny verdicted samples — both are RATES / counts,
# never a period-relative dollar floor, so they are identically honest for 7d and
# all_time.
_DEFAULT_JUNK_HEAVY_PCT = 25.0
_DEFAULT_SMALL_SAMPLE = 5

_QUALIFIED = "qualified"
_JUNK = "junk"
_IN_PROGRESS = "in_progress"
_WRONG_FIT = "wrong_fit"
_UNKNOWN = "unknown"


def _norm(label) -> str:
    """Normalise a campaign label for matching (lowercase, collapse spaces)."""
    if not label:
        return ""
    return " ".join(str(label).replace("_", " ").strip().lower().split())


def _round2(value):
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def load_status_thresholds() -> dict[str, float]:
    """Junk-heavy % + small-sample guard from config/thresholds.yaml (safe defaults)."""
    junk_pct = _DEFAULT_JUNK_HEAVY_PCT
    small_sample = _DEFAULT_SMALL_SAMPLE
    try:  # pragma: no cover - config presence varies by env
        import yaml  # noqa: PLC0415

        with open("config/thresholds.yaml", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        junk_pct = float(cfg["campaign_verdicts"]["fix"]["min_junk_pct"])
        small_sample = int(cfg["lead_quality"]["small_sample_warning_threshold"])
    except Exception as exc:  # noqa: BLE001
        logger.debug("thresholds.yaml load failed, using defaults: %s", exc)
    return {"junk_heavy_pct": junk_pct, "small_sample": small_sample}


def _window_bounds(window: str, now: datetime | None) -> tuple[date | None, date, dict]:
    """Resolve an evidence window to (start_date | None, end_date, resolved).

    ``all_time`` → start is None (NO lower bound). Otherwise start = end - days.
    end is 'today' (the window is the last N days up to now). Raises
    EvidenceWindowError (caller maps to HTTP 400) for an unknown window.
    """
    resolved = resolve_evidence_window(window)  # raises on unknown window
    now = now or datetime.now(tz=timezone.utc)
    end = now.date()
    days = resolved["days"]
    start = None if days is None else end - timedelta(days=days)
    return start, end, resolved


def _junk_rate(qualified, in_progress, junk, wrong_fit):
    """APPROVED junk rate — verdicted denominator EXCLUDES unknown. None when 0."""
    verdicted = (qualified or 0) + (in_progress or 0) + (junk or 0) + (wrong_fit or 0)
    if verdicted <= 0:
        return None, verdicted
    return round(((junk or 0) / verdicted) * 100, 2), verdicted


def _lead_outcomes_by_campaign(lead_result: dict) -> dict:
    """Aggregate deduped lead rows into per-campaign outcome counts by norm name."""
    by_key: dict = {}
    for r in (lead_result.get("rows") or []):
        name = r.get("campaign_name")
        key = _norm(name)
        if not key:
            continue
        agg = by_key.setdefault(key, {
            "display_name": name, _QUALIFIED: 0, _IN_PROGRESS: 0,
            _JUNK: 0, _WRONG_FIT: 0, _UNKNOWN: 0, "total_leads": 0,
        })
        cat = r.get("status_category")
        if cat not in (_QUALIFIED, _IN_PROGRESS, _JUNK, _WRONG_FIT, _UNKNOWN):
            cat = _UNKNOWN
        agg[cat] += 1
        agg["total_leads"] += 1
    return by_key


def _spend_by_campaign(spend_result: dict) -> dict:
    """Map canonical spend rows by norm name (native GBP, FX-safe USD, campaign_id)."""
    by_key: dict = {}
    for r in (spend_result.get("rows") or []):
        name = r.get("campaign_name")
        key = _norm(name)
        if not key:
            continue
        by_key[key] = {
            "display_name": name,
            "campaign_id": r.get("campaign_id"),
            "native": r.get("spend"),
            "usd": r.get("spend_usd"),
            "fx_complete": bool(r.get("fx_complete")),
        }
    return by_key


def _outcome_status(*, native_spend, confirmed_sqls, confirmed_junk, total_leads,
                    junk_rate, verdicted, lead_available, spend_available,
                    junk_heavy_pct, small_sample) -> str:
    """Factual, window-safe status (first match wins). No action verdict, no
    period-relative dollar floor — only >0 tests and a rate threshold."""
    has_spend = native_spend is not None and native_spend > 0
    sqls = confirmed_sqls or 0
    leads = total_leads or 0
    # 0. Genuinely uncomputable — both sides unavailable (never coerce to 0).
    if (native_spend is None and confirmed_sqls is None
            and confirmed_junk is None and not spend_available and not lead_available):
        return STATUS_DATA_UNAVAILABLE
    # 1. Confirmed SQL production — the headline positive outcome.
    if sqls > 0:
        return STATUS_SQL_PRODUCER
    # 2. Junk-heavy — high verdicted junk rate on a non-trivial sample.
    if (junk_rate is not None and junk_rate >= junk_heavy_pct
            and (verdicted or 0) >= small_sample):
        return STATUS_JUNK_HEAVY
    # 3. Spend but no SQL proof — real spend, no confirmed pipeline.
    if has_spend:
        return STATUS_SPEND_NO_SQL
    # 4. Mapping review — lead outcomes exist but no canonical spend row maps
    #    (absence of a spend row is NOT £0 — it is an unmapped/unavailable spend).
    if leads > 0 and native_spend is None:
        return STATUS_MAPPING_REVIEW
    # 5. Nothing to show for this window.
    return STATUS_NO_EVIDENCE


def _reconcile(row_sum, source_total, *, available: bool) -> str:
    """pass when the rebuilt row-sum matches the source total within tolerance."""
    if not available or source_total is None or row_sum is None:
        return "unavailable"
    try:
        target = float(source_total)
        got = float(row_sum)
    except (TypeError, ValueError):
        return "unavailable"
    if abs(got - target) <= 0.01:
        return "pass"
    denom = abs(target) or 1.0
    return "pass" if (abs(got - target) / denom) <= SPEND_VARIANCE_TOLERANCE else "variance"


def build_campaign_evidence(window: str, now: datetime | None = None) -> dict[str, Any]:
    """Genuine selected-window campaign evidence payload. Read-only.

    Returns the full /api/campaigns response body (campaigns, summary, audit
    metadata, spend/lead semantics). Never raises for a DB outage — returns a
    db_unavailable shape. Raises EvidenceWindowError only for an unknown window
    (the caller maps that to HTTP 400).
    """
    import db.revenue_repository as repo  # noqa: PLC0415

    start, end, resolved = _window_bounds(window, now)
    window_key = resolved["key"]
    is_all_time = resolved["is_all_time"]
    thresholds = load_status_thresholds()

    spend_result = repo.fetch_canonical_campaign_spend(start, end)
    lead_result = repo.fetch_lead_quality(start, end)

    spend_available = bool(spend_result.get("available"))
    lead_available = bool(lead_result.get("available"))

    base = {
        "window": window_key,
        "window_start": start.isoformat() if start else None,
        "window_end": end.isoformat(),
        "all_time": is_all_time,
        "generated_at": (now or datetime.now(tz=timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "spend_semantics": "selected_window_canonical_total",
        "spend_currency": (spend_result.get("currency_code") or "GBP"),
        "reporting_currency": (spend_result.get("reporting_currency") or "USD"),
        "lead_semantics": "selected_window_deduplicated_event_date",
    }

    # Total DB outage on the spend side AND lead side → honest unavailable shape.
    if not spend_available and not lead_available:
        return {
            **base,
            "db_unavailable": True,
            "campaigns": [],
            "summary": _empty_summary(),
            "audit": _audit_block(base, spend_result, lead_result,
                                  spend_native_sum=None, spend_usd_sum=None,
                                  sql_sum=None, junk_sum=None,
                                  spend_available=False, lead_available=False),
        }

    spend_by = _spend_by_campaign(spend_result)
    leads_by = _lead_outcomes_by_campaign(lead_result)
    fx_complete = bool(spend_result.get("fx_complete"))

    campaigns: list[dict] = []
    for key in sorted(set(spend_by) | set(leads_by)):
        sp = spend_by.get(key)
        lq = leads_by.get(key)
        display = (sp or {}).get("display_name") or (lq or {}).get("display_name") or key

        native_spend = sp.get("native") if sp else None
        usd_spend = sp.get("usd") if sp else None
        row_fx_complete = sp.get("fx_complete") if sp else False

        if lq is not None:
            confirmed_sqls = lq[_QUALIFIED]
            confirmed_junk = lq[_JUNK]
            in_progress = lq[_IN_PROGRESS]
            wrong_fit = lq[_WRONG_FIT]
            total_leads = lq["total_leads"]
        elif lead_available:
            # Lead source is live and this campaign has no rows → genuine zero.
            confirmed_sqls = confirmed_junk = in_progress = wrong_fit = total_leads = 0
        else:
            # Lead source unavailable → unknown, never a fabricated 0.
            confirmed_sqls = confirmed_junk = in_progress = wrong_fit = total_leads = None

        junk_rate, verdicted = (None, 0)
        if lq is not None or (lead_available and total_leads is not None):
            junk_rate, verdicted = _junk_rate(confirmed_sqls, in_progress,
                                               confirmed_junk, wrong_fit)

        # CPQL — verified-window USD spend ÷ confirmed SQLs. None when spend/FX or
        # the SQL denominator is unavailable; a genuine zero SQL count → None (the
        # UI renders N/A, never $0).
        cpql_usd = None
        if usd_spend is not None and (confirmed_sqls or 0) > 0:
            cpql_usd = round(float(usd_spend) / confirmed_sqls, 2)

        mapping_status = "mapped" if sp else ("unmapped_spend" if lq else "unmapped")

        campaigns.append({
            "campaign_name": display,
            "campaign_id": (sp or {}).get("campaign_id"),
            "spend_native": _round2(native_spend),
            "spend_usd": _round2(usd_spend),
            "spend_currency": base["spend_currency"],
            "fx_complete": bool(row_fx_complete),
            "total_leads": total_leads,
            "confirmed_sqls": confirmed_sqls,
            "confirmed_junk": confirmed_junk,
            "in_progress": in_progress,
            "wrong_fit": wrong_fit,
            "unknown": (lq[_UNKNOWN] if lq is not None
                        else (0 if lead_available else None)),
            "junk_rate_pct": junk_rate,
            "verdicted_leads": verdicted if (lq is not None or lead_available) else None,
            "cpql_usd": cpql_usd,
            "mapping_status": mapping_status,
            "outcome_status": _outcome_status(
                native_spend=native_spend, confirmed_sqls=confirmed_sqls,
                confirmed_junk=confirmed_junk, total_leads=total_leads,
                junk_rate=junk_rate, verdicted=verdicted,
                lead_available=lead_available, spend_available=spend_available,
                junk_heavy_pct=thresholds["junk_heavy_pct"],
                small_sample=thresholds["small_sample"]),
        })

    # Default ordering: highest native spend first, then most SQLs, then name —
    # unavailable (None) values sink last (never coerced to 0 for the sort).
    campaigns.sort(key=lambda c: (
        c["spend_native"] is None, -(c["spend_native"] or 0.0),
        -(c["confirmed_sqls"] or 0), c["campaign_name"]))

    summary, sums = _build_summary(campaigns, spend_result, lead_result,
                                   spend_available, lead_available, fx_complete)

    return {
        **base,
        "campaigns": campaigns,
        "summary": summary,
        "audit": _audit_block(base, spend_result, lead_result,
                              spend_native_sum=sums["native"], spend_usd_sum=sums["usd"],
                              sql_sum=sums["sqls"], junk_sum=sums["junk"],
                              spend_available=spend_available, lead_available=lead_available),
    }


def unavailable_response(window: str, now: datetime | None = None) -> dict[str, Any]:
    """Consistent db-unavailable payload (same shape as a live response) for the
    handler's last-resort error path — reconciliation statuses are 'unavailable',
    every metric is null (never a fabricated 0). Falls back to a bare shape if the
    window itself cannot be resolved."""
    try:
        start, end, resolved = _window_bounds(window, now)
        window_key, is_all_time = resolved["key"], resolved["is_all_time"]
        window_start = start.isoformat() if start else None
        window_end = end.isoformat()
    except Exception:  # noqa: BLE001 - unknown/unresolvable window
        window_key = window if isinstance(window, str) else "30d"
        window_start = window_end = None
        is_all_time = window_key == "all_time"
    base = {
        "window": window_key, "window_start": window_start, "window_end": window_end,
        "all_time": is_all_time,
        "generated_at": (now or datetime.now(tz=timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "spend_semantics": "selected_window_canonical_total",
        "spend_currency": "GBP", "reporting_currency": "USD",
        "lead_semantics": "selected_window_deduplicated_event_date",
    }
    return {
        **base, "db_unavailable": True, "campaigns": [], "summary": _empty_summary(),
        "audit": {
            "spend_source": "google_ads_campaign_daily_spend (canonical)",
            "lead_source": "leads (durable · contact_created_at · deduped · paid_search)",
            "window_start": window_start, "window_end": window_end,
            "all_time": is_all_time, "fx_status": "unavailable",
            "spend_reconciliation_status": "unavailable",
            "lead_reconciliation_status": "unavailable",
            "event_date_safe": None, "fx_missing_days": None,
        },
    }


def build_campaign_evidence_row(window: str, campaign_name: str,
                                now: datetime | None = None) -> dict[str, Any]:
    """Single campaign's selected-window evidence row (for the drawer headline).

    Always returns a dict (never None): the matched row (same shape
    ``build_campaign_evidence`` emits, so the drawer headline matches the table
    exactly), or a sentinel ``{"_not_found": True, ...}`` when the campaign has no
    evidence in the window / ``{"db_unavailable": True, ...}`` when the source is
    down. Callers branch on those flags. Read-only.
    """
    payload = build_campaign_evidence(window, now=now)
    db_unavailable = payload.get("db_unavailable", False)
    key = _norm(campaign_name)
    for row in payload.get("campaigns", []):
        if _norm(row.get("campaign_name")) == key:
            return {**row, "window": payload.get("window"),
                    "window_start": payload.get("window_start"),
                    "window_end": payload.get("window_end"),
                    "all_time": payload.get("all_time"),
                    "db_unavailable": db_unavailable}
    return {"_not_found": True, "window": payload.get("window"),
            "db_unavailable": db_unavailable}


def _empty_summary() -> dict:
    return {
        "campaigns": 0,
        "spend_usd": None, "spend_native": None, "spend_currency": "GBP",
        "confirmed_sqls_total": None, "confirmed_junk_total": None,
        "overall_cpql_usd": None,
    }


def _build_summary(campaigns, spend_result, lead_result, spend_available,
                   lead_available, fx_complete) -> tuple[dict, dict]:
    """KPI summary reconciled to canonical spend + deduped lead totals."""
    # Spend KPIs come straight from the canonical totals so they reconcile EXACTLY
    # with Revenue by Source / the Revenue Decision Mart for the same window.
    native_total = spend_result.get("total_spend") if spend_available else None
    usd_total = spend_result.get("total_spend_usd") if spend_available else None

    # SQL / junk totals: sum genuine values; None (unavailable) is never summed as 0.
    if lead_available:
        sqls_total = sum((c["confirmed_sqls"] or 0) for c in campaigns
                         if c["confirmed_sqls"] is not None)
        junk_total = sum((c["confirmed_junk"] or 0) for c in campaigns
                         if c["confirmed_junk"] is not None)
    else:
        sqls_total = junk_total = None

    overall_cpql = None
    if usd_total is not None and (sqls_total or 0) > 0:
        overall_cpql = round(float(usd_total) / sqls_total, 2)

    # Row-sum reconciliation checks (rebuilt sums vs source totals).
    native_row_sum = sum((c["spend_native"] or 0.0) for c in campaigns
                         if c["spend_native"] is not None) if spend_available else None
    usd_row_sum = (sum((c["spend_usd"] or 0.0) for c in campaigns
                       if c["spend_usd"] is not None)
                   if (spend_available and fx_complete) else None)

    summary = {
        "campaigns": len(campaigns),
        "spend_usd": _round2(usd_total),
        "spend_native": _round2(native_total),
        "spend_currency": (spend_result.get("currency_code") or "GBP"),
        "confirmed_sqls_total": sqls_total,
        "confirmed_junk_total": junk_total,
        "overall_cpql_usd": overall_cpql,
    }
    sums = {"native": native_row_sum, "usd": usd_row_sum,
            "sqls": sqls_total, "junk": junk_total}
    return summary, sums


def _audit_block(base, spend_result, lead_result, *, spend_native_sum, spend_usd_sum,
                 sql_sum, junk_sum, spend_available, lead_available) -> dict:
    """Machine-verifiable reconciliation metadata (not shown in the UI)."""
    fx_complete = spend_result.get("fx_complete")
    if not spend_available:
        fx_status = "unavailable"
    elif fx_complete:
        fx_status = "verified"
    else:
        fx_status = "incomplete"

    # Spend reconciliation: rebuilt native row-sum == canonical native total; and
    # USD row-sum == canonical USD total when FX is complete.
    native_total = spend_result.get("total_spend") if spend_available else None
    usd_total = spend_result.get("total_spend_usd") if spend_available else None
    native_recon = _reconcile(spend_native_sum, native_total, available=spend_available)
    usd_recon = (_reconcile(spend_usd_sum, usd_total, available=spend_available)
                 if fx_complete else "unavailable")
    if native_recon == "pass" and usd_recon in ("pass", "unavailable"):
        spend_recon = native_recon if usd_recon == "unavailable" else "pass"
    else:
        spend_recon = "variance" if "variance" in (native_recon, usd_recon) else "unavailable"

    # Lead reconciliation: rebuilt SQL/junk row-sums == deduped lead-row aggregate.
    lead_rows = lead_result.get("rows") or []
    src_sqls = sum(1 for r in lead_rows if r.get("status_category") == _QUALIFIED) \
        if lead_available else None
    src_junk = sum(1 for r in lead_rows if r.get("status_category") == _JUNK) \
        if lead_available else None
    sql_recon = _reconcile(sql_sum, src_sqls, available=lead_available)
    junk_recon = _reconcile(junk_sum, src_junk, available=lead_available)
    lead_recon = ("pass" if (sql_recon == "pass" and junk_recon == "pass")
                  else ("variance" if "variance" in (sql_recon, junk_recon)
                        else "unavailable"))

    return {
        "spend_source": "google_ads_campaign_daily_spend (canonical)",
        "lead_source": "leads (durable · contact_created_at · deduped · paid_search)",
        "window_start": base["window_start"],
        "window_end": base["window_end"],
        "all_time": base["all_time"],
        "fx_status": fx_status,
        "spend_reconciliation_status": spend_recon,
        "lead_reconciliation_status": lead_recon,
        "event_date_safe": bool(lead_result.get("event_date_safe")) if lead_available else None,
        "fx_missing_days": spend_result.get("fx_missing_days") if spend_available else None,
    }
