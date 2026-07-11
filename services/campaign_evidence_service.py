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

from analysis.evidence_windows import EvidenceWindowError, resolve_evidence_window
from services.campaign_identity_service import normalize_campaign_name

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


# Google Ads account reporting timezone — spend_date rows are account-local days,
# so the window boundary is resolved in this zone (not an implicit UTC date).
ACCOUNT_TZ = "Europe/London"


def _account_today(now: datetime | None) -> date:
    """Today's date in the Google Ads account timezone (Europe/London)."""
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        tz = ZoneInfo(ACCOUNT_TZ)
    except Exception:  # noqa: BLE001 - zoneinfo/tzdata unavailable
        tz = timezone.utc
    base = now or datetime.now(tz=timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base.astimezone(tz).date()


def _window_bounds(window: str, now: datetime | None) -> tuple[date | None, date, dict]:
    """Resolve an evidence window to (start_date | None, end_date, resolved).

    Rolling day windows are INCLUSIVE of exactly N calendar dates:
    ``start = end - (N - 1)`` (so 7d covers exactly 7 dates, ending today). The
    boundary date is resolved in the Google Ads account timezone (Europe/London),
    not an implicit UTC date. ``all_time`` → start is None (NO lower bound). Raises
    EvidenceWindowError (caller maps to HTTP 400) for an unknown window.
    """
    resolved = resolve_evidence_window(window)  # raises on unknown window
    end = _account_today(now)
    days = resolved["days"]
    start = None if days is None else end - timedelta(days=days - 1)
    return start, end, resolved


def _numeric_window_bounds(days: int, now: datetime | None) -> tuple[date, date, dict]:
    """Resolve an EXACT numeric day period (legacy ``days=`` path). Honours the
    requested period verbatim — 90 stays 90, 365 stays 365 — never snapped to a
    dropdown window. Inclusive of exactly ``days`` dates, account-tz bounded."""
    if not (1 <= days <= 365):
        raise EvidenceWindowError(
            f"Unsupported days value '{days}'. Must be 1–365 (or use window=)."
        )
    end = _account_today(now)
    start = end - timedelta(days=days - 1)
    key = f"{days}d"
    return start, end, {"key": key, "days": days, "is_all_time": False}


def _junk_rate(qualified, in_progress, junk, wrong_fit):
    """APPROVED junk rate — verdicted denominator EXCLUDES unknown. None when 0."""
    verdicted = (qualified or 0) + (in_progress or 0) + (junk or 0) + (wrong_fit or 0)
    if verdicted <= 0:
        return None, verdicted
    return round(((junk or 0) / verdicted) * 100, 2), verdicted


def _spend_by_campaign_id(spend_result: dict) -> tuple[dict, dict]:
    """Canonical spend keyed by campaign_id (the stable Google Ads identity) plus a
    normalized-name → {campaign_id} index for the exact-normalized fallback.

    Two campaigns whose display names normalize to the same text keep SEPARATE ids
    (the index maps the shared norm to a SET, so the fallback stays ambiguous and
    never merges them)."""
    by_id: dict = {}
    norm_to_ids: dict = {}
    for r in (spend_result.get("rows") or []):
        cid = r.get("campaign_id")
        if cid is None:
            continue
        cid = str(cid)
        by_id[cid] = {
            "campaign_id": cid,
            "campaign_name": r.get("campaign_name"),
            "native": r.get("spend"),
            "usd": r.get("spend_usd"),
            "fx_complete": bool(r.get("fx_complete")),
        }
        norm = normalize_campaign_name(r.get("campaign_name"))
        if norm:
            norm_to_ids.setdefault(norm, set()).add(cid)
    return by_id, norm_to_ids


def _identity_index(identity_result: dict) -> tuple[dict, dict]:
    """Approved durable mappings → {norm(external_label): mapping} + aliases-by-id.
    Only approved rows are present (fetch_campaign_identity filters); never fuzzy."""
    by_label: dict = {}
    aliases_by_id: dict = {}
    for m in (identity_result.get("mappings") or []):
        label = m.get("external_campaign_label")
        norm = normalize_campaign_name(label)
        if norm:
            by_label.setdefault(norm, m)
        cid = m.get("campaign_id")
        if cid is not None and label:
            aliases_by_id.setdefault(str(cid), set()).add(label)
    return by_label, aliases_by_id


def _assign_lead(label, identity_by_label: dict, spend_norm_to_ids: dict) -> tuple[str, Any]:
    """Map a HubSpot/external lead label to a canonical campaign, or a bucket.

    Returns ``(kind, key)``:
      - ``("google_ads", campaign_id)`` — mapped to a Google Ads campaign;
      - ``("not_google_ads", None)`` — approved as a non-Google-Ads label (excluded);
      - ``("unmatched", norm)`` — no durable/exact mapping → Mapping Review.

    Order: durable APPROVED identity mapping first; the exact-normalized fallback
    (against canonical spend names) applies ONLY when no durable mapping exists;
    never fuzzy; never merge two campaign ids sharing a normalized display name.
    """
    norm = normalize_campaign_name(label)
    if not norm:
        return ("unmatched", "")
    m = identity_by_label.get(norm)
    if m is not None:
        method = (m.get("match_method") or "").strip().lower()
        cid = m.get("campaign_id")
        if method == "not_google_ads":
            return ("not_google_ads", None)
        if cid is not None and method in ("manual", "exact_normalized"):
            return ("google_ads", str(cid))
        return ("unmatched", norm)      # unmatched method / no campaign_id
    ids = spend_norm_to_ids.get(norm)
    if ids and len(ids) == 1:            # exactly one spend campaign → safe fallback
        return ("google_ads", next(iter(ids)))
    return ("unmatched", norm)           # ambiguous (>1 id) or no spend match


def _new_outcomes(display_name):
    return {"display_name": display_name, _QUALIFIED: 0, _IN_PROGRESS: 0,
            _JUNK: 0, _WRONG_FIT: 0, _UNKNOWN: 0, "total_leads": 0}


def _add_lead(agg: dict, status_category):
    cat = status_category
    if cat not in (_QUALIFIED, _IN_PROGRESS, _JUNK, _WRONG_FIT, _UNKNOWN):
        cat = _UNKNOWN
    agg[cat] += 1
    agg["total_leads"] += 1


def _outcome_status(*, native_spend, confirmed_sqls, confirmed_junk, total_leads,
                    junk_rate, verdicted, lead_available, spend_available,
                    junk_heavy_pct, small_sample, is_mapping_review=False) -> str:
    """Factual, window-safe status (first match wins). No action verdict, no
    period-relative dollar floor — only >0 tests and a rate threshold.

    RISK-FIRST precedence: a statistically valid Junk-heavy signal (junk rate over
    the threshold on a sufficiently sampled verdicted set) wins over an incidental
    SQL, so a campaign with 1 SQL and overwhelming junk is never shown as a
    positive "SQL producer".
    """
    has_spend = native_spend is not None and native_spend > 0
    sqls = confirmed_sqls or 0
    leads = total_leads or 0
    # 0. Explicit mapping-review row (unmatched lead label, no canonical spend id).
    if is_mapping_review:
        return STATUS_MAPPING_REVIEW
    # 1. Genuinely uncomputable — both sides unavailable (never coerce to 0).
    if (native_spend is None and confirmed_sqls is None
            and confirmed_junk is None and not spend_available and not lead_available):
        return STATUS_DATA_UNAVAILABLE
    # 2. Junk-heavy — statistically valid high junk rate wins (risk-first).
    if (junk_rate is not None and junk_rate >= junk_heavy_pct
            and (verdicted or 0) >= small_sample):
        return STATUS_JUNK_HEAVY
    # 3. Confirmed SQL production.
    if sqls > 0:
        return STATUS_SQL_PRODUCER
    # 4. Spend but no SQL proof — real spend, no confirmed pipeline.
    if has_spend:
        return STATUS_SPEND_NO_SQL
    # 5. Lead outcomes but no canonical spend row maps (unmapped spend, not £0).
    if leads > 0 and native_spend is None:
        return STATUS_MAPPING_REVIEW
    # 6. Nothing to show for this window.
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


def build_campaign_evidence(window: str, now: datetime | None = None,
                            days: int | None = None) -> dict[str, Any]:
    """Genuine selected-window campaign evidence payload. Read-only.

    HubSpot lead labels are mapped to canonical Google Ads campaigns through the
    durable ``google_ads_campaign_identity`` mapping (approved only; never fuzzy),
    with an exact-normalized fallback for labels with no durable mapping. Campaigns
    are keyed by ``campaign_id`` — two campaigns whose display names normalize to
    the same text are never merged. ``not_google_ads`` labels are excluded from the
    Google Ads SQL/CPQL scope; unmatched labels are preserved as Mapping Review.

    Pass ``days`` (1–365) instead of ``window`` for the exact-numeric legacy path.
    Never raises for a DB outage (returns a db_unavailable shape); raises
    EvidenceWindowError for an unknown window / out-of-range days (caller → 400).
    """
    import db.revenue_repository as repo  # noqa: PLC0415

    if days is not None:
        start, end, resolved = _numeric_window_bounds(days, now)
    else:
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

    if not spend_available and not lead_available:
        return {
            **base, "db_unavailable": True, "campaigns": [],
            "summary": _empty_summary(),
            "audit": _audit_block(base, spend_result, lead_result,
                                  spend_native_sum=None, spend_usd_sum=None,
                                  sql_sum=None, junk_sum=None,
                                  spend_available=False, lead_available=False),
        }

    fx_complete = bool(spend_result.get("fx_complete"))
    spend_by_id, norm_to_ids = _spend_by_campaign_id(spend_result)

    # Durable identity mapping for THIS customer (approved rows only).
    customer_id = spend_result.get("customer_id")
    identity_result = repo.fetch_campaign_identity(customer_id)
    identity_by_label, aliases_by_id = _identity_index(identity_result)

    # ── Assign each deduped paid-search lead to a canonical campaign or bucket ──
    google_by_id: dict = {}
    unmatched: dict = {}
    excluded = _new_outcomes("Not Google Ads")   # not_google_ads aggregate
    for r in (lead_result.get("rows") or []):
        label = r.get("campaign_name")
        cat = r.get("status_category")
        kind, key = _assign_lead(label, identity_by_label, norm_to_ids)
        if kind == "google_ads":
            _add_lead(google_by_id.setdefault(key, _new_outcomes(None)), cat)
        elif kind == "not_google_ads":
            _add_lead(excluded, cat)
        else:  # unmatched → Mapping Review (keep a representative display label)
            _add_lead(unmatched.setdefault(key, _new_outcomes(label)), cat)

    campaigns: list[dict] = []
    # Google Ads campaigns — keyed by campaign_id (stable identity).
    for cid in sorted(set(spend_by_id) | set(google_by_id)):
        sp = spend_by_id.get(cid)
        lq = google_by_id.get(cid)
        display = ((sp or {}).get("campaign_name")
                   or (lq or {}).get("display_name") or cid)
        campaigns.append(_row(
            base, cid, display, sp, lq, lead_available, spend_available, thresholds,
            aliases=sorted(aliases_by_id.get(cid, set())), mapping_status="mapped",
            is_mapping_review=False))

    # Mapping Review — unmatched lead labels (no canonical spend id).
    for norm, agg in sorted(unmatched.items()):
        campaigns.append(_row(
            base, f"unmatched:{norm}", agg["display_name"], None, agg,
            lead_available, spend_available, thresholds, aliases=[],
            mapping_status="unmatched", is_mapping_review=True))

    campaigns.sort(key=lambda c: (
        c["spend_native"] is None, -(c["spend_native"] or 0.0),
        -(c["confirmed_sqls"] or 0), c["campaign_name"] or ""))

    summary, sums = _build_summary(
        campaigns, spend_result, lead_result, spend_available, lead_available,
        fx_complete, unmatched=unmatched, excluded=excluded)

    return {
        **base,
        "campaigns": campaigns,
        "summary": summary,
        "audit": _audit_block(base, spend_result, lead_result,
                              spend_native_sum=sums["native"], spend_usd_sum=sums["usd"],
                              sql_sum=sums["sqls"], junk_sum=sums["junk"],
                              spend_available=spend_available, lead_available=lead_available,
                              identity_available=bool(identity_result.get("available"))),
    }


def _row(base, campaign_key, display, sp, lq, lead_available, spend_available,
         thresholds, *, aliases, mapping_status, is_mapping_review) -> dict:
    """Build one campaign evidence row (Google Ads campaign or Mapping Review)."""
    native_spend = sp.get("native") if sp else None
    usd_spend = sp.get("usd") if sp else None
    row_fx_complete = sp.get("fx_complete") if sp else False

    if lq is not None:
        confirmed_sqls = lq[_QUALIFIED]
        confirmed_junk = lq[_JUNK]
        in_progress = lq[_IN_PROGRESS]
        wrong_fit = lq[_WRONG_FIT]
        unknown = lq[_UNKNOWN]
        total_leads = lq["total_leads"]
    elif lead_available:
        confirmed_sqls = confirmed_junk = in_progress = wrong_fit = unknown = total_leads = 0
    else:
        confirmed_sqls = confirmed_junk = in_progress = wrong_fit = unknown = total_leads = None

    junk_rate, verdicted = (None, 0)
    if lq is not None or (lead_available and total_leads is not None):
        junk_rate, verdicted = _junk_rate(confirmed_sqls, in_progress,
                                          confirmed_junk, wrong_fit)

    # Per-campaign CPQL uses ONLY this campaign's canonical USD spend and its
    # mapped confirmed SQLs. None when spend/FX or the SQL denominator is
    # unavailable; a genuine zero SQL count → None (UI renders N/A, never $0).
    cpql_usd = None
    if usd_spend is not None and (confirmed_sqls or 0) > 0:
        cpql_usd = round(float(usd_spend) / confirmed_sqls, 2)

    return {
        "campaign_key": campaign_key,
        "campaign_id": (sp or {}).get("campaign_id"),
        "campaign_name": display,
        "aliases": aliases,
        "spend_native": _round2(native_spend),
        "spend_usd": _round2(usd_spend),
        "spend_currency": base["spend_currency"],
        "fx_complete": bool(row_fx_complete),
        "total_leads": total_leads,
        "confirmed_sqls": confirmed_sqls,
        "confirmed_junk": confirmed_junk,
        "in_progress": in_progress,
        "wrong_fit": wrong_fit,
        "unknown": unknown,
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
            small_sample=thresholds["small_sample"],
            is_mapping_review=is_mapping_review),
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
                                now: datetime | None = None,
                                campaign_key: str | None = None) -> dict[str, Any]:
    """Single campaign's selected-window evidence row (for the drawer headline).

    STABLE-KEY lookup: when ``campaign_key`` is supplied it matches ONLY the
    ``campaign_key`` / ``campaign_id`` — never a display-name fallback — so a key
    that does not exist returns ``_not_found`` (a wrong key can never resolve to a
    same-named duplicate campaign). Exact-normalized name match is used ONLY when no
    ``campaign_key`` was supplied. Always returns a dict (never None): the matched
    row (same shape ``build_campaign_evidence`` emits, so the drawer headline
    matches the table exactly), a ``{"_not_found": True, ...}`` sentinel, or a
    ``{"db_unavailable": True, ...}`` sentinel. Read-only.
    """
    payload = build_campaign_evidence(window, now=now)
    db_unavailable = payload.get("db_unavailable", False)
    want_key = str(campaign_key) if campaign_key is not None else None
    want_norm = normalize_campaign_name(campaign_name)
    for row in payload.get("campaigns", []):
        if want_key is not None:
            # Key supplied → id/key match ONLY (no display-name fallback).
            matches = (str(row.get("campaign_key")) == want_key
                       or (row.get("campaign_id") is not None
                           and str(row.get("campaign_id")) == want_key))
        else:
            matches = normalize_campaign_name(row.get("campaign_name")) == want_norm
        if matches:
            return {**row, "window": payload.get("window"),
                    "window_start": payload.get("window_start"),
                    "window_end": payload.get("window_end"),
                    "all_time": payload.get("all_time"),
                    "db_unavailable": db_unavailable}
    return {"_not_found": True, "window": payload.get("window"),
            "db_unavailable": db_unavailable}


def _lead_split(rows: list) -> dict:
    """Lead-quality split (approved denominator) from a set of deduped lead rows."""
    agg = _new_outcomes(None)
    for r in rows:
        _add_lead(agg, r.get("status_category"))
    jr, verdicted = _junk_rate(agg[_QUALIFIED], agg[_IN_PROGRESS], agg[_JUNK], agg[_WRONG_FIT])
    return {
        "total_leads": agg["total_leads"],
        "confirmed_sqls": agg[_QUALIFIED], "in_progress": agg[_IN_PROGRESS],
        "confirmed_junk": agg[_JUNK], "wrong_fit": agg[_WRONG_FIT], "unknown": agg[_UNKNOWN],
        "verdicted_leads": verdicted, "junk_rate_pct": jr,
    }


def _country_split(rows: list) -> list:
    """Per-country lead split (same approved denominator), sorted by lead count."""
    by_country: dict = {}
    for r in rows:
        c = (r.get("country") or "").strip() or "(unknown)"
        by_country.setdefault(c, []).append(r)
    out = []
    for country, crows in by_country.items():
        split = _lead_split(crows)
        out.append({"country": country, **split})
    out.sort(key=lambda x: -x["total_leads"])
    return out


def build_campaign_drawer_evidence(window: str, campaign_name: str,
                                   campaign_key: str | None = None,
                                   now: datetime | None = None,
                                   recent_limit: int = 10) -> dict[str, Any]:
    """Drawer supplementary evidence (Lead Quality / Countries / Recent Leads) for
    a single campaign, aggregated across its APPROVED ALIAS SET using the SAME
    durable event-date lead population and per-campaign assignment as the table —
    so the drawer totals reconcile EXACTLY with the table row. Read-only.

    Returns {campaign(row) | None, lead_quality, countries, recent_leads,
    label_set, db_unavailable}. Country/Recent for a Mapping Review row use only
    that exact unmatched external label.
    """
    import db.revenue_repository as repo  # noqa: PLC0415

    row = build_campaign_evidence_row(window, campaign_name, now=now,
                                      campaign_key=campaign_key)
    if row.get("db_unavailable"):
        return {"campaign": None, "lead_quality": None, "countries": [],
                "recent_leads": [], "label_set": [], "db_unavailable": True}
    if row.get("_not_found"):
        return {"campaign": None, "lead_quality": None, "countries": [],
                "recent_leads": [], "label_set": [], "db_unavailable": False}

    start, end, _ = _window_bounds(window, now)
    spend_result = repo.fetch_canonical_campaign_spend(start, end)
    identity_result = repo.fetch_campaign_identity(spend_result.get("customer_id"))
    _, norm_to_ids = _spend_by_campaign_id(spend_result)
    identity_by_label, aliases_by_id = _identity_index(identity_result)

    target_id = row.get("campaign_id")
    is_review = row.get("mapping_status") == "unmatched"
    review_norm = normalize_campaign_name(row.get("campaign_name")) if is_review else None

    detail = repo.fetch_campaign_lead_detail(start, end)
    mine = []
    for r in (detail.get("rows") or []):
        kind, key = _assign_lead(r.get("campaign_name"), identity_by_label, norm_to_ids)
        if is_review:
            if kind == "unmatched" and key == review_norm:
                mine.append(r)
        elif kind == "google_ads" and str(key) == str(target_id):
            mine.append(r)

    # Label set: canonical spend name + all approved aliases (excludes not_google_ads,
    # which never carry a campaign_id); for a review row, only the exact label.
    if is_review:
        label_set = [row.get("campaign_name")]
    else:
        label_set = sorted({row.get("campaign_name"), *aliases_by_id.get(str(target_id), set())}
                           - {None})

    recent = [{
        "company": r.get("company"), "country": r.get("country"),
        "keyword": r.get("keyword"), "mql_status": r.get("mql_status"),
        "status_category": r.get("status_category"),
        "run_date": str(r.get("contact_created_at")) if r.get("contact_created_at") else None,
    } for r in mine[:recent_limit]]

    return {
        "campaign": row,
        "lead_quality": _lead_split(mine) if mine else _lead_split([]),
        "countries": _country_split(mine),
        "recent_leads": recent,
        "label_set": label_set,
        "db_unavailable": False,
    }


def _empty_summary() -> dict:
    return {
        "campaigns": 0,
        "spend_usd": None, "spend_native": None, "spend_currency": "GBP",
        "confirmed_sqls_total": None, "confirmed_junk_total": None,
        "overall_cpql_usd": None, "overall_cpql_scope": "unavailable",
        "mapping_coverage": {
            "mapped_sqls": None, "unmatched_sqls": None,
            "excluded_not_google_sqls": None, "total_paid_search_sqls": None,
            "status": "unavailable",
        },
    }


def _build_summary(campaigns, spend_result, lead_result, spend_available,
                   lead_available, fx_complete, *, unmatched, excluded) -> tuple[dict, dict]:
    """KPI summary reconciled to canonical spend + deduped lead totals, with the
    Google-Ads-aligned CPQL scope + explicit mapping coverage."""
    # Spend KPIs come straight from the canonical totals so they reconcile EXACTLY
    # with Revenue by Source / the Revenue Decision Mart for the same window.
    native_total = spend_result.get("total_spend") if spend_available else None
    usd_total = spend_result.get("total_spend_usd") if spend_available else None

    google_rows = [c for c in campaigns if c["mapping_status"] == "mapped"]
    if lead_available:
        mapped_sqls = sum((c["confirmed_sqls"] or 0) for c in google_rows
                          if c["confirmed_sqls"] is not None)
        mapped_junk = sum((c["confirmed_junk"] or 0) for c in google_rows
                          if c["confirmed_junk"] is not None)
        unmatched_sqls = sum(a[_QUALIFIED] for a in unmatched.values())
        unmatched_junk = sum(a[_JUNK] for a in unmatched.values())
        excluded_sqls = excluded[_QUALIFIED]
        excluded_junk = excluded[_JUNK]
        total_ps_sqls = mapped_sqls + unmatched_sqls + excluded_sqls
        total_junk = mapped_junk + unmatched_junk + excluded_junk
        coverage_status = ("complete" if (unmatched_sqls == 0 and excluded_sqls == 0)
                           else "partial")
    else:
        mapped_sqls = mapped_junk = None
        unmatched_sqls = excluded_sqls = total_ps_sqls = total_junk = None
        coverage_status = "unavailable"

    # Overall CPQL — canonical Google Ads USD spend ÷ SQLs mapped to those SAME
    # Google Ads campaigns (EXCLUDES unmatched + not-Google-Ads SQLs). Never an
    # account-wide figure when coverage is partial → scope disclosed as
    # "mapped_only" so an unmatched qualified lead can never lower canonical CPQL.
    overall_cpql = None
    overall_cpql_scope = "unavailable"
    if usd_total is not None and mapped_sqls is not None and mapped_sqls > 0:
        overall_cpql = round(float(usd_total) / mapped_sqls, 2)
        overall_cpql_scope = "complete" if coverage_status == "complete" else "mapped_only"

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
        "confirmed_sqls_total": mapped_sqls,     # Google-Ads-mapped scope
        "confirmed_junk_total": mapped_junk,
        "overall_cpql_usd": overall_cpql,
        "overall_cpql_scope": overall_cpql_scope,
        "mapping_coverage": {
            "mapped_sqls": mapped_sqls,
            "unmatched_sqls": unmatched_sqls,
            "excluded_not_google_sqls": excluded_sqls,
            "total_paid_search_sqls": total_ps_sqls,
            "status": coverage_status,
        },
    }
    # Audit reconciles ALL assigned leads (mapped + unmatched + excluded) against
    # the source deduped aggregate — proving no lead is dropped by the mapping.
    sums = {"native": native_row_sum, "usd": usd_row_sum,
            "sqls": total_ps_sqls, "junk": total_junk}
    return summary, sums


def _audit_block(base, spend_result, lead_result, *, spend_native_sum, spend_usd_sum,
                 sql_sum, junk_sum, spend_available, lead_available,
                 identity_available=None) -> dict:
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
        "identity_source": "google_ads_campaign_identity (approved mappings)",
        "identity_available": identity_available,
        "account_timezone": ACCOUNT_TZ,
        "window_start": base["window_start"],
        "window_end": base["window_end"],
        "all_time": base["all_time"],
        "fx_status": fx_status,
        "spend_reconciliation_status": spend_recon,
        "lead_reconciliation_status": lead_recon,
        "event_date_safe": bool(lead_result.get("event_date_safe")) if lead_available else None,
        "fx_missing_days": spend_result.get("fx_missing_days") if spend_available else None,
    }
