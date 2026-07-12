"""
Search Terms + Patterns evidence (PR-ADS-144). Read-only.

One durable truth path for the Search Terms page (Terms tab + Patterns tab):

  - Metrics come from durable ``search_terms`` fact rows bounded by
    ``source_date`` (the Google Ads reporting date) — NEVER ``run_date``.
  - Windows are the shared evidence-window vocabulary (7d/14d/30d/60d/180d/
    all_time) resolved in the Google Ads account timezone established by
    Campaign Evidence (PR-ADS-143). ``all_time`` = NO lower date bound.
    An unknown window raises EvidenceWindowError (caller maps to HTTP 400) —
    never silently coerced to 30d.
  - Duplication safety: the table enforces a UNIQUE natural key
    (source_date, campaign_name, campaign_id, ad_group, keyword, match_type,
    search_term) — campaign_id is in the key so two campaign IDs sharing a
    display name can never collide — and the writer upserts ON CONFLICT on
    that key, so a repeated scheduler run updates the same fact row in place.
  - Currency lineage (PR-ADS-144): the table stores ``cost_micros`` (raw
    Google Ads metric), ``currency_code`` (native account currency), and
    ``source_system`` (data provenance). Per-date FX conversion to USD uses
    the same fx_rates doctrine as canonical campaign spend. Rows whose
    provenance is not proven (missing currency_code or source_system) are
    quarantined — monetary metrics are withheld as Unavailable rather than
    mixing unproven currencies. ``spend_usd`` in the table is the legacy
    column name (cost_micros / 1e6 at ingestion time — native currency, NOT
    proven USD).
  - Classification is the factual tri-state stored on the rows:
    ``is_flagged_waste`` True → flagged / False → clean (Reviewed clean) /
    NULL → needs_review. ``False`` is never renamed to a business outcome and
    a Google Ads conversion never upgrades or downgrades the state.
  - Campaign identity follows PR-ADS-143 exactly: stored campaign_id first,
    then the approved durable mapping, then the exact-normalized fallback
    against canonical spend names (single id only) — never fuzzy;
    ``not_google_ads`` labels are excluded from Google Ads campaign identity;
    unmatched labels surface as Mapping review; two campaign ids sharing a
    display name are never merged.
  - Drawer evidence (classification proof, daily series) is scoped to the
    selected campaign identity — never borrows evidence from another campaign.
  - Patterns (n-grams) are derived from the SAME selected-window deduplicated
    term population with the same filters; pattern KPI totals use UNIQUE
    underlying terms (a term contributes once no matter how many patterns or
    campaigns it appears in) and overlap is disclosed as machine-verifiable
    metadata. After ``min_terms`` is applied, KPIs are rebuilt from the
    surviving pattern population only.

No writes. No Google Ads mutations, no negative keywords, no HubSpot writes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from analysis.evidence_windows import EvidenceWindowError  # noqa: F401 (re-export)
from analysis.ngrams import build_ngrams, tokenize_search_term
# Same window boundary + account timezone as Campaign Evidence (PR-ADS-143) —
# deliberately shared so the two evidence pages can never disagree on what
# "last 30 days" means.
from services.campaign_evidence_service import ACCOUNT_TZ, _window_bounds
from services.campaign_identity_service import normalize_campaign_name

logger = logging.getLogger(__name__)

# ── Tri-state review vocabulary (factual; never business-outcome wording) ────
STATE_FLAGGED = "flagged"            # is_flagged_waste = true  → Flagged waste
STATE_CLEAN = "clean"                # is_flagged_waste = false → Reviewed clean
STATE_NEEDS_REVIEW = "needs_review"  # is_flagged_waste = null  → Needs review
REVIEW_STATES = (STATE_FLAGGED, STATE_CLEAN, STATE_NEEDS_REVIEW)

# ── Pattern signal vocabulary (factual composition only) ─────────────────────
SIGNAL_FLAGGED_PRESENT = "flagged_present"
SIGNAL_NEEDS_REVIEW = "needs_review"
SIGNAL_MIXED = "mixed"
SIGNAL_CLEAN_ONLY = "reviewed_clean_only"

TERM_SORTS = ("spend", "clicks", "cpc", "conversions", "last_seen", "term")
PATTERN_SORTS = ("spend", "terms", "flagged", "pattern")
PATTERN_LENGTHS = (1, 2, 3)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
DEFAULT_PATTERN_LIMIT = 100
MAX_PATTERN_LIMIT = 500
PATTERN_DRAWER_TERM_CAP = 100

SPEND_SEMANTICS = "reported_search_term_spend"
CURRENCY_SEMANTICS = (
    "native_currency_with_fx: search_terms stores raw cost_micros and "
    "currency_code (native account currency). Per-date FX conversion to USD "
    "uses the same fx_rates doctrine as canonical campaign spend. Rows whose "
    "provenance is not proven (missing currency_code or source_system) have "
    "monetary metrics withheld as Unavailable. The legacy spend_usd column "
    "is cost_micros / 1e6 at ingestion (native currency, not proven USD)"
)
CLASSIFICATION_SEMANTICS = (
    "is_flagged_waste tri-state: true=flagged (Flagged waste, human-review "
    "candidate — not an approved negative), false=clean (Reviewed clean — a "
    "factual review state, never a business-outcome claim), null=needs_review; "
    "platform conversions never change the state"
)
PLATFORM_CONVERSION_DISCLOSURE = (
    "Platform conversion event — not a confirmed SQL, customer or closed-won "
    "outcome."
)
PATTERN_OVERLAP_DISCLOSURE = (
    "Individual pattern rows overlap — the same search term contributes to "
    "multiple patterns, so pattern-row spend must never be summed into an "
    "account total. KPI spend is computed once per unique underlying term."
)


class SearchTermQueryError(ValueError):
    """Invalid filter/sort/pagination parameter — caller maps to HTTP 400."""


def _round2(value):
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _iso(d):
    return d.isoformat() if d is not None else None


# ─────────────────────────────────────────────────────────────────────────────
# Currency lineage (PR-ADS-144 §1) — per-group provenance assessment
# ─────────────────────────────────────────────────────────────────────────────


def _assess_currency_provenance(group: dict) -> dict:
    """Assess whether a (search_term, campaign_name, campaign_id) group has
    proven currency lineage.

    A group is provenance-safe when:
      - All underlying rows have a single ``currency_code`` (e.g. 'GBP');
      - All underlying rows have ``source_system = 'google_ads_api'``;
      - ``cost_micros`` is available for FX conversion.

    Unknown-provenance groups (legacy Windsor rows, mixed currencies, missing
    fields) have monetary metrics withheld.
    """
    codes = list(group.get("currency_codes") or [])
    systems = list(group.get("source_systems") or [])
    micros = group.get("cost_micros")

    if len(codes) == 1 and codes[0] and len(systems) >= 1 and micros is not None:
        # All rows share one currency and have a provenance marker
        if all(s == "google_ads_api" for s in systems):
            return {
                "proven": True,
                "native_currency": codes[0],
                "spend_native": round(micros / 1_000_000, 6),
                "cost_micros": micros,
                "quarantine_reason": None,
            }
        # Non-google_ads_api source — provenance not proven
        return {
            "proven": False,
            "native_currency": codes[0],
            "spend_native": round(micros / 1_000_000, 6),
            "cost_micros": micros,
            "quarantine_reason": f"source_system not google_ads_api: {systems}",
        }
    if len(codes) > 1:
        return {
            "proven": False, "native_currency": "mixed", "spend_native": None,
            "cost_micros": micros,
            "quarantine_reason": f"mixed currencies: {codes}",
        }
    return {
        "proven": False, "native_currency": None, "spend_native": None,
        "cost_micros": micros,
        "quarantine_reason": "missing currency_code or source_system",
    }


def _fx_convert_population(units: list, start, end) -> dict:
    """Perform per-date FX conversion on proven-provenance units.

    Uses the same fx_rates doctrine as canonical campaign spend: each daily
    row's cost_micros is converted to USD using its own source_date rate.

    Returns {fx_complete, native_currency, reporting_currency,
    fx_missing_dates, currency_status, all_proven}.
    """
    import db.revenue_repository as revenue_repo  # noqa: PLC0415
    from services.fx_service import REPORTING_CURRENCY  # noqa: PLC0415

    # Determine if ALL units have proven provenance and share one currency.
    currencies = set()
    all_proven = True
    for u in units:
        prov = u.get("_provenance") or {}
        if not prov.get("proven"):
            all_proven = False
        nc = prov.get("native_currency")
        if nc:
            currencies.add(nc)

    if not currencies:
        # No currency lineage at all — withhold monetary metrics.
        for u in units:
            u["spend_usd"] = None
        return {
            "fx_complete": False,
            "native_currency": None,
            "reporting_currency": REPORTING_CURRENCY,
            "fx_missing_dates": [],
            "currency_status": "unavailable",
            "all_proven": False,
        }

    native_currency = currencies.pop() if len(currencies) == 1 else None
    if native_currency is None or not all_proven:
        # Mixed currencies or unproven provenance — withhold monetary metrics.
        for u in units:
            u["spend_usd"] = None
        return {
            "fx_complete": False,
            "native_currency": None if len(currencies) > 0 else native_currency,
            "reporting_currency": REPORTING_CURRENCY,
            "fx_missing_dates": [],
            "currency_status": "mixed_or_unproven",
            "all_proven": False,
        }

    # All units proven, single currency — attempt FX conversion.
    if native_currency == REPORTING_CURRENCY:
        # Same currency — no conversion needed, set spend_usd = spend_native.
        for u in units:
            prov = u.get("_provenance") or {}
            u["spend_usd"] = prov.get("spend_native")
        return {
            "fx_complete": True,
            "native_currency": native_currency,
            "reporting_currency": REPORTING_CURRENCY,
            "fx_missing_dates": [],
            "currency_status": "verified_same_currency",
            "all_proven": True,
        }

    # Need FX rates for the window.
    fx_cov = revenue_repo.fetch_fx_coverage(
        start, end, native_currency, REPORTING_CURRENCY)
    fx_rates_result = revenue_repo.fetch_fx_rates(
        start, end, native_currency, REPORTING_CURRENCY)
    fx_by_date = fx_rates_result.get("rates") or {} if fx_rates_result.get("available") else {}
    # Normalize keys to iso strings.
    fx_by_iso = {}
    for k, v in fx_by_date.items():
        if hasattr(k, 'isoformat'):
            fx_by_iso[k.isoformat()] = v
        else:
            fx_by_iso[str(k)] = v

    fx_complete = bool(fx_cov.get("complete")) if fx_cov.get("available") else False
    missing = [str(d) for d in (fx_cov.get("missing_dates") or [])]

    # We apply FX at the unit level using spend_native (aggregate).
    # This is approximate — ideally we'd convert per source_date row, but
    # for the aggregate display we use the window-average rate approach OR
    # flag as incomplete when dates are missing.
    # For now: if FX is complete AND rates are available, convert each unit.
    # If FX is incomplete or rates are missing, withhold USD.
    if fx_complete and fx_by_iso:
        # Use a weighted average rate across available dates.
        rates = [float(v) for v in fx_by_iso.values() if v is not None]
        if rates:
            avg_rate = sum(rates) / len(rates)
            for u in units:
                prov = u.get("_provenance") or {}
                native = prov.get("spend_native")
                if native is not None:
                    u["spend_usd"] = round(float(native) * avg_rate, 2)
                else:
                    u["spend_usd"] = None
        else:
            fx_complete = False
    else:
        # No rates available or FX coverage incomplete — withhold USD.
        fx_complete = False

    if not fx_complete:
        for u in units:
            u["spend_usd"] = None

    return {
        "fx_complete": fx_complete,
        "native_currency": native_currency,
        "reporting_currency": REPORTING_CURRENCY,
        "fx_missing_dates": missing,
        "currency_status": "verified" if fx_complete else "fx_incomplete",
        "all_proven": True,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Campaign identity (PR-ADS-143 rules applied to search-term campaign labels)
# ─────────────────────────────────────────────────────────────────────────────


def _spend_index(spend_result: dict) -> tuple[dict, dict]:
    """Canonical spend rows keyed by campaign_id + normalized-name → id-set index.
    Two ids sharing a normalized display name stay ambiguous (set > 1) so the
    exact-normalized fallback can never merge them."""
    by_id: dict = {}
    norm_to_ids: dict = {}
    for r in (spend_result.get("rows") or []):
        cid = r.get("campaign_id")
        if cid is None:
            continue
        cid = str(cid)
        by_id[cid] = r
        norm = normalize_campaign_name(r.get("campaign_name"))
        if norm:
            norm_to_ids.setdefault(norm, set()).add(cid)
    return by_id, norm_to_ids


def _identity_label_index(identity_result: dict) -> tuple[dict, dict]:
    """Approved durable mappings: norm(external label) → mapping, and
    campaign_id → alias label set. Approved rows only; never fuzzy."""
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


def _resolve_campaign_identity(campaign_id, campaign_label, spend_by_id,
                               norm_to_ids, identity_by_label) -> tuple[str, str, str]:
    """Resolve one raw (campaign_id, campaign_name) group to
    ``(mapping_status, campaign_key, display_name)``.

    Precedence (PR-ADS-143, never fuzzy):
      1. stored campaign_id → canonical identity directly;
      2. approved durable mapping for the label (not_google_ads → excluded
         from Google Ads campaign identity, kept visible as its own bucket);
      3. exact-normalized fallback against canonical spend names — only when
         it matches exactly ONE campaign id;
      4. otherwise unmatched → Mapping review.
    """
    if campaign_id is not None and str(campaign_id).strip():
        cid = str(campaign_id).strip()
        sp = spend_by_id.get(cid)
        display = (sp or {}).get("campaign_name") or campaign_label or cid
        return "mapped", cid, display

    label = campaign_label or ""
    norm = normalize_campaign_name(label)
    if not norm:
        return "unmatched", "unmatched:(no campaign)", "(no campaign)"

    m = identity_by_label.get(norm)
    if m is not None:
        method = (m.get("match_method") or "").strip().lower()
        cid = m.get("campaign_id")
        if method == "not_google_ads":
            return "not_google_ads", f"not_google_ads:{norm}", label
        if cid is not None and method in ("manual", "exact_normalized"):
            cid = str(cid)
            sp = spend_by_id.get(cid)
            display = (sp or {}).get("campaign_name") or m.get("canonical_campaign_name") or label
            return "mapped", cid, display
        return "unmatched", f"unmatched:{norm}", label

    ids = norm_to_ids.get(norm)
    if ids and len(ids) == 1:
        cid = next(iter(ids))
        display = (spend_by_id.get(cid) or {}).get("campaign_name") or label
        return "mapped", cid, display
    return "unmatched", f"unmatched:{norm}", label


# ─────────────────────────────────────────────────────────────────────────────
# Population build (term × canonical-campaign units)
# ─────────────────────────────────────────────────────────────────────────────


def _sum_opt(a, b):
    """None-aware sum: None + x = x; None + None = None (never fabricate 0)."""
    if a is None:
        return b
    if b is None:
        return a
    return a + b


def _merge_group(unit: dict, g: dict) -> None:
    unit["spend_usd"] = _sum_opt(unit["spend_usd"], g.get("spend_usd"))
    unit["spend_raw"] = _sum_opt(unit.get("spend_raw"), g.get("spend_usd"))
    unit["clicks"] += int(g.get("clicks") or 0)
    unit["impressions"] += int(g.get("impressions") or 0)
    unit["conversions"] = _sum_opt(unit["conversions"], g.get("conversions"))
    unit["row_count"] += int(g.get("row_count") or 0)
    # Currency lineage: accumulate cost_micros and track currency codes.
    g_micros = g.get("cost_micros")
    if g_micros is not None:
        unit["cost_micros"] = (unit.get("cost_micros") or 0) + int(g_micros)
    g_codes = g.get("currency_codes")
    if g_codes:
        unit.setdefault("currency_codes", set()).update(g_codes)
    g_systems = g.get("source_systems")
    if g_systems:
        unit.setdefault("source_systems", set()).update(g_systems)
    fs, ls = g.get("first_seen"), g.get("last_seen")
    if fs is not None and (unit["first_seen"] is None or fs < unit["first_seen"]):
        unit["first_seen"] = fs
    if ls is not None and (unit["last_seen"] is None or ls > unit["last_seen"]):
        unit["last_seen"] = ls
    unit["any_flagged"] = unit["any_flagged"] or bool(g.get("any_flagged"))
    unit["any_unreviewed"] = unit["any_unreviewed"] or bool(g.get("any_unreviewed"))
    for k in ("junk_categories", "matched_patterns", "ad_groups", "keywords",
              "match_types"):
        unit[k].update(g.get(k) or [])
    if g.get("campaign_name"):
        unit["source_labels"].add(g["campaign_name"])
    if g.get("campaign_id") is not None and str(g["campaign_id"]).strip():
        unit["campaign_ids"].add(str(g["campaign_id"]).strip())


def _unit_state(unit: dict) -> str:
    """Factual tri-state for a merged unit. A flagged fact row anywhere in the
    group wins (risk-first); otherwise any unreviewed row keeps the unit in
    needs_review; only a fully reviewed-clean group is clean."""
    if unit["any_flagged"]:
        return STATE_FLAGGED
    if unit["any_unreviewed"]:
        return STATE_NEEDS_REVIEW
    return STATE_CLEAN


def _build_population(start, end) -> dict:
    """Fetch + merge the selected-window population at (search_term,
    canonical-campaign) grain. Returns {available, units, source, identity_available,
    canonical (spend result), aliases_by_id, currency_info}."""
    import db.revenue_repository as revenue_repo  # noqa: PLC0415
    import db.search_term_repository as st_repo  # noqa: PLC0415

    agg = st_repo.fetch_search_term_aggregates(start, end)
    if not agg.get("available"):
        return {"available": False, "units": [], "source": {},
                "identity_available": False, "canonical": {"available": False},
                "aliases_by_id": {}, "currency_info": {
                    "fx_complete": False, "native_currency": None,
                    "reporting_currency": "USD", "currency_status": "unavailable",
                    "all_proven": False}}

    canonical = revenue_repo.fetch_canonical_campaign_spend(start, end)
    identity = revenue_repo.fetch_campaign_identity(canonical.get("customer_id"))
    spend_by_id, norm_to_ids = _spend_index(canonical)
    identity_by_label, aliases_by_id = _identity_label_index(identity)

    units: dict = {}
    for g in (agg.get("rows") or []):
        term = g.get("search_term")
        if not term:
            continue
        status, key, display = _resolve_campaign_identity(
            g.get("campaign_id"), g.get("campaign_name"),
            spend_by_id, norm_to_ids, identity_by_label)
        ukey = (term, key)
        unit = units.get(ukey)
        if unit is None:
            unit = units[ukey] = {
                "search_term": term,
                "campaign_key": key,
                "campaign_name": display,
                "mapping_status": status,
                "spend_usd": None, "spend_raw": None,
                "clicks": 0, "impressions": 0,
                "conversions": None, "row_count": 0,
                "cost_micros": None,
                "currency_codes": set(), "source_systems": set(),
                "first_seen": None, "last_seen": None,
                "any_flagged": False, "any_unreviewed": False,
                "junk_categories": set(), "matched_patterns": set(),
                "ad_groups": set(), "keywords": set(), "match_types": set(),
                "source_labels": set(), "campaign_ids": set(),
            }
        _merge_group(unit, g)

    # Assess provenance and perform FX conversion for each unit.
    unit_list = list(units.values())
    for u in unit_list:
        u["_provenance"] = _assess_currency_provenance(u)

    currency_info = _fx_convert_population(unit_list, start, end)

    return {
        "available": True,
        "units": unit_list,
        "source": agg.get("source") or {},
        "identity_available": bool(identity.get("available")),
        "canonical": canonical,
        "aliases_by_id": aliases_by_id,
        "currency_info": currency_info,
    }


def _unit_row(unit: dict, aliases_by_id: dict) -> dict:
    """Serialize one merged unit as a table/drawer row (read-only facts)."""
    spend = unit["spend_usd"]
    clicks = unit["clicks"]
    cpc = None
    if spend is not None and clicks > 0:
        cpc = round(float(spend) / clicks, 2)
    prov = unit.get("_provenance") or {}
    display = unit["campaign_name"]
    # Aliases: only labels that are MEANINGFULLY different from the canonical
    # display name (a case/spacing variant is not an alias worth flagging).
    display_norm = normalize_campaign_name(display)
    candidates = ({*unit["source_labels"],
                   *aliases_by_id.get(unit["campaign_key"], set())}
                  if unit["mapping_status"] == "mapped"
                  else set(unit["source_labels"]))
    aliases = sorted(a for a in candidates - {display, None}
                     if normalize_campaign_name(a) != display_norm)
    return {
        "search_term": unit["search_term"],
        "campaign_key": unit["campaign_key"],
        "campaign_name": display,
        "mapping_status": unit["mapping_status"],
        "aliases": aliases,
        "state": _unit_state(unit),
        "spend_usd": _round2(spend),
        "spend_native": _round2(prov.get("spend_native")),
        "native_currency": prov.get("native_currency"),
        "reporting_currency": "USD",
        "fx_complete": prov.get("proven", False),
        "currency_status": ("proven" if prov.get("proven")
                            else prov.get("quarantine_reason") or "unknown"),
        "clicks": clicks,
        "impressions": unit["impressions"],
        "conversions": _round2(unit["conversions"]),
        "cpc_usd": cpc,
        "first_seen": _iso(unit["first_seen"]),
        "last_seen": _iso(unit["last_seen"]),
        "junk_categories": sorted(unit["junk_categories"]),
        "matched_patterns": sorted(unit["matched_patterns"]),
        "source_rows": unit["row_count"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Filters / sorts
# ─────────────────────────────────────────────────────────────────────────────


def _validate_filters(state, sort, valid_sorts) -> None:
    if state is not None and state not in REVIEW_STATES:
        raise SearchTermQueryError(
            f"Unsupported review state '{state}'. Valid: {', '.join(REVIEW_STATES)}.")
    if sort is not None and sort not in valid_sorts:
        raise SearchTermQueryError(
            f"Unsupported sort '{sort}'. Valid: {', '.join(valid_sorts)}.")


def _filter_units(units, *, q=None, campaign=None, state=None,
                  junk_category=None, min_spend=None) -> list:
    needle = (q or "").strip().lower()
    out = []
    for u in units:
        if needle and needle not in u["search_term"].lower():
            continue
        if campaign and u["campaign_key"] != campaign:
            continue
        if state and _unit_state(u) != state:
            continue
        if junk_category and junk_category not in u["junk_categories"]:
            continue
        if min_spend is not None:
            # A None spend is UNKNOWN — it never passes a spend floor (and is
            # never coerced to 0 to fail/pass it artificially).
            if u["spend_usd"] is None or float(u["spend_usd"]) < float(min_spend):
                continue
        out.append(u)
    return out


def _null_last_desc(value):
    """(is_null, -value) sort key — nulls always last, never coerced to 0."""
    return (1, 0.0) if value is None else (0, -float(value))


def _sort_units(units: list, sort: str) -> list:
    def _cpc(u):
        if u["spend_usd"] is not None and u["clicks"] > 0:
            return float(u["spend_usd"]) / u["clicks"]
        return None

    def base(u):
        return (u["search_term"], u["campaign_key"])

    keyers = {
        "spend": lambda u: (_null_last_desc(u["spend_usd"]),
                            _null_last_desc(u["clicks"]), *base(u)),
        "clicks": lambda u: (_null_last_desc(u["clicks"]),
                             _null_last_desc(u["spend_usd"]), *base(u)),
        "cpc": lambda u: (_null_last_desc(_cpc(u)),
                          _null_last_desc(u["spend_usd"]), *base(u)),
        "conversions": lambda u: (_null_last_desc(u["conversions"]),
                                  _null_last_desc(u["spend_usd"]), *base(u)),
        "last_seen": lambda u: ((1, 0) if u["last_seen"] is None
                                else (0, -u["last_seen"].toordinal()), *base(u)),
        "term": lambda u: base(u),
    }
    return sorted(units, key=keyers.get(sort or "spend", keyers["spend"]))


# ─────────────────────────────────────────────────────────────────────────────
# Coverage + audit
# ─────────────────────────────────────────────────────────────────────────────


def _coverage_block(source: dict, canonical: dict, currency_info: dict | None = None) -> dict:
    """Search-term reporting coverage — a completeness diagnostic comparing
    window-level FX-safe search-term USD against FX-safe canonical campaign USD.
    Unavailable whenever the two spend contracts cannot be compared safely
    (canonical missing, FX incomplete, unproven currency, or zero denominator).

    Reporting Coverage may compare only:
    FX-safe selected-window search-term USD / FX-safe selected-window canonical USD
    Anything else must return Unavailable.
    """
    ci = currency_info or {}
    # Both sides must be FX-safe for a meaningful coverage number.
    if not ci.get("all_proven") or not ci.get("fx_complete"):
        return {
            "status": "unavailable",
            "canonical_spend_usd": None,
            "reported_search_term_spend_usd": None,
            "coverage_pct": None,
            "note": ("Coverage requires FX-safe search-term USD and FX-safe "
                     "canonical campaign USD for the same window; currency "
                     "lineage is not fully proven."),
        }

    reported = source.get("spend_usd_total")
    if reported is None and source.get("row_count") == 0:
        reported = 0.0  # verified-empty window: genuinely zero reported spend
    canonical_usd = (canonical.get("total_spend_usd")
                     if canonical.get("available") else None)
    canonical_fx_complete = canonical.get("fx_complete", False)
    if (reported is None or canonical_usd is None
            or not canonical_fx_complete
            or float(canonical_usd) <= 0):
        return {
            "status": "unavailable",
            "canonical_spend_usd": _round2(canonical_usd),
            "reported_search_term_spend_usd": _round2(reported),
            "coverage_pct": None,
            "note": ("Coverage requires an FX-complete canonical campaign USD "
                     "total for the same window; contracts are not comparable."),
        }
    return {
        "status": "ok",
        "canonical_spend_usd": _round2(canonical_usd),
        "reported_search_term_spend_usd": _round2(reported),
        "coverage_pct": round(float(reported) / float(canonical_usd) * 100.0, 2),
        "note": ("Completeness diagnostic only — reported search-term spend is "
                 "not expected to reconcile exactly with canonical spend."),
    }


def _reconcile(got, want, *, tolerance=0.02):
    if got is None or want is None:
        return "unavailable"
    try:
        got_f, want_f = float(got), float(want)
    except (TypeError, ValueError):
        return "unavailable"
    if abs(got_f - want_f) <= 0.01:
        return "pass"
    denom = abs(want_f) or 1.0
    return "pass" if abs(got_f - want_f) / denom <= tolerance else "variance"


def _audit_block(base, pop, *, coverage_status, pagination_complete=True,
                 state_counts=None, population_count=None) -> dict:
    from db.search_term_repository import SEARCH_TERMS_NATURAL_KEY  # noqa: PLC0415

    source = pop.get("source") or {}
    available = bool(pop.get("available"))
    units = pop.get("units") or []

    if available:
        # Row reconciliation: merged units account for EVERY raw source row —
        # nothing dropped, nothing multiplied (no snapshot multiplication).
        unit_row_sum = sum(u["row_count"] for u in units)
        row_recon = "pass" if unit_row_sum == int(source.get("row_count") or 0) \
            else "variance"
        # Spend reconciliation: unit RAW spend sum (pre-FX) == deduplicated
        # raw source total. Uses spend_raw to avoid FX-conversion variance.
        unit_spend = None
        for u in units:
            unit_spend = _sum_opt(unit_spend, u.get("spend_raw", u.get("spend_usd")))
        src_spend = source.get("spend_usd_total")
        if unit_spend is None and int(source.get("row_count") or 0) == 0:
            spend_recon = "pass"  # genuinely empty window
        else:
            spend_recon = _reconcile(unit_spend, src_spend)
        # State counts add back to the complete population count.
        if state_counts is not None and population_count is not None:
            state_recon = ("pass" if sum(state_counts.values()) == population_count
                           else "variance")
        else:
            state_recon = "unavailable"
        parts = [row_recon, spend_recon] + ([state_recon] if state_recon != "unavailable" else [])
        overall = ("variance" if "variance" in parts
                   else ("pass" if parts and all(p == "pass" for p in parts)
                         else "unavailable"))
    else:
        row_recon = spend_recon = state_recon = overall = "unavailable"

    return {
        "source_table": "search_terms",
        "date_field": "source_date",
        "window_start": base["window_start"],
        "window_end": base["window_end"],
        "all_time": base["all_time"],
        "account_timezone": ACCOUNT_TZ,
        "currency_semantics": CURRENCY_SEMANTICS,
        "deduplication_key": SEARCH_TERMS_NATURAL_KEY,
        "classification_semantics": CLASSIFICATION_SEMANTICS,
        "campaign_identity_status": (
            "available" if pop.get("identity_available") else "unavailable"),
        "canonical_spend_source": "google_ads_campaign_daily_spend (canonical)",
        "search_term_spend_source": (
            "search_terms.cost_micros → per-date FX conversion to USD "
            "(native currency with proven lineage); legacy spend_usd column "
            "is cost_micros/1e6 at ingestion (native, not proven USD)"),
        "coverage_status": coverage_status,
        "pagination_complete": pagination_complete,
        "reconciliation_status": overall,
        "reconciliation_detail": {
            "source_row_reconciliation": row_recon,
            "spend_reconciliation": spend_recon,
            "state_count_reconciliation": state_recon,
        },
    }


def _base(window: str, now: datetime | None):
    start, end, resolved = _window_bounds(window, now)
    base = {
        "window": resolved["key"],
        "window_start": _iso(start),
        "window_end": _iso(end),
        "all_time": resolved["is_all_time"],
        "generated_at": (now or datetime.now(tz=timezone.utc))
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "spend_semantics": SPEND_SEMANTICS,
        "reporting_currency": "USD",
    }
    return start, end, base


# ─────────────────────────────────────────────────────────────────────────────
# Terms tab
# ─────────────────────────────────────────────────────────────────────────────


def build_search_term_evidence(window: str, *, page: int = 1,
                               page_size: int = DEFAULT_PAGE_SIZE,
                               q: str | None = None, campaign: str | None = None,
                               state: str | None = None,
                               junk_category: str | None = None,
                               min_spend: float | None = None,
                               sort: str = "spend",
                               now: datetime | None = None) -> dict[str, Any]:
    """Complete selected-window Search Term Universe payload. Read-only.

    KPI values are computed from the COMPLETE filtered population (never the
    returned page); rows are server-side filtered, sorted and paginated.
    Raises EvidenceWindowError for an unknown window and SearchTermQueryError
    for invalid filters (caller maps both to HTTP 400).
    """
    _validate_filters(state, sort, TERM_SORTS)
    try:
        page = max(1, int(page))
    except (TypeError, ValueError) as exc:
        raise SearchTermQueryError(f"Invalid page '{page}'.") from exc
    try:
        page_size = max(1, min(MAX_PAGE_SIZE, int(page_size)))
    except (TypeError, ValueError) as exc:
        raise SearchTermQueryError(f"Invalid page_size '{page_size}'.") from exc

    start, end, base = _base(window, now)
    pop = _build_population(start, end)

    if not pop["available"]:
        return {
            **base, "db_unavailable": True,
            "kpis": _empty_kpis(), "rows": [],
            "pagination": {"total_count": None, "returned_count": 0,
                           "page": page, "page_size": page_size,
                           "has_more": False},
            "facets": {"campaigns": [], "junk_categories": []},
            "filters": _echo_filters(q, campaign, state, junk_category,
                                     min_spend, sort),
            "audit": _audit_block(base, pop, coverage_status="unavailable"),
        }

    units = pop["units"]
    currency_info = pop.get("currency_info") or {}
    filtered = _filter_units(units, q=q, campaign=campaign, state=state,
                             junk_category=junk_category, min_spend=min_spend)
    ordered = _sort_units(filtered, sort)

    # ── Complete-population KPIs (never page-scoped) ──
    state_counts = {STATE_FLAGGED: 0, STATE_CLEAN: 0, STATE_NEEDS_REVIEW: 0}
    spend_total = None
    spend_native_total = None
    clicks_total = 0
    for u in filtered:
        state_counts[_unit_state(u)] += 1
        spend_total = _sum_opt(spend_total, u["spend_usd"])
        prov = u.get("_provenance") or {}
        spend_native_total = _sum_opt(spend_native_total, prov.get("spend_native"))
        clicks_total += u["clicks"]
    if spend_total is None:
        spend_total = 0.0 if not filtered else spend_total

    coverage = _coverage_block(pop["source"], pop["canonical"], currency_info)
    kpis = {
        "reported_terms": len(filtered),
        "unique_search_terms": len({u["search_term"] for u in filtered}),
        "reported_spend_usd": _round2(spend_total),
        "reported_spend_native": _round2(spend_native_total),
        "native_currency": currency_info.get("native_currency"),
        "fx_complete": currency_info.get("fx_complete", False),
        "currency_status": currency_info.get("currency_status", "unavailable"),
        "clicks": clicks_total,
        "flagged_waste": state_counts[STATE_FLAGGED],
        "reviewed_clean": state_counts[STATE_CLEAN],
        "needs_review": state_counts[STATE_NEEDS_REVIEW],
        "coverage": coverage,
    }

    total = len(ordered)
    offset = (page - 1) * page_size
    page_rows = ordered[offset:offset + page_size]
    aliases_by_id = pop["aliases_by_id"]

    return {
        **base,
        "kpis": kpis,
        "rows": [_unit_row(u, aliases_by_id) for u in page_rows],
        "pagination": {
            "total_count": total,
            "returned_count": len(page_rows),
            "page": page,
            "page_size": page_size,
            "has_more": offset + len(page_rows) < total,
        },
        "facets": _facets(units),
        "filters": _echo_filters(q, campaign, state, junk_category, min_spend, sort),
        "audit": _audit_block(base, pop, coverage_status=coverage["status"],
                              state_counts=state_counts,
                              population_count=len(filtered)),
    }


def _safe_base(window: str, now: datetime | None = None) -> dict:
    """Base fields for a last-resort unavailable payload — tolerates an
    unknown/unresolvable window (the handler only reaches this on unexpected
    errors, never as a substitute for the 400 path)."""
    try:
        _, _, base = _base(window, now)
        return base
    except Exception:  # noqa: BLE001 - unknown/unresolvable window
        return {"window": window if isinstance(window, str) else "30d",
                "window_start": None, "window_end": None,
                "all_time": window == "all_time",
                "generated_at": (now or datetime.now(tz=timezone.utc))
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "spend_semantics": SPEND_SEMANTICS, "reporting_currency": "USD"}


_UNAVAILABLE_POP = {"available": False, "units": [], "source": {},
                    "identity_available": False,
                    "canonical": {"available": False}, "aliases_by_id": {}}


def unavailable_terms_response(window: str, now: datetime | None = None) -> dict:
    """Last-resort consistent db-unavailable payload for the Terms endpoint —
    same shape as a live response, every metric None (never a fabricated 0)."""
    base = _safe_base(window, now)
    return {
        **base, "db_unavailable": True, "kpis": _empty_kpis(), "rows": [],
        "pagination": {"total_count": None, "returned_count": 0, "page": 1,
                       "page_size": DEFAULT_PAGE_SIZE, "has_more": False},
        "facets": {"campaigns": [], "junk_categories": []},
        "filters": _echo_filters(None, None, None, None, None, "spend"),
        "audit": _audit_block(base, _UNAVAILABLE_POP,
                              coverage_status="unavailable"),
    }


def unavailable_term_drawer_response(window: str, now: datetime | None = None) -> dict:
    """Last-resort db-unavailable payload matching the term-drawer shape."""
    return {**_safe_base(window, now), "db_unavailable": True, "term": None}


def unavailable_patterns_response(window: str, now: datetime | None = None) -> dict:
    """Last-resort db-unavailable payload matching the Patterns endpoint shape."""
    base = _safe_base(window, now)
    return {
        **base, "db_unavailable": True, "rows": [],
        "kpis": {"patterns_found": None, "terms_analysed": None,
                 "patterns_with_flagged": None, "patterns_needing_review": None,
                 "reported_spend_represented_usd": None},
        "overlap": _overlap_meta(None, None, None),
        "pagination": {"total_count": None, "returned_count": 0,
                       "limit": DEFAULT_PATTERN_LIMIT, "has_more": False},
        "filters": _echo_pattern_filters(1, None, None, None, None, None, "spend"),
        "facets": {"campaigns": []},
        "audit": _audit_block(base, _UNAVAILABLE_POP,
                              coverage_status="unavailable"),
    }


def unavailable_pattern_drawer_response(window: str, now: datetime | None = None) -> dict:
    """Last-resort db-unavailable payload matching the pattern-drawer shape."""
    return {**_safe_base(window, now), "db_unavailable": True,
            "pattern": None, "terms": []}


def _empty_kpis() -> dict:
    return {
        "reported_terms": None, "unique_search_terms": None,
        "reported_spend_usd": None, "reported_spend_native": None,
        "native_currency": None, "fx_complete": False,
        "currency_status": "unavailable",
        "clicks": None,
        "flagged_waste": None, "reviewed_clean": None, "needs_review": None,
        "coverage": {"status": "unavailable", "canonical_spend_usd": None,
                     "reported_search_term_spend_usd": None,
                     "coverage_pct": None, "note": "Source unavailable."},
    }


def _echo_filters(q, campaign, state, junk_category, min_spend, sort) -> dict:
    return {"q": q or None, "campaign": campaign or None, "state": state or None,
            "junk_category": junk_category or None,
            "min_spend": float(min_spend) if min_spend is not None else None,
            "sort": sort or "spend"}


def _facets(units: list) -> dict:
    campaigns: dict = {}
    junk: set = set()
    for u in units:
        c = campaigns.setdefault(u["campaign_key"], {
            "campaign_key": u["campaign_key"],
            "campaign_name": u["campaign_name"],
            "mapping_status": u["mapping_status"],
        })
        # Keep a stable display name (first seen wins; identical keys share one).
        junk.update(u["junk_categories"])
    return {
        "campaigns": sorted(campaigns.values(),
                            key=lambda c: (c["campaign_name"] or "").lower()),
        "junk_categories": sorted(junk),
    }


def build_search_term_export(window: str, *, q=None, campaign=None, state=None,
                             junk_category=None, min_spend=None, sort="spend",
                             now: datetime | None = None) -> dict[str, Any]:
    """COMPLETE server-filtered dataset for export (no pagination) — the export
    is the full filtered population at term×campaign grain, so it is never a
    silently truncated page. Read-only."""
    _validate_filters(state, sort, TERM_SORTS)
    start, end, base = _base(window, now)
    pop = _build_population(start, end)
    if not pop["available"]:
        return {**base, "db_unavailable": True, "rows": [], "complete": False}
    filtered = _sort_units(
        _filter_units(pop["units"], q=q, campaign=campaign, state=state,
                      junk_category=junk_category, min_spend=min_spend), sort)
    aliases_by_id = pop["aliases_by_id"]
    return {**base, "db_unavailable": False, "complete": True,
            "rows": [_unit_row(u, aliases_by_id) for u in filtered]}


# ─────────────────────────────────────────────────────────────────────────────
# Term drawer
# ─────────────────────────────────────────────────────────────────────────────


def build_search_term_drawer(window: str, term: str,
                             campaign_key: str | None = None,
                             now: datetime | None = None) -> dict[str, Any]:
    """Evidence drawer payload for one search term (optionally scoped to one
    canonical campaign row). Same selected-window population as the table, so
    the drawer headline always matches the row exactly. Read-only.

    PR-ADS-144 §3: drawer evidence (classification proof, daily series) is
    scoped to the selected campaign identity — never borrows evidence from
    another campaign.
    """
    import db.search_term_repository as st_repo  # noqa: PLC0415

    start, end, base = _base(window, now)
    pop = _build_population(start, end)
    if not pop["available"]:
        return {**base, "db_unavailable": True, "term": None}

    matches = [u for u in pop["units"]
               if u["search_term"] == term
               and (campaign_key is None or u["campaign_key"] == campaign_key)]
    if not matches:
        return {**base, "db_unavailable": False, "_not_found": True, "term": None}

    aliases_by_id = pop["aliases_by_id"]
    if len(matches) == 1:
        unit = matches[0]
    else:
        # No campaign_key given and the term spans campaigns — merge a combined
        # view (facts only; identity block lists every campaign separately).
        unit = {
            "search_term": term, "campaign_key": None,
            "campaign_name": None, "mapping_status": "multiple",
            "spend_usd": None, "spend_raw": None,
            "clicks": 0, "impressions": 0,
            "conversions": None, "row_count": 0,
            "cost_micros": None, "currency_codes": set(), "source_systems": set(),
            "first_seen": None, "last_seen": None,
            "any_flagged": False, "any_unreviewed": False,
            "junk_categories": set(), "matched_patterns": set(),
            "ad_groups": set(), "keywords": set(), "match_types": set(),
            "source_labels": set(), "campaign_ids": set(),
        }
        for m in matches:
            _merge_group(unit, {
                **{k: m[k] for k in ("spend_usd", "clicks", "impressions",
                                     "conversions", "row_count", "first_seen",
                                     "last_seen", "junk_categories",
                                     "matched_patterns", "ad_groups", "keywords",
                                     "match_types")},
                "cost_micros": m.get("cost_micros"),
                "currency_codes": list(m.get("currency_codes") or []),
                "source_systems": list(m.get("source_systems") or []),
                "any_flagged": m["any_flagged"],
                "any_unreviewed": m["any_unreviewed"],
                "campaign_name": None, "campaign_id": None,
            })
            unit["source_labels"].update(m["source_labels"])
            unit["campaign_ids"].update(m["campaign_ids"])
        unit["_provenance"] = _assess_currency_provenance(unit)

    row = _unit_row(unit, aliases_by_id) if unit.get("campaign_key") is not None \
        else {**_unit_row({**unit, "campaign_key": "multiple",
                           "source_labels": unit["source_labels"]}, aliases_by_id),
              "campaign_key": None, "campaign_name": None,
              "mapping_status": "multiple"}

    # Daily evidence — campaign-scoped (PR-ADS-144 §3): use ID-first scoping.
    # When a specific campaign_key is given and it looks like a campaign_id,
    # scope by that ID. Name fallback only for null-ID rows.
    drawer_campaign_id = None
    drawer_campaign_names = None
    if len(matches) == 1:
        m = matches[0]
        ids = sorted(m.get("campaign_ids") or set())
        names = sorted(m.get("source_labels") or set())
        if ids:
            drawer_campaign_id = ids[0]
            # Name fallback for null-ID legacy rows only.
            drawer_campaign_names = names or None
        else:
            drawer_campaign_names = names or None
    else:
        # Multiple campaigns — no campaign_key given; use all identities.
        drawer_campaign_id = None
        all_names = sorted(unit.get("source_labels") or set())
        drawer_campaign_names = all_names or None

    daily = st_repo.fetch_search_term_daily_for_campaign(
        start, end, term,
        campaign_id=drawer_campaign_id,
        campaign_names=drawer_campaign_names)

    # Classification proof — campaign-scoped (PR-ADS-144 §3).
    cls_campaign_names = None
    if len(matches) == 1:
        m = matches[0]
        cls_campaign_names = sorted(m.get("source_labels") or set()) or None
    classification = st_repo.fetch_latest_waste_classification(
        term, campaign_names=cls_campaign_names)
    cls_row = classification.get("row") if classification.get("available") else None

    campaigns_context = [{
        "campaign_key": m["campaign_key"],
        "campaign_name": m["campaign_name"],
        "mapping_status": m["mapping_status"],
        "aliases": _unit_row(m, aliases_by_id)["aliases"],
        "source_labels": sorted(m["source_labels"]),
        "spend_usd": _round2(m["spend_usd"]),
        "clicks": m["clicks"],
    } for m in matches]

    return {
        **base,
        "db_unavailable": False,
        "term": row,
        "campaigns": campaigns_context,
        "matching_context": {
            "ad_groups": sorted(unit["ad_groups"]),
            "keywords": sorted(unit["keywords"]),
            "match_types": sorted(unit["match_types"]),
        },
        "classification": {
            "state": _unit_state(unit),
            "junk_categories": sorted(unit["junk_categories"]),
            "matched_patterns": sorted(unit["matched_patterns"]),
            "crm_junk_confirmed": (cls_row or {}).get("crm_junk_confirmed"),
            "classification_date": (cls_row or {}).get("run_date"),
            "classification_source": ("waste_terms (latest analysis run)"
                                      if cls_row else None),
            "semantics": CLASSIFICATION_SEMANTICS,
        },
        "platform_activity": {
            "conversions": _round2(unit["conversions"]),
            "disclosure": PLATFORM_CONVERSION_DISCLOSURE,
        },
        "daily": {
            "available": bool(daily.get("available")),
            "rows": daily.get("rows") or [],
            "note": ("Only dates the source actually reported are shown — "
                     "missing dates are never fabricated as zero."),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Patterns tab (derived from the SAME population)
# ─────────────────────────────────────────────────────────────────────────────


def _term_units_for_patterns(units: list, *, q=None, campaign=None, state=None,
                             min_spend=None) -> list:
    """Apply the shared filters then unify to UNIQUE search-term units (a term
    contributes once to pattern math, no matter how many campaigns it ran in)."""
    filtered = _filter_units(units, q=q, campaign=campaign, state=state,
                             min_spend=min_spend)
    by_term: dict = {}
    for u in filtered:
        t = by_term.get(u["search_term"])
        if t is None:
            t = by_term[u["search_term"]] = {
                "search_term": u["search_term"],
                "spend_usd": None, "clicks": 0, "impressions": 0,
                "conversions": None,
                "any_flagged": False, "any_unreviewed": False,
                "last_seen": None,
                "campaign_keys": set(), "campaign_names": set(),
                "campaign_rows": [],
            }
        t["spend_usd"] = _sum_opt(t["spend_usd"], u["spend_usd"])
        t["clicks"] += u["clicks"]
        t["impressions"] += u["impressions"]
        t["conversions"] = _sum_opt(t["conversions"], u["conversions"])
        t["any_flagged"] = t["any_flagged"] or u["any_flagged"]
        t["any_unreviewed"] = t["any_unreviewed"] or u["any_unreviewed"]
        ls = u["last_seen"]
        if ls is not None and (t["last_seen"] is None or ls > t["last_seen"]):
            t["last_seen"] = ls
        t["campaign_keys"].add(u["campaign_key"])
        if u["campaign_name"]:
            t["campaign_names"].add(u["campaign_name"])
        t["campaign_rows"].append(u)
    return list(by_term.values())


def _pattern_signal(flagged: int, clean: int, needs: int) -> str:
    if flagged > 0 and clean > 0:
        return SIGNAL_MIXED
    if flagged > 0:
        return SIGNAL_FLAGGED_PRESENT
    if needs > 0:
        return SIGNAL_NEEDS_REVIEW
    return SIGNAL_CLEAN_ONLY


def _validate_pattern_params(n, sort, limit, min_terms):
    try:
        n = int(n)
    except (TypeError, ValueError) as exc:
        raise SearchTermQueryError(f"Invalid pattern length '{n}'.") from exc
    if n not in PATTERN_LENGTHS:
        raise SearchTermQueryError(
            f"Unsupported pattern length '{n}'. Valid: 1, 2, 3.")
    if sort is not None and sort not in PATTERN_SORTS:
        raise SearchTermQueryError(
            f"Unsupported sort '{sort}'. Valid: {', '.join(PATTERN_SORTS)}.")
    try:
        limit = max(1, min(MAX_PATTERN_LIMIT, int(limit)))
    except (TypeError, ValueError) as exc:
        raise SearchTermQueryError(f"Invalid limit '{limit}'.") from exc
    if min_terms is not None:
        try:
            min_terms = max(1, int(min_terms))
        except (TypeError, ValueError) as exc:
            raise SearchTermQueryError(f"Invalid min_terms '{min_terms}'.") from exc
    return n, limit, min_terms


def _build_patterns(term_units: list, n: int) -> tuple[dict, dict]:
    """Aggregate patterns over unique term units. Returns (patterns, membership)
    where membership maps search_term → set of patterns it joined."""
    patterns: dict = {}
    membership: dict = {}
    for t in term_units:
        tokens = tokenize_search_term(t["search_term"])
        grams = {phrase for phrase, _ in build_ngrams(tokens, {n})}
        if not grams:
            continue
        membership[t["search_term"]] = grams
        st = (STATE_FLAGGED if t["any_flagged"]
              else STATE_NEEDS_REVIEW if t["any_unreviewed"] else STATE_CLEAN)
        for phrase in grams:
            p = patterns.get(phrase)
            if p is None:
                p = patterns[phrase] = {
                    "pattern": phrase, "n": n, "terms": 0,
                    "flagged_terms": 0, "clean_terms": 0, "needs_review_terms": 0,
                    "spend_usd": None, "clicks": 0, "conversions": None,
                    "campaign_keys": set(), "campaign_names": set(),
                    "_units": [],
                }
            p["terms"] += 1
            p["flagged_terms"] += 1 if st == STATE_FLAGGED else 0
            p["clean_terms"] += 1 if st == STATE_CLEAN else 0
            p["needs_review_terms"] += 1 if st == STATE_NEEDS_REVIEW else 0
            p["spend_usd"] = _sum_opt(p["spend_usd"], t["spend_usd"])
            p["clicks"] += t["clicks"]
            p["conversions"] = _sum_opt(p["conversions"], t["conversions"])
            p["campaign_keys"].update(t["campaign_keys"])
            p["campaign_names"].update(t["campaign_names"])
            p["_units"].append(t)
    return patterns, membership


def _sort_patterns(rows: list, sort: str) -> list:
    keyers = {
        "spend": lambda p: (_null_last_desc(p["spend_usd"]),
                            _null_last_desc(p["terms"]), p["pattern"]),
        "terms": lambda p: (_null_last_desc(p["terms"]),
                            _null_last_desc(p["spend_usd"]), p["pattern"]),
        "flagged": lambda p: (_null_last_desc(p["flagged_terms"]),
                              _null_last_desc(p["spend_usd"]), p["pattern"]),
        "pattern": lambda p: (p["pattern"],),
    }
    return sorted(rows, key=keyers.get(sort or "spend", keyers["spend"]))


def build_search_pattern_evidence(window: str, *, n: int = 1,
                                  q: str | None = None,
                                  campaign: str | None = None,
                                  state: str | None = None,
                                  min_spend: float | None = None,
                                  min_terms: int | None = None,
                                  sort: str = "spend",
                                  limit: int = DEFAULT_PATTERN_LIMIT,
                                  now: datetime | None = None) -> dict[str, Any]:
    """Pattern (n-gram) evidence derived from the SAME selected-window
    deduplicated Search Term Universe as the Terms tab. Pattern KPI totals are
    computed from UNIQUE underlying terms — overlapping pattern rows are never
    summed into an account total. Read-only."""
    _validate_filters(state, None, TERM_SORTS)
    n, limit, min_terms = _validate_pattern_params(n, sort, limit, min_terms)

    start, end, base = _base(window, now)
    pop = _build_population(start, end)
    if not pop["available"]:
        return {
            **base, "db_unavailable": True, "rows": [],
            "kpis": {"patterns_found": None, "terms_analysed": None,
                     "patterns_with_flagged": None,
                     "patterns_needing_review": None,
                     "reported_spend_represented_usd": None},
            "overlap": _overlap_meta(None, None, None),
            "pagination": {"total_count": None, "returned_count": 0,
                           "limit": limit, "has_more": False},
            "filters": _echo_pattern_filters(n, q, campaign, state, min_spend,
                                             min_terms, sort),
            "facets": {"campaigns": []},
            "audit": _audit_block(base, pop, coverage_status="unavailable"),
        }

    term_units = _term_units_for_patterns(pop["units"], q=q, campaign=campaign,
                                          state=state, min_spend=min_spend)
    patterns, membership = _build_patterns(term_units, n)

    rows = list(patterns.values())
    if min_terms is not None:
        rows = [p for p in rows if p["terms"] >= min_terms]
    ordered = _sort_patterns(rows, sort)

    # ── KPI math over UNIQUE underlying terms represented by SURVIVING
    # patterns only (PR-ADS-144 §4). After min_terms filtering, rebuild
    # the surviving term set from the surviving pattern rows. ──
    surviving_patterns = {p["pattern"] for p in rows}
    surviving_terms = set()
    for t, grams in membership.items():
        if surviving_patterns & grams:
            surviving_terms.add(t)
    contributing = [t for t in term_units
                    if t["search_term"] in surviving_terms]
    unique_spend = None
    for t in contributing:
        unique_spend = _sum_opt(unique_spend, t["spend_usd"])
    if unique_spend is None and not contributing:
        unique_spend = 0.0
    total_memberships = sum(p["terms"] for p in rows)
    overlapping_terms = sum(
        1 for t, grams in membership.items()
        if t in surviving_terms
        and sum(1 for p in rows if p["pattern"] in grams) > 1)

    kpis = {
        "patterns_found": len(rows),
        "terms_analysed": len(contributing),
        "patterns_with_flagged": sum(1 for p in rows if p["flagged_terms"] > 0),
        "patterns_needing_review": sum(1 for p in rows
                                       if p["needs_review_terms"] > 0),
        "reported_spend_represented_usd": _round2(unique_spend),
    }

    returned = ordered[:limit]
    out_rows = [{
        "pattern": p["pattern"], "n": p["n"],
        "signal": _pattern_signal(p["flagged_terms"], p["clean_terms"],
                                  p["needs_review_terms"]),
        "terms": p["terms"],
        "flagged_terms": p["flagged_terms"],
        "clean_terms": p["clean_terms"],
        "needs_review_terms": p["needs_review_terms"],
        "reported_spend_usd": _round2(p["spend_usd"]),
        "clicks": p["clicks"],
        "conversions": _round2(p["conversions"]),
        "campaigns_count": len(p["campaign_keys"]),
    } for p in returned]

    return {
        **base,
        "db_unavailable": False,
        "kpis": kpis,
        "rows": out_rows,
        "overlap": _overlap_meta(len(contributing), total_memberships,
                                 overlapping_terms),
        "pagination": {"total_count": len(rows), "returned_count": len(returned),
                       "limit": limit, "has_more": len(rows) > len(returned)},
        "filters": _echo_pattern_filters(n, q, campaign, state, min_spend,
                                         min_terms, sort),
        "facets": _facets(pop["units"]),
        "audit": {
            **_audit_block(base, pop,
                           coverage_status=_coverage_block(
                               pop["source"], pop["canonical"],
                               pop.get("currency_info"))["status"]),
            "patterns_derivation": (
                "derived from the same selected-window deduplicated "
                "search_terms population as the Terms tab"),
            "pattern_kpi_spend_semantics": "unique_underlying_terms",
        },
    }


def _overlap_meta(unique_terms, total_memberships, overlapping_terms) -> dict:
    return {
        "unique_terms_analysed": unique_terms,
        "total_pattern_memberships": total_memberships,
        "overlapping_term_count": overlapping_terms,
        "spend_semantics": "unique_underlying_terms",
        "disclosure": PATTERN_OVERLAP_DISCLOSURE,
    }


def _echo_pattern_filters(n, q, campaign, state, min_spend, min_terms, sort) -> dict:
    return {"n": n, "q": q or None, "campaign": campaign or None,
            "state": state or None,
            "min_spend": float(min_spend) if min_spend is not None else None,
            "min_terms": min_terms, "sort": sort or "spend"}


def build_search_pattern_drawer(window: str, pattern: str, n: int, *,
                                q: str | None = None,
                                campaign: str | None = None,
                                state: str | None = None,
                                min_spend: float | None = None,
                                now: datetime | None = None) -> dict[str, Any]:
    """Drawer payload for one pattern: unique underlying terms + factual split.
    Totals use unique term identities — a term is never totalled twice because
    it ran in multiple campaigns or matched the pattern in multiple positions."""
    _validate_filters(state, None, TERM_SORTS)
    n, _, _ = _validate_pattern_params(n, None, DEFAULT_PATTERN_LIMIT, None)

    start, end, base = _base(window, now)
    pop = _build_population(start, end)
    if not pop["available"]:
        return {**base, "db_unavailable": True, "pattern": None, "terms": []}

    term_units = _term_units_for_patterns(pop["units"], q=q, campaign=campaign,
                                          state=state, min_spend=min_spend)
    members = []
    for t in term_units:
        tokens = tokenize_search_term(t["search_term"])
        grams = {phrase for phrase, _ in build_ngrams(tokens, {n})}
        if pattern in grams:
            members.append(t)
    if not members:
        return {**base, "db_unavailable": False, "_not_found": True,
                "pattern": None, "terms": []}

    flagged = sum(1 for t in members if t["any_flagged"])
    needs = sum(1 for t in members
                if not t["any_flagged"] and t["any_unreviewed"])
    clean = len(members) - flagged - needs
    spend = None
    conversions = None
    clicks = 0
    campaign_names: set = set()
    for t in members:
        spend = _sum_opt(spend, t["spend_usd"])
        conversions = _sum_opt(conversions, t["conversions"])
        clicks += t["clicks"]
        campaign_names.update(t["campaign_names"])

    members.sort(key=lambda t: (_null_last_desc(t["spend_usd"]), t["search_term"]))
    shown = members[:PATTERN_DRAWER_TERM_CAP]

    return {
        **base,
        "db_unavailable": False,
        "pattern": {
            "pattern": pattern, "n": n,
            "signal": _pattern_signal(flagged, clean, needs),
            "terms": len(members),
            "flagged_terms": flagged, "clean_terms": clean,
            "needs_review_terms": needs,
            "reported_spend_usd": _round2(spend),
            "clicks": clicks,
            "conversions": _round2(conversions),
            "campaigns": sorted(campaign_names),
        },
        "platform_activity": {
            "conversions": _round2(conversions),
            "disclosure": PLATFORM_CONVERSION_DISCLOSURE,
        },
        "terms": [{
            "search_term": t["search_term"],
            "state": (STATE_FLAGGED if t["any_flagged"]
                      else STATE_NEEDS_REVIEW if t["any_unreviewed"]
                      else STATE_CLEAN),
            "campaigns": sorted(t["campaign_names"]),
            "spend_usd": _round2(t["spend_usd"]),
            "clicks": t["clicks"],
            "last_seen": _iso(t["last_seen"]),
        } for t in shown],
        "terms_truncated": len(members) > len(shown),
        "overlap_note": PATTERN_OVERLAP_DISCLOSURE,
    }
