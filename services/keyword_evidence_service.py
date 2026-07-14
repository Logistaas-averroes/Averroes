"""
Keyword Evidence (PR-ADS-146). Read-only.

One durable truth path for the Keyword Evidence page:

  - Metrics come from durable ``keyword_daily_facts`` rows bounded by
    ``source_date`` (the Google Ads reporting date) — NEVER ``run_date``. The
    table enforces a UNIQUE natural key on immutable Google Ads identity
    (source_date + customer_id + campaign_id + ad_group_id + criterion_id) and
    the writer upserts ON CONFLICT, so overlapping scheduler snapshots can never
    multiply the same fact. Two campaigns / ad groups / criteria sharing a
    display name stay separate.
  - Windows are the shared evidence-window vocabulary (7d/14d/30d/60d/180d/
    all_time) resolved in the Google Ads account timezone established by
    Campaign Evidence (PR-ADS-143). ``all_time`` = NO lower date bound. An
    unknown window raises EvidenceWindowError (caller maps to HTTP 400).
  - Currency + per-unit monetary completeness reuse the EXACT Search Terms
    doctrine (PR-ADS-144/145): each unique keyword criterion is FX-converted
    per source-date INDEPENDENTLY (``Σ native_day × rate[day]``); an unproven /
    legacy / FX-incomplete unit never poisons a verified unit; monetary KPIs
    carry a complete / partial / unavailable status and, when partial, a
    verified-only subtotal ("Verified Keyword Spend").
  - Quality diagnostics are LATEST-OBSERVED keyword attributes (never averaged
    across snapshots); NULL = unavailable, kept distinct from a genuine 0.
  - Campaign identity reuses the durable Campaign Evidence identity service
    (campaign_id first, approved mapping for no-id history, never fuzzy,
    not_google_ads excluded, duplicate display names never merged). Ad-group
    identity prefers ad_group_id.
  - Review signals are factual review-prioritisation labels only (never
    winner/loser/waste), with thresholds sourced from config/thresholds.yaml.

No writes. No Google Ads mutations, no negative keywords, no HubSpot writes.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from analysis.evidence_windows import EvidenceWindowError  # noqa: F401 (re-export)
from services.campaign_evidence_service import ACCOUNT_TZ, _window_bounds
from services.campaign_identity_service import normalize_campaign_name

# Reuse the EXACT Search Terms currency / FX / monetary / identity doctrine so
# the two Platform Evidence pages can never disagree on money or identity.
from services.search_term_evidence_service import (  # noqa: PLC0415
    _assess_currency_provenance,
    _convert_daily_series,
    _coverage_block,
    _fx_convert_population,
    _identity_label_index,
    _iso,
    _monetary_summary,
    _resolve_campaign_identity,
    _round2,
    _spend_index,
    MON_COMPLETE,
    MON_PARTIAL,
    MON_UNAVAILABLE,
)

logger = logging.getLogger(__name__)

# ── Vocabulary / limits ──────────────────────────────────────────────────────
KEYWORD_SORTS = ("spend", "clicks", "cpc", "ctr", "quality", "keyword", "last_seen",
                 "attributed_sqls")
# PR-ADS-146C — Attributed-SQL row-state filters.
KEYWORD_SQL_STATES = ("all", "has_sql", "known_zero", "ambiguous", "unavailable")
MATCH_TYPES = ("BROAD", "PHRASE", "EXACT")
MATCH_UNKNOWN = "UNKNOWN"
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

QUALITY_BANDS = ("strong", "medium", "weak", "unavailable")

# Review signals (PR-ADS-146 §12) — factual prioritisation only, never a verdict.
SIG_BROAD_EXPOSURE = "Broad-match exposure"
SIG_LOW_QUALITY = "Low quality evidence"
SIG_SPEND_NO_CONV = "Spend without platform conversion"
SIG_NO_ACTIVITY = "No recent activity"
SIG_LIMITED_DATA = "Limited data"
SIG_NONE = "No platform concern detected"
SIG_QUALITY_UNAVAILABLE = "Quality unavailable"
SIG_CURRENCY_UNAVAILABLE = "Currency unavailable"
SIG_CONV_UNAVAILABLE = "Platform conversions unavailable"
REVIEW_SIGNALS = (
    SIG_BROAD_EXPOSURE, SIG_LOW_QUALITY, SIG_SPEND_NO_CONV, SIG_NO_ACTIVITY,
    SIG_LIMITED_DATA, SIG_NONE, SIG_QUALITY_UNAVAILABLE, SIG_CURRENCY_UNAVAILABLE,
    SIG_CONV_UNAVAILABLE,
)

SPEND_SEMANTICS = (
    "Verified keyword spend is Σ(per-source-date native cost × that date's FX "
    "rate) over durable keyword_daily_facts; a partial subtotal excludes "
    "unverified/FX-incomplete units and is never presented as complete."
)
CURRENCY_SEMANTICS = (
    "cost_micros is raw native Google Ads cost; USD is per-source-date FX; the "
    "legacy spend_usd keyword column is never used."
)
QUALITY_SEMANTICS = (
    "Latest-observed Google Ads quality diagnostics per keyword criterion — "
    "never a selected-window average; NULL = unavailable, distinct from 0."
)
PLATFORM_CONVERSION_DISCLOSURE = (
    "Platform conversion event — not a confirmed SQL, customer or closed-won "
    "outcome."
)


class KeywordQueryError(ValueError):
    """Invalid filter/sort/pagination — caller maps to HTTP 400."""


# ── Config-sourced thresholds (PR-ADS-146 §12) ───────────────────────────────
def _load_signal_thresholds() -> dict:
    """Review-signal thresholds from config/thresholds.yaml (safe fallbacks).
    Never hardcoded frontend numbers — the config doctrine is the source."""
    th = {
        "min_spend": 5.0,             # waste_detection.min_spend_to_flag
        "small_sample_clicks": 5,     # lead_quality.small_sample_warning_threshold
        "quality_weak_max": 5,        # ui.quality_score.medium_min (weak < medium_min)
        "quality_strong_min": 8,      # ui.quality_score.strong_min
    }
    try:  # pragma: no cover - config presence varies by env
        import yaml  # noqa: PLC0415
        with open("config/thresholds.yaml", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        th["min_spend"] = float(cfg["waste_detection"]["min_spend_to_flag"])
        th["small_sample_clicks"] = int(cfg["lead_quality"]["small_sample_warning_threshold"])
        th["quality_weak_max"] = int(cfg["ui"]["quality_score"]["medium_min"])
        th["quality_strong_min"] = int(cfg["ui"]["quality_score"]["strong_min"])
    except Exception as exc:  # noqa: BLE001
        logger.debug("thresholds.yaml load failed, using safe defaults: %s", exc)
    return th


def _iso_str(d) -> str | None:
    return d.isoformat() if hasattr(d, "isoformat") else (str(d) if d is not None else None)


def _criterion_grain(row: dict) -> tuple:
    return (
        str(row.get("customer_id") or ""),
        str(row.get("campaign_id") or ""),
        str(row.get("ad_group_id") or ""),
        str(row.get("criterion_id") or ""),
    )


def _criterion_key(row: dict) -> str:
    """Stable drawer key from immutable identity (NOT display names)."""
    return "|".join((
        str(row.get("campaign_id") or ""),
        str(row.get("ad_group_id") or ""),
        str(row.get("criterion_id") or ""),
    ))


def _norm_match_type(mt) -> str:
    up = str(mt or "").strip().upper()
    return up if up in MATCH_TYPES else MATCH_UNKNOWN


def _is_active(unit: dict) -> bool:
    """Active = not paused/removed. Unknown status is treated as active (we do
    not exclude a keyword just because status is unavailable)."""
    status = (unit.get("criterion_status") or "").upper()
    return status not in ("PAUSED", "REMOVED")


def _quality_band(quality_score, th: dict) -> str:
    if quality_score is None:
        return "unavailable"
    if quality_score >= th["quality_strong_min"]:
        return "strong"
    if quality_score >= th["quality_weak_max"]:
        return "medium"
    return "weak"


# Conversion completeness statuses (PR-ADS-146 §2).
CONV_COMPLETE = "complete"
CONV_PARTIAL = "partial"
CONV_UNAVAILABLE = "unavailable"


def _conversion_evidence(unit: dict) -> tuple:
    """Classify a criterion's platform-conversion evidence and known total.

    Returns ``(status, known_total)`` where status is complete (every fact row
    reported a conversion count), partial (some rows reported, some are NULL —
    NEVER a confirmed zero), or unavailable (no row reported one → total None).
    A raw ``conversions`` value is honoured for simple/mocked rows that predate
    the completeness columns."""
    known = unit.get("known_conversion_fact_rows")
    if known is not None:
        missing = unit.get("missing_conversion_fact_rows") or 0
        total = unit.get("conversions_known_total")
        if known == 0:
            return CONV_UNAVAILABLE, None
        if missing > 0:
            return CONV_PARTIAL, (float(total) if total is not None else 0.0)
        return CONV_COMPLETE, (float(total) if total is not None else 0.0)
    # Fallback: a single scalar conversions value (mocked/simple rows).
    conv = unit.get("conversions")
    if conv is None:
        return CONV_UNAVAILABLE, None
    return CONV_COMPLETE, float(conv)


# ── Population build ─────────────────────────────────────────────────────────
def _build_population(start: date | None, end: date) -> dict:
    """Fetch + assemble the selected-window keyword population at the unique
    criterion grain. Returns {available, units, source, canonical,
    identity_available, currency_info}. Each unit is FX-converted independently.
    """
    import db.keyword_repository as kw_repo  # noqa: PLC0415
    import db.revenue_repository as revenue_repo  # noqa: PLC0415

    agg = kw_repo.fetch_keyword_aggregates(start, end)
    if not agg.get("available"):
        return {"available": False, "units": [], "source": {},
                "canonical": {"available": False}, "identity_available": False,
                "currency_info": _monetary_summary([])}

    canonical = revenue_repo.fetch_canonical_campaign_spend(start, end)
    identity = revenue_repo.fetch_campaign_identity(canonical.get("customer_id"))
    spend_by_id, norm_to_ids = _spend_index(canonical)
    identity_by_label, aliases_by_id = _identity_label_index(identity)

    # Per-(criterion, source_date) native cost for genuine per-date FX.
    daily = kw_repo.fetch_keyword_daily_costs(start, end)
    daily_by_grain: dict = {}
    for d in (daily.get("rows") or []):
        grain = _criterion_grain(d)
        micros = d.get("cost_micros")
        if micros is None:
            continue
        iso = _iso_str(d.get("source_date"))
        daily_by_grain.setdefault(grain, {})[iso] = daily_by_grain.get(grain, {}).get(iso, 0) + int(micros)

    units: list = []
    for g in (agg.get("rows") or []):
        status, campaign_key, display = _resolve_campaign_identity(
            g.get("campaign_id"), g.get("campaign_name"),
            spend_by_id, norm_to_ids, identity_by_label)
        unit = dict(g)
        unit["mapping_status"] = status
        unit["campaign_key"] = campaign_key
        unit["campaign_display"] = display
        unit["match_type_norm"] = _norm_match_type(g.get("match_type"))
        unit["criterion_key"] = _criterion_key(g)
        unit["_provenance"] = _assess_currency_provenance(g)
        unit["_daily_micros"] = daily_by_grain.get(_criterion_grain(g), {})
        unit["_conv_status"], unit["_conv_total"] = _conversion_evidence(g)
        units.append(unit)

    _fx_convert_population(units, start, end)
    # A unit is monetarily verified only when its currency is proven AND FX is
    # complete (same rule the monetary summary uses) — legacy/FX-incomplete units
    # never contribute to verified spend and surface a Currency-unavailable signal.
    for u in units:
        u["_currency_verified"] = bool(u.get("fx_complete")) and u.get("spend_usd") is not None

    return {
        "available": True,
        "units": units,
        "source": agg.get("source") or {},
        "canonical": canonical,
        "identity_available": bool(identity.get("available")),
        "aliases_by_id": aliases_by_id,
        "currency_info": _monetary_summary(units),
    }


# ── HubSpot SQL attribution (PR-ADS-146C §4) ─────────────────────────────────
def _keyword_sql_attribution(pop: dict, start: date | None, end: date) -> dict:
    """Attribute deduplicated qualified SQL contacts to the keyword population
    over the WHOLE window (filter-independent). Defensive — a failure yields an
    unavailable verdict rather than breaking the page."""
    try:
        from services.platform_sql_attribution_service import (  # noqa: PLC0415
            attribute_keywords, fetch_and_resolve_contacts,
        )
        contacts_res = fetch_and_resolve_contacts(start, end)
        available = bool(contacts_res.get("available"))
        contacts = contacts_res.get("contacts") or []
        criteria = [{
            "criterion_key": u.get("criterion_key"),
            "campaign_id": u.get("campaign_id"),
            "keyword_text": u.get("keyword_text"),
            "mapping_status": u.get("mapping_status"),
        } for u in (pop.get("units") or [])]
        attr = attribute_keywords(contacts, criteria, available=available)
        attr["contacts"] = contacts
        return attr
    except Exception as exc:  # noqa: BLE001
        logger.warning("[keyword-evidence] SQL attribution failed: %s", exc)
        return {"by_criterion": {}, "reconciliation": {}, "coverage": {},
                "audit": {}, "available": False, "contacts": []}


def _apply_keyword_sql(rows: list, attr: dict, *, drawer: bool = False) -> None:
    """Merge per-criterion attribution onto rows. ``sql_contact_keys`` is included
    only in drawer payloads (never leaked into the table/CSV)."""
    by = attr.get("by_criterion") or {}
    cov_pct = (attr.get("coverage") or {}).get("coverage_pct")
    for r in rows:
        st = by.get(r.get("criterion_key")) or {
            "attributed_sqls": None, "sql_attribution_status": "unavailable",
            "sql_attribution_source": None, "sql_ambiguity_reason": None,
            "sql_candidate_count": None, "sql_contact_keys": []}
        r["attributed_sqls"] = st.get("attributed_sqls")
        r["sql_attribution_status"] = st.get("sql_attribution_status")
        r["sql_attribution_source"] = st.get("sql_attribution_source")
        r["sql_attribution_coverage"] = cov_pct
        r["sql_candidate_count"] = st.get("sql_candidate_count")
        r["sql_ambiguity_reason"] = st.get("sql_ambiguity_reason")
        if drawer:
            r["sql_contact_keys"] = st.get("sql_contact_keys") or []


def _sql_attribution_block(attr: dict) -> dict:
    """Response-level SQL-attribution audit + reconciliation (PR-ADS-146C §11/§13)."""
    from services.platform_sql_attribution_service import (  # noqa: PLC0415
        SQL_ATTRIBUTION_METHOD_KEYWORD, SQL_DATE_FIELD, SQL_DEDUP_KEY,
        SQL_DEFINITION, SQL_SOURCE,
    )
    recon = attr.get("reconciliation") or {}
    cov = attr.get("coverage") or {}
    audit = attr.get("audit") or {}
    comp = attr.get("completeness") or {}
    return {
        "sql_source": SQL_SOURCE,
        "sql_definition": SQL_DEFINITION,
        "sql_date_field": SQL_DATE_FIELD,
        "sql_dedup_key": SQL_DEDUP_KEY,
        "sql_attribution_method": SQL_ATTRIBUTION_METHOD_KEYWORD,
        "sql_attribution_available": bool(attr.get("available")),
        "sql_attribution_coverage_pct": cov.get("coverage_pct"),
        "sql_attributed_count": recon.get("uniquely_attributed_sql_contacts"),
        "sql_ambiguous_count": recon.get("ambiguous_sql_contacts"),
        "sql_unattributed_count": recon.get("unattributed_sql_contacts"),
        "sql_total_contacts": recon.get("total_sql_contacts"),
        "sql_row_sum": recon.get("row_sql_sum"),
        "sql_reconciliation_status": recon.get("reconciliation_status"),
        # §3 — attribution completeness / zero-proof.
        "sql_contacts_with_campaign_identity": comp.get("sql_contacts_with_campaign_identity"),
        "sql_contacts_with_keyword": comp.get("sql_contacts_with_keyword"),
        "sql_contacts_missing_campaign_identity": comp.get("sql_contacts_missing_campaign_identity"),
        "sql_contacts_missing_keyword": comp.get("sql_contacts_missing_keyword"),
        "sql_attribution_completeness_status": comp.get("sql_attribution_completeness_status"),
        "zero_proof_available": comp.get("zero_proof_available"),
        "population": audit.get("population"),
        "keyword_attribution": audit.get("keyword_attribution"),
    }


# ── Review signal (factual, config-thresholded) ──────────────────────────────
def _keyword_signal(unit: dict, th: dict, window_end: date | None) -> str:
    """One factual review-prioritisation signal per keyword (PR-ADS-146 §12).

    Precedence (first match wins), all thresholds from config:
      1. Currency unavailable — money cannot be verified for this unit.
      2. No recent activity — served nothing in the window (0 impr & 0 clicks).
      3. Limited data — clicks below the small-sample guard (too little to judge).
      4. Broad-match exposure — broad match with verified spend ≥ min_spend.
      5. Low quality evidence — a latest quality score below the medium band.
      6. Quality unavailable — active keyword with no observed quality score.
      7. Spend without platform conversion — verified spend ≥ min_spend, 0 conv.
      8. No platform concern detected — default.
    Never returns a verdict (winner/loser/waste); zero platform conversions is
    never treated as confirmed waste.
    """
    verified = bool(unit.get("_currency_verified"))
    if not verified:
        return SIG_CURRENCY_UNAVAILABLE

    clicks = int(unit.get("clicks") or 0)
    impressions = int(unit.get("impressions") or 0)
    if impressions == 0 and clicks == 0:
        return SIG_NO_ACTIVITY
    if clicks < th["small_sample_clicks"]:
        return SIG_LIMITED_DATA

    verified_usd = float(unit.get("spend_usd") or 0.0)
    if unit.get("match_type_norm") == "BROAD" and verified_usd >= th["min_spend"]:
        return SIG_BROAD_EXPOSURE

    qs = unit.get("quality_score")
    if qs is not None and qs < th["quality_weak_max"]:
        return SIG_LOW_QUALITY
    if qs is None and _is_active(unit):
        return SIG_QUALITY_UNAVAILABLE

    # Spend-without-conversion requires COMPLETE conversion evidence AND a total
    # of exactly zero — never inferred from a partial/unavailable value (a
    # criterion with a 0 day and a NULL day is NOT a confirmed zero).
    if verified_usd >= th["min_spend"]:
        conv_status = unit.get("_conv_status")
        conv_total = unit.get("_conv_total")
        if conv_status in (CONV_PARTIAL, CONV_UNAVAILABLE):
            return SIG_CONV_UNAVAILABLE
        if conv_status == CONV_COMPLETE and float(conv_total or 0) == 0.0:
            return SIG_SPEND_NO_CONV

    return SIG_NONE


# ── Serialization ────────────────────────────────────────────────────────────
def _ctr(clicks, impressions):
    c = int(clicks or 0)
    i = int(impressions or 0)
    return round(c / i * 100.0, 2) if i > 0 else None


def _cpc(spend_usd, clicks):
    c = int(clicks or 0)
    return round(float(spend_usd) / c, 2) if (spend_usd is not None and c > 0) else None


def _unit_row(unit: dict, th: dict, window_end: date | None) -> dict:
    verified = bool(unit.get("_currency_verified"))
    spend_usd = unit.get("spend_usd") if verified else None
    return {
        "criterion_key": unit.get("criterion_key"),
        "criterion_id": unit.get("criterion_id"),
        "keyword": unit.get("keyword_text"),
        "match_type": unit.get("match_type_norm"),
        "criterion_status": unit.get("criterion_status"),
        "campaign": unit.get("campaign_display"),
        "campaign_key": unit.get("campaign_key"),
        "campaign_id": unit.get("campaign_id"),
        "ad_group": unit.get("ad_group_name"),
        "ad_group_id": unit.get("ad_group_id"),
        "mapping_status": unit.get("mapping_status"),
        # Money — verified USD (per-date FX) or null; native shown separately.
        "spend_usd": _round2(spend_usd) if spend_usd is not None else None,
        "spend_native": _round2((unit.get("_provenance") or {}).get("spend_native")),
        "native_currency": (unit.get("_provenance") or {}).get("native_currency"),
        "reporting_currency": "USD",
        "fx_complete": bool(unit.get("fx_complete")),
        "currency_status": unit.get("currency_status"),
        "clicks": int(unit.get("clicks") or 0),
        "impressions": int(unit.get("impressions") or 0),
        "ctr": _ctr(unit.get("clicks"), unit.get("impressions")),
        "cpc_usd": _cpc(spend_usd, unit.get("clicks")),
        # Latest-observed quality (attributes, not window averages).
        "quality_score": unit.get("quality_score"),
        "quality_band": _quality_band(unit.get("quality_score"), th),
        "expected_ctr": unit.get("expected_ctr"),
        "ad_relevance": unit.get("ad_relevance"),
        "landing_page_experience": unit.get("landing_page_experience"),
        # Genuine observation time (pull time), never the activity source_date.
        "quality_observed_date": _iso_str(unit.get("quality_observed_at")),
        # Platform evidence only — never business value. NULL stays NULL
        # (conversion evidence unavailable), never coerced to 0. conversion_status
        # discloses complete / partial / unavailable so a partial is never read as
        # a confirmed zero.
        "platform_conversions": _round2(unit.get("_conv_total")) if unit.get("_conv_total") is not None else None,
        "conversion_status": unit.get("_conv_status"),
        "first_seen": _iso_str(unit.get("first_seen")),
        "last_seen": _iso_str(unit.get("last_seen")),
        "review_signal": _keyword_signal(unit, th, window_end),
    }


# ── Aggregations ─────────────────────────────────────────────────────────────
def _match_type_summary(units: list) -> dict:
    """Compact match-type comparison. Verified spend only; reconciles to the
    complete filtered keyword population. UNKNOWN stays explicit (never Broad)."""
    total_verified = sum(float(u.get("spend_usd") or 0.0)
                         for u in units if u.get("_currency_verified"))
    buckets = {}
    for mt in (*MATCH_TYPES, MATCH_UNKNOWN):
        members = [u for u in units if u.get("match_type_norm") == mt]
        if not members and mt == MATCH_UNKNOWN:
            continue
        verified_usd = sum(float(u.get("spend_usd") or 0.0)
                           for u in members if u.get("_currency_verified"))
        clicks = sum(int(u.get("clicks") or 0) for u in members)
        impressions = sum(int(u.get("impressions") or 0) for u in members)
        # Conversions: sum ONLY the KNOWN totals; unavailable when every member's
        # conversion evidence is unavailable; disclose partial coverage when some
        # members are partial/unavailable. Never treat a NULL day as 0.
        statuses = [u.get("_conv_status") for u in members]
        known_totals = [u.get("_conv_total") for u in members if u.get("_conv_total") is not None]
        has_known = any(s in (CONV_COMPLETE, CONV_PARTIAL) for s in statuses)
        platform_conversions = _round2(sum(known_totals)) if has_known else None
        conversions_partial = has_known and any(s != CONV_COMPLETE for s in statuses)
        any_unverified = any(not u.get("_currency_verified") for u in members)
        buckets[mt] = {
            "match_type": mt,
            "unique_keywords": len(members),
            "verified_spend_usd": _round2(verified_usd),
            "spend_share_pct": (round(verified_usd / total_verified * 100.0, 1)
                                if total_verified > 0 else None),
            "clicks": clicks,
            "ctr": _ctr(clicks, impressions),
            "avg_cpc_usd": _cpc(verified_usd, clicks),
            "platform_conversions": platform_conversions,
            "conversions_partial": conversions_partial,
            "spend_partial": any_unverified,
        }
    return {
        "buckets": [buckets[mt] for mt in (*MATCH_TYPES, MATCH_UNKNOWN) if mt in buckets],
        "total_verified_spend_usd": _round2(total_verified),
        "reconciles_to_population": True,
    }


def _quality_evidence_kpi(units: list) -> dict:
    """Keywords with Quality Evidence — count + pct of unique ACTIVE criteria
    that have a latest observed quality score. Never a fake average."""
    active = [u for u in units if _is_active(u)]
    with_quality = [u for u in active if u.get("quality_score") is not None]
    denom = len(active)
    return {
        "active_criteria": denom,
        "with_quality": len(with_quality),
        "pct": round(len(with_quality) / denom * 100.0, 1) if denom else None,
    }


def _broad_exposure_kpi(units: list, mon: dict) -> dict:
    verified_total = mon.get("verified_usd_spend")
    broad_usd = sum(float(u.get("spend_usd") or 0.0)
                    for u in units
                    if u.get("match_type_norm") == "BROAD" and u.get("_currency_verified"))
    share = (round(broad_usd / verified_total * 100.0, 1)
             if verified_total not in (None, 0) else None)
    return {
        "broad_verified_spend_usd": _round2(broad_usd),
        "broad_share_pct": share,
        "spend_status": mon.get("monetary_completeness_status"),
    }


# ── Filters / sort / validate ────────────────────────────────────────────────
def _validate(match_type, criterion_status, quality_band, signal, sort, sql_state=None):
    if sort not in KEYWORD_SORTS:
        raise KeywordQueryError(f"invalid sort '{sort}'")
    if match_type and match_type.upper() not in (*MATCH_TYPES, MATCH_UNKNOWN):
        raise KeywordQueryError(f"invalid match_type '{match_type}'")
    if quality_band and quality_band not in QUALITY_BANDS:
        raise KeywordQueryError(f"invalid quality_band '{quality_band}'")
    if signal and signal not in REVIEW_SIGNALS:
        raise KeywordQueryError(f"invalid review signal '{signal}'")
    if sql_state and sql_state not in KEYWORD_SQL_STATES:
        raise KeywordQueryError(f"invalid sql_state '{sql_state}'")


def _filter_rows(rows: list, *, q, campaign, match_type, criterion_status,
                 quality_band, signal, min_spend) -> list:
    out = rows
    if q:
        needle = q.strip().lower()
        out = [r for r in out if needle in (
            f"{r.get('keyword') or ''} {r.get('campaign') or ''} "
            f"{r.get('ad_group') or ''}").lower()]
    if campaign:
        out = [r for r in out if r.get("campaign_key") == campaign]
    if match_type:
        mt = match_type.upper()
        out = [r for r in out if r.get("match_type") == mt]
    if criterion_status:
        cs = criterion_status.upper()
        out = [r for r in out if (r.get("criterion_status") or "").upper() == cs]
    if quality_band:
        out = [r for r in out if r.get("quality_band") == quality_band]
    if signal:
        out = [r for r in out if r.get("review_signal") == signal]
    if min_spend is not None:
        out = [r for r in out if (r.get("spend_usd") or 0.0) >= min_spend]
    return out


def _null_last(v):
    return (v is None, -(v if isinstance(v, (int, float)) else 0))


def _sort_rows(rows: list, sort: str) -> list:
    if sort == "keyword":
        return sorted(rows, key=lambda r: (r.get("keyword") or "").lower())
    if sort == "last_seen":
        # Recently active: most recent first, rows with no last_seen ALWAYS last.
        # (has_date sorts before the date so reverse=True keeps nulls at the end.)
        return sorted(rows, key=lambda r: (r.get("last_seen") is not None, r.get("last_seen") or ""),
                      reverse=True)
    keymap = {
        "spend": lambda r: r.get("spend_usd"),
        "clicks": lambda r: r.get("clicks"),
        "cpc": lambda r: r.get("cpc_usd"),
        "ctr": lambda r: r.get("ctr"),
        "quality": lambda r: r.get("quality_score"),
        # Attributed SQLs: highest first; ambiguous/unavailable (null count) last.
        "attributed_sqls": lambda r: r.get("attributed_sqls"),
    }
    getter = keymap.get(sort, keymap["spend"])
    return sorted(rows, key=lambda r: _null_last(getter(r)))


def _filter_sql_state(rows: list, sql_state: str | None) -> list:
    """Filter by Attributed-SQL row state (PR-ADS-146C §7)."""
    if not sql_state or sql_state == "all":
        return rows
    if sql_state == "has_sql":
        return [r for r in rows if r.get("sql_attribution_status") == "attributed"
                and (r.get("attributed_sqls") or 0) > 0]
    if sql_state == "known_zero":
        return [r for r in rows if r.get("sql_attribution_status") == "known_zero"]
    if sql_state == "ambiguous":
        return [r for r in rows if r.get("sql_attribution_status") == "ambiguous"]
    if sql_state == "unavailable":
        return [r for r in rows if r.get("sql_attribution_status")
                in ("unavailable", "mapping_review", "partial_attribution")]
    return rows


def _facets(rows: list) -> dict:
    campaigns = {}
    for r in rows:
        key = r.get("campaign_key")
        if key and key not in campaigns:
            campaigns[key] = {
                "campaign_key": key,
                "campaign_name": r.get("campaign"),
                "mapping_status": r.get("mapping_status"),
            }
    return {
        "campaigns": sorted(campaigns.values(), key=lambda c: (c["campaign_name"] or "").lower()),
        "match_types": list(MATCH_TYPES) + [MATCH_UNKNOWN],
        "quality_bands": list(QUALITY_BANDS),
        "review_signals": list(REVIEW_SIGNALS),
    }


# ── Base / KPIs / audit ──────────────────────────────────────────────────────
def _base(window: str, now: datetime | None):
    start, end, resolved = _window_bounds(window, now)
    base = {
        "window": resolved["key"],
        "window_start": _iso(start),
        "window_end": _iso(end),
        "all_time": resolved["is_all_time"],
        "generated_at": (now or datetime.now(tz=timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "account_timezone": ACCOUNT_TZ,
        "spend_semantics": SPEND_SEMANTICS,
        "reporting_currency": "USD",
        "platform_conversion_disclosure": PLATFORM_CONVERSION_DISCLOSURE,
    }
    return start, end, base


def _kpis(rows: list, units: list, mon: dict, canonical: dict) -> dict:
    clicks = sum(int(r.get("clicks") or 0) for r in rows)
    return {
        "keywords_reported": len(rows),
        "verified_spend_usd": mon.get("verified_usd_spend"),
        "verified_spend_native": mon.get("verified_native_spend"),
        "native_currency": mon.get("native_currency"),
        "reporting_currency": "USD",
        "monetary": mon,
        "monetary_status": mon.get("monetary_completeness_status"),
        "clicks": clicks,
        "broad_match_exposure": _broad_exposure_kpi(units, mon),
        "quality_evidence": _quality_evidence_kpi(units),
        "coverage": _keyword_coverage(mon, canonical),
    }


def _keyword_coverage(mon: dict, canonical: dict) -> dict:
    """Keyword-view reporting coverage. Reuses the shared Search Terms coverage
    math but renames its search-term-specific field/note into keyword-view terms
    so the /api/keyword-evidence contract never leaks cross-endpoint vocabulary."""
    cov = dict(_coverage_block(mon, canonical))
    if "verified_search_term_spend_usd" in cov:
        cov["verified_keyword_spend_usd"] = cov.pop("verified_search_term_spend_usd")
    note = cov.get("note")
    if isinstance(note, str):
        cov["note"] = (note.replace("Verified-row reporting coverage",
                                    "Verified keyword-view reporting coverage")
                       .replace("verified search-term spend", "verified keyword-view spend")
                       .replace("search-term", "keyword-view"))
    return cov


def _durable_coverage(pop: dict) -> tuple:
    src = pop.get("source") or {}
    return _iso_str(src.get("min_source_date")), _iso_str(src.get("max_source_date"))


def _selected_window_coverage(start, end, is_all_time: bool, now) -> dict:
    """Backend selected-window coverage proof (PR-ADS-146A §6). Delegates to the
    keyword-sync service; defensive so a coverage-lookup failure never breaks the
    evidence page (falls back to an unknown, not-fully-synced verdict)."""
    try:
        from services.keyword_sync_service import selected_window_coverage  # noqa: PLC0415
        return selected_window_coverage(start, end, is_all_time=bool(is_all_time), now=now)
    except Exception:  # noqa: BLE001
        return {
            "selected_window_coverage_status": "unknown",
            "selected_window_fully_synced": False,
            "selected_window_coverage_start": None,
            "selected_window_coverage_end": None,
            "successful_coverage_ranges": [],
            "missing_window_ranges": [],
            "latest_successful_sync_date": None,
        }


def _audit_block(base: dict, pop: dict, mon: dict, rows_total: int,
                 filtered_rows: list, pagination_complete: bool) -> dict:
    src = pop.get("source") or {}
    canonical = pop.get("canonical") or {}
    # Reconciliation: KPI/row counts use unique criteria; totals sum durable
    # facts once (guaranteed by the natural key, so window sums never multiply).
    fact_rows = src.get("fact_rows")
    unique_criteria = src.get("unique_criteria")
    recon = {
        "row_grain": "unique_keyword_criterion",
        "durable_fact_rows_in_window": fact_rows,
        "unique_criteria_in_window": unique_criteria,
        "population_rows": len(pop.get("units") or []),
        "rows_equal_unique_criteria": (unique_criteria == len(pop.get("units") or [])),
        "verified_usd_from_source_date_fx": True,
        "match_type_reconciles": True,
        "quality_latest_not_averaged": True,
        "table_drawer_export_same_population": True,
        "status": "ok" if (unique_criteria == len(pop.get("units") or [])) else "variance",
    }
    # Conversion-evidence completeness rollup across the population (§2).
    units = pop.get("units") or []
    conv_counts = {CONV_COMPLETE: 0, CONV_PARTIAL: 0, CONV_UNAVAILABLE: 0}
    for u in units:
        conv_counts[u.get("_conv_status", CONV_UNAVAILABLE)] = \
            conv_counts.get(u.get("_conv_status", CONV_UNAVAILABLE), 0) + 1
    conversion_evidence = {
        "complete_criteria": conv_counts[CONV_COMPLETE],
        "partial_criteria": conv_counts[CONV_PARTIAL],
        "unavailable_criteria": conv_counts[CONV_UNAVAILABLE],
        "status": (CONV_COMPLETE if conv_counts[CONV_PARTIAL] == 0 and conv_counts[CONV_UNAVAILABLE] == 0
                   else CONV_UNAVAILABLE if conv_counts[CONV_COMPLETE] == 0 and conv_counts[CONV_PARTIAL] == 0
                   else CONV_PARTIAL),
        "semantics": ("Platform conversions are counted only where every source-date "
                      "fact reported one; a criterion with any NULL day is partial "
                      "(never a confirmed zero); zero conversions is never waste."),
    }
    return {
        "source_table": "keyword_daily_facts",
        "legacy_source_table": "keywords",
        "date_field": "source_date",
        "natural_key": _kw_natural_key(),
        "window_start": base.get("window_start"),
        "window_end": base.get("window_end"),
        "all_time": base.get("all_time"),
        "account_timezone": ACCOUNT_TZ,
        "currency_semantics": CURRENCY_SEMANTICS,
        "fx_status": ("complete" if canonical.get("fx_complete") else "incomplete_or_unavailable"),
        "monetary_completeness_status": mon.get("monetary_completeness_status"),
        "durable_unit_count": len(units),
        "legacy_unverified_count": mon.get("unverified_currency_units"),
        "campaign_identity_status": ("available" if pop.get("identity_available") else "unavailable"),
        "quality_semantics": QUALITY_SEMANTICS,
        "conversion_evidence": conversion_evidence,
        "keyword_coverage_status": (pop.get("currency_info") or {}).get(
            "monetary_completeness_status"),
        "pagination_complete": pagination_complete,
        "reconciliation_status": recon,
    }


def _kw_natural_key() -> str:
    import db.keyword_repository as kw_repo  # noqa: PLC0415
    return kw_repo.KEYWORD_DAILY_FACTS_NATURAL_KEY


# ── Main builders ────────────────────────────────────────────────────────────
def build_keyword_evidence(window: str, *, page: int = 1,
                           page_size: int = DEFAULT_PAGE_SIZE, q: str | None = None,
                           campaign: str | None = None, match_type: str | None = None,
                           criterion_status: str | None = None,
                           quality_band: str | None = None, signal: str | None = None,
                           min_spend: float | None = None, sort: str = "spend",
                           sql_state: str | None = None,
                           now: datetime | None = None) -> dict:
    """Main Keyword Evidence builder. KPIs are computed over the COMPLETE filtered
    population; only the page slice is serialized into ``rows``."""
    _validate(match_type, criterion_status, quality_band, signal, sort, sql_state)
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))

    start, end, base = _base(window, now)
    pop = _build_population(start, end)
    if not pop.get("available"):
        return unavailable_keyword_response(window, now)

    th = _load_signal_thresholds()
    units = pop["units"]
    all_rows = [_unit_row(u, th, end) for u in units]

    # §4 — attribute qualified SQL contacts to the WHOLE population (window-scoped,
    # filter-independent) then merge the per-criterion verdict onto every row.
    sql_attr = _keyword_sql_attribution(pop, start, end)
    _apply_keyword_sql(all_rows, sql_attr)

    filtered = _filter_rows(all_rows, q=q, campaign=campaign, match_type=match_type,
                            criterion_status=criterion_status, quality_band=quality_band,
                            signal=signal, min_spend=min_spend)
    filtered = _filter_sql_state(filtered, sql_state)
    # Units aligned to the filtered rows (for monetary/match-type math).
    fk = {r["criterion_key"] for r in filtered}
    filtered_units = [u for u in units if u.get("criterion_key") in fk]

    mon = _monetary_summary(filtered_units)
    ordered = _sort_rows(filtered, sort)
    total = len(ordered)
    offset = (page - 1) * page_size
    page_rows = ordered[offset:offset + page_size]

    kpis = _kpis(ordered, filtered_units, mon, pop.get("canonical") or {})
    dcov_start, dcov_end = _durable_coverage(pop)
    # §6 — backend proof of whether THIS window was actually synced, so the
    # frontend never infers "$0 Complete" from a stored-range comparison alone.
    swc = _selected_window_coverage(start, end, base.get("all_time"), now)
    return {
        **base,
        "durable_coverage_start": dcov_start,
        "durable_coverage_end": dcov_end,
        **swc,
        "kpis": kpis,
        "match_type_summary": _match_type_summary(filtered_units),
        "rows": page_rows,
        "pagination": {
            "total_count": total,
            "returned_count": len(page_rows),
            "page": page,
            "page_size": page_size,
            "has_more": offset + page_size < total,
        },
        "facets": _facets(all_rows),
        "filters": {
            "q": q, "campaign": campaign, "match_type": match_type,
            "criterion_status": criterion_status, "quality_band": quality_band,
            "signal": signal, "min_spend": min_spend, "sort": sort,
            "sql_state": sql_state,
        },
        # §10 — the window applies to Google Ads facts by source_date AND to SQL
        # contacts by contact_created_at; disclose both grains, never conflate them.
        "platform_date_field": "source_date",
        "sql_date_field": "contact_created_at",
        "sql_attribution": _sql_attribution_block(sql_attr),
        "audit": _audit_block(base, pop, mon, total, ordered,
                              pagination_complete=(total <= offset + page_size)),
    }


def build_keyword_export(window: str, *, q=None, campaign=None, match_type=None,
                         criterion_status=None, quality_band=None, signal=None,
                         min_spend=None, sort="spend", sql_state=None, now=None) -> dict:
    """Full server-filtered dataset (NO pagination) for CSV export."""
    _validate(match_type, criterion_status, quality_band, signal, sort, sql_state)
    start, end, base = _base(window, now)
    pop = _build_population(start, end)
    if not pop.get("available"):
        return {**base, "db_unavailable": True, "complete": False, "rows": []}
    th = _load_signal_thresholds()
    all_rows = [_unit_row(u, th, end) for u in pop["units"]]
    # SQL attribution for the export (no sql_contact_keys — the main CSV never
    # carries contact ids, PR-ADS-146C §12).
    _apply_keyword_sql(all_rows, _keyword_sql_attribution(pop, start, end))
    filtered = _filter_rows(all_rows, q=q, campaign=campaign, match_type=match_type,
                            criterion_status=criterion_status, quality_band=quality_band,
                            signal=signal, min_spend=min_spend)
    filtered = _filter_sql_state(filtered, sql_state)
    ordered = _sort_rows(filtered, sort)
    return {**base, "db_unavailable": False, "complete": True, "rows": ordered}


def build_keyword_drawer(window: str, criterion_key: str, *, now: datetime | None = None) -> dict:
    """Single keyword-criterion drawer, scoped strictly by immutable identity —
    never borrows another criterion's rows or a same-named campaign's evidence."""
    start, end, base = _base(window, now)
    pop = _build_population(start, end)
    if not pop.get("available"):
        return unavailable_keyword_drawer_response(window, now)

    th = _load_signal_thresholds()
    match = next((u for u in pop["units"] if u.get("criterion_key") == criterion_key), None)
    if match is None:
        return {**base, "db_unavailable": False, "found": False,
                "criterion_key": criterion_key, "keyword": None}

    row = _unit_row(match, th, end)
    # Daily series scoped by immutable identity (id-first, never borrowed).
    import db.keyword_repository as kw_repo  # noqa: PLC0415
    daily = kw_repo.fetch_keyword_daily_for_criterion(
        start, end,
        criterion_id=match.get("criterion_id"),
        campaign_id=match.get("campaign_id"),
        ad_group_id=match.get("ad_group_id"),
        customer_id=match.get("customer_id"))
    daily_usd = _convert_daily_series(daily.get("rows") or [], start, end)

    # §7 — HubSpot SQL attribution for this criterion + its supporting contacts.
    sql_attr = _keyword_sql_attribution(pop, start, end)
    _apply_keyword_sql([row], sql_attr, drawer=True)
    sql_block = _keyword_drawer_sql_block(row, sql_attr)

    return {
        **base,
        "db_unavailable": False,
        "found": True,
        "keyword": row,
        "identity": {
            "keyword": row["keyword"], "match_type": row["match_type"],
            "criterion_status": row["criterion_status"], "criterion_id": row["criterion_id"],
            "campaign": row["campaign"], "campaign_id": row["campaign_id"],
            "campaign_key": row["campaign_key"], "ad_group": row["ad_group"],
            "ad_group_id": row["ad_group_id"], "mapping_status": row["mapping_status"],
        },
        "activity": {
            "spend_usd": row["spend_usd"], "spend_native": row["spend_native"],
            "native_currency": row["native_currency"], "currency_status": row["currency_status"],
            "clicks": row["clicks"], "impressions": row["impressions"],
            "ctr": row["ctr"], "cpc_usd": row["cpc_usd"],
            "platform_conversions": row["platform_conversions"],
            "platform_conversion_disclosure": PLATFORM_CONVERSION_DISCLOSURE,
            "first_seen": row["first_seen"], "last_seen": row["last_seen"],
        },
        "latest_quality": {
            "quality_score": row["quality_score"], "expected_ctr": row["expected_ctr"],
            "ad_relevance": row["ad_relevance"],
            "landing_page_experience": row["landing_page_experience"],
            "observed_date": row["quality_observed_date"],
            "semantics": QUALITY_SEMANTICS,
        },
        "review_signal": row["review_signal"],
        "daily": daily_usd,
        "sql_attribution": sql_block,
        "search_terms_link": {
            "campaign_key": row["campaign_key"], "ad_group_id": row["ad_group_id"],
            "keyword": row["keyword"], "window": base["window"],
        },
    }


def _keyword_drawer_sql_block(row: dict, attr: dict) -> dict:
    """HubSpot SQL attribution block for the keyword drawer — status, coverage,
    source, ambiguity, and the supporting deduplicated contacts (no emails)."""
    from services.platform_sql_attribution_service import (  # noqa: PLC0415
        contact_details_for_keys,
    )
    contacts = contact_details_for_keys(attr.get("contacts") or [],
                                        row.get("sql_contact_keys") or [])
    cov = attr.get("coverage") or {}
    return {
        "attributed_sqls": row.get("attributed_sqls"),
        "sql_attribution_status": row.get("sql_attribution_status"),
        "sql_attribution_source": row.get("sql_attribution_source"),
        "sql_attribution_coverage_pct": cov.get("coverage_pct"),
        "sql_candidate_count": row.get("sql_candidate_count"),
        "sql_ambiguity_reason": row.get("sql_ambiguity_reason"),
        "available": bool(attr.get("available")),
        "contacts": contacts,
        "contact_count": len(contacts),
    }


# ── DB-unavailable shapes ────────────────────────────────────────────────────
def _safe_base(window, now):
    try:
        _s, _e, base = _base(window, now)
        return base
    except EvidenceWindowError:
        raise
    except Exception:  # noqa: BLE001
        return {"window": window, "reporting_currency": "USD"}


def unavailable_keyword_response(window: str, now: datetime | None = None) -> dict:
    base = _safe_base(window, now)
    mon = _monetary_summary([]) | {"monetary_completeness_status": MON_UNAVAILABLE}
    return {
        **base, "db_unavailable": True,
        "durable_coverage_start": None, "durable_coverage_end": None,
        "kpis": {
            "keywords_reported": None, "verified_spend_usd": None,
            "verified_spend_native": None, "native_currency": None,
            "reporting_currency": "USD", "monetary": mon, "monetary_status": MON_UNAVAILABLE,
            "clicks": None,
            "broad_match_exposure": {"broad_verified_spend_usd": None,
                                     "broad_share_pct": None, "spend_status": MON_UNAVAILABLE},
            "quality_evidence": {"active_criteria": None, "with_quality": None, "pct": None},
            "coverage": {"status": "unavailable", "scope": "unavailable"},
        },
        "match_type_summary": {"buckets": [], "total_verified_spend_usd": None,
                               "reconciles_to_population": True},
        "rows": [],
        "pagination": {"total_count": 0, "returned_count": 0, "page": 1,
                       "page_size": DEFAULT_PAGE_SIZE, "has_more": False},
        "facets": {"campaigns": [], "match_types": list(MATCH_TYPES) + [MATCH_UNKNOWN],
                   "quality_bands": list(QUALITY_BANDS), "review_signals": list(REVIEW_SIGNALS)},
        "filters": {}, "audit": {"source_table": "keyword_daily_facts",
                                 "reconciliation_status": {"status": "db_unavailable"}},
    }


def unavailable_keyword_drawer_response(window: str, now: datetime | None = None) -> dict:
    base = _safe_base(window, now)
    return {**base, "db_unavailable": True, "found": False, "keyword": None}
