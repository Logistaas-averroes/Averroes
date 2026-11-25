"""
Search Terms + Patterns evidence (PR-ADS-144, completed by PR-ADS-145).
Read-only.

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
  - Currency lineage (PR-ADS-144) + per-unit monetary completeness
    (PR-ADS-145): the table stores ``cost_micros`` (raw Google Ads metric),
    ``currency_code`` (native account currency), and ``source_system`` (data
    provenance). Each durable term × campaign unit is assessed and per-date
    FX-converted to USD INDEPENDENTLY, using the same fx_rates doctrine as
    canonical campaign spend. A unit contributes to verified monetary KPIs
    only when its currency is proven AND every source_date has an FX rate
    (fx_complete); an unproven/legacy or FX-incomplete unit never suppresses
    or poisons a verified unit's USD. Monetary KPIs therefore carry a
    three-state completeness — complete / partial / unavailable — and, when
    partial, a verified-only subtotal plus a count of the excluded units.
    Legacy rows with no provable currency stay visible with monetary fields
    null and ``legacy_currency_unverified: true`` — never assigned a currency,
    never a fabricated zero. ``spend_usd`` in the table is the legacy column
    name (cost_micros / 1e6 at ingestion time — native currency, NOT proven
    USD).
  - Classification (PR-ADS-145 precedence) resolves per unit, highest first:
      1. durable ``search_terms.is_flagged_waste = true`` → Flagged waste;
      2. safely campaign-scoped confirmed ``waste_terms`` evidence (the scheduled
         waste-detection step — monthly since PR-ADS-165A) → Flagged waste;
      3. durable ``is_flagged_waste = false`` on every underlying row →
         Reviewed clean;
      4. otherwise (any unreviewed row, or no evidence at all) → Needs review.
    ``waste_terms`` evidence is bridged in only when the (term, campaign
    name/alias) UNIQUELY and safely identifies one campaign identity — no
    fuzzy matching, no cross-campaign borrowing; a display name shared by two
    campaign ids for the same term is ambiguous and attaches to neither;
    ``not_google_ads`` labels are excluded. Absence from ``waste_terms`` NEVER
    means clean — only an explicit durable ``false`` does. ``False`` is never
    renamed to a business outcome and a Google Ads conversion never upgrades
    or downgrades the state. Table rows, the drawer and Patterns all derive
    from this same resolved state; ``classification_source`` discloses which
    durable evidence drove it.
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

import heapq
import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from analysis.evidence_windows import EvidenceWindowError  # noqa: F401 (re-export)
from analysis.ngrams import build_ngrams, tokenize_search_term
from analysis.search_term_identity import normalize_search_term, unit_identity
from analysis.search_term_review_state import (
    REVIEW_STATES as LOCAL_REVIEW_STATES,
    STATE_UNREVIEWED as LOCAL_STATE_UNREVIEWED,
    normalize_review_state,
    requires_action,
    review_state_payload,
)
from analysis.waste_reason_taxonomy import (
    ALL_REASONS,
    classify_reasons,
    primary_reason,
)
# The one evidence-window boundary + account timezone (PR-ADS-143), shared with
# the Keywords evidence so the Keywords & Search Terms page can never disagree
# with itself on what "last 30 days" means. PR-ADS-177C: moved out of the
# retired campaign_evidence_service, unchanged.
from analysis.account_time import ACCOUNT_TZ  # noqa: E402
from analysis.evidence_windows import evidence_window_bounds as _window_bounds  # noqa: E402
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

TERM_SORTS = ("spend", "clicks", "cpc", "conversions", "last_seen", "term",
              "attributed_sqls")
# PR-ADS-146C — Attributed-SQL row-state filters (search terms have no
# "ambiguous" state: a query is either directly persisted or unavailable).
TERM_SQL_STATES = ("all", "has_sql", "known_zero", "unavailable")
PATTERN_SORTS = ("spend", "terms", "flagged", "pattern")
PATTERN_LENGTHS = (1, 2, 3)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# ── Flagged / Waste view (PR-ADS-153D) ───────────────────────────────────────
# Sorts offered by the flagged view. ``priority`` is the deterministic,
# explainable ordering defined in ``_flagged_priority`` — never an opaque score.
FLAGGED_SORTS = ("priority", "spend", "clicks", "last_seen", "term",
                 "attributed_sqls")

# Truth states, shared vocabulary with the rest of the canonical contracts.
TRUTH_RECONCILED = "reconciled"
TRUTH_PARTIAL = "partial"
TRUTH_MISMATCH = "mismatch"
TRUTH_UNAVAILABLE = "unavailable"
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


class SearchTermIdentityError(RuntimeError):
    """One durable identity resolved to more than one row.

    An internal invariant break, never a user input error: it means the
    aggregation grain and the durable identity grain have diverged, so page
    rows and KPI counts can no longer be reconciled. Raised
    rather than served, because serving it means publishing numbers that
    contradict each other.
    """


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


class FxRatesUnavailable(Exception):
    """The FX rate READ failed (PR-ADS-172A).

    Not the same statement as "a rate is missing for some day". A missing rate
    is a fact about the rate table and is published as ``fx_incomplete``. A
    failed read is a fact about us: nothing about FX was learned, so nothing
    may be published as FX-incomplete on the strength of it.
    """


def _fx_rate_map(start, end, native_currency, reporting_currency, *,
                 strict: bool = False) -> dict:
    """{iso_date: rate} for native→reporting over the window (empty if none).

    ``strict`` (PR-ADS-172A): a rate read that FAILED raises
    ``FxRatesUnavailable`` instead of returning the same empty map a window
    with no rates returns. The population builds pass it; a drawer's daily
    series keeps the lenient form.
    """
    import db.revenue_repository as revenue_repo  # noqa: PLC0415

    result = revenue_repo.fetch_fx_rates(
        start, end, native_currency, reporting_currency)
    if strict and result.get("available") is False:
        raise FxRatesUnavailable(f"{native_currency}->{reporting_currency}")
    if not result.get("available"):
        return {}
    out = {}
    for k, v in (result.get("rates") or {}).items():
        if v is None:
            continue
        out[k.isoformat() if hasattr(k, "isoformat") else str(k)] = float(v)
    return out


def _convert_daily_micros(daily_micros: dict, native_currency, reporting_currency,
                          fx_by_iso: dict) -> tuple:
    """GENUINE per-source-date FX conversion (PR-ADS-144 §2). Read-only.

    ``daily_micros`` maps ``iso_date -> cost_micros``. Each day is converted at
    ITS OWN source-date rate — never a window-average — and the resulting USD
    amounts are summed:  ``window_usd = Σ(native_day × rate[day])``. When ANY
    required source-date rate is missing the USD total is withheld (returned as
    ``None``) and the missing dates are reported, so the amount is never
    silently wrong.

    Returns ``(spend_usd | None, complete, missing_dates)``.
    """
    if native_currency == reporting_currency:
        # Same currency — every day converts 1:1, always complete.
        total = sum(m for m in daily_micros.values() if m is not None) / 1_000_000
        return round(total, 2), True, []
    total = 0.0
    missing: list = []
    for iso_date, micros in sorted(daily_micros.items()):
        if micros is None:
            continue
        rate = fx_by_iso.get(iso_date)
        if rate is None:
            missing.append(iso_date)
            continue
        total += (micros / 1_000_000) * rate
    if missing:
        return None, False, sorted(missing)
    return round(total, 2), True, []


def _convert_daily_series(rows: list, start, end) -> list:
    """Convert a drawer's daily rows to USD at EACH row's own source-date FX
    rate (PR-ADS-144 §3/§4). Never passes the legacy ``spend_usd`` column
    through as USD. Each returned row carries: source_date, cost_micros,
    native_currency, spend_native, spend_usd (per-date converted or None),
    reporting_currency, fx_complete, currency_status, clicks, impressions."""
    from services.fx_service import REPORTING_CURRENCY  # noqa: PLC0415

    # Determine the single proven native currency across the series.
    currencies = set()
    for r in rows:
        codes = r.get("currency_codes") or []
        for c in codes:
            currencies.add(c)
    native_currency = next(iter(currencies)) if len(currencies) == 1 else None

    fx_by_iso = {}
    if native_currency and native_currency != REPORTING_CURRENCY:
        fx_by_iso = _fx_rate_map(start, end, native_currency, REPORTING_CURRENCY)

    out = []
    for r in rows:
        iso = r.get("source_date")
        micros = r.get("cost_micros")
        codes = r.get("currency_codes") or []
        systems = r.get("source_systems") or []
        proven = (len(codes) == 1 and bool(codes[0]) and micros is not None
                  and all(s == "google_ads_api" for s in systems) and bool(systems))
        row_currency = codes[0] if len(codes) == 1 else None
        spend_native = round(micros / 1_000_000, 6) if micros is not None else None

        if not proven or row_currency is None:
            spend_usd, fx_complete, status = None, False, "unavailable"
        elif row_currency == REPORTING_CURRENCY:
            spend_usd, fx_complete, status = (
                _round2(spend_native), True, "verified_same_currency")
        else:
            rate = fx_by_iso.get(iso)
            if rate is None:
                spend_usd, fx_complete, status = None, False, "fx_incomplete"
            else:
                spend_usd = round(float(spend_native) * rate, 2)
                fx_complete, status = True, "verified"

        out.append({
            "source_date": iso,
            "cost_micros": micros,
            "native_currency": row_currency,
            "spend_native": _round2(spend_native),
            "spend_usd": spend_usd,
            "reporting_currency": REPORTING_CURRENCY,
            "fx_complete": fx_complete,
            "currency_status": status,
            "clicks": r.get("clicks", 0),
            "impressions": r.get("impressions", 0),
        })
    return out


# ── Per-unit currency status vocabulary (PR-ADS-145 §2) ──────────────────────
CS_VERIFIED = "verified"                       # proven GBP, FX-complete → USD ok
CS_VERIFIED_SAME = "verified_same_currency"    # proven, native == USD
CS_FX_INCOMPLETE = "fx_incomplete"             # proven currency, a date lacks FX
CS_LEGACY_UNVERIFIED = "legacy_currency_unverified"  # no currency lineage (78 legacy)
CS_MIXED = "mixed_currency"                     # >1 currency within the unit
CS_UNPROVEN_SOURCE = "unproven_source"          # has currency, non-google source

# Monetary completeness statuses (population/filter level).
MON_COMPLETE = "complete"
MON_PARTIAL = "partial"
MON_UNAVAILABLE = "unavailable"


def _fx_convert_population(units: list, start, end, *, strict: bool = False) -> None:
    """Convert EACH unit's per-source-date native cost to USD at each day's own
    FX rate, INDEPENDENTLY per durable term × campaign unit (PR-ADS-145 §2).

    An unproven / legacy / FX-incomplete unit NEVER poisons an unrelated
    verified unit: it simply has its own ``spend_usd`` withheld while every
    verified, FX-complete unit keeps a genuine USD amount. Each unit is left
    carrying ``spend_usd``, ``fx_complete`` and ``currency_status``. Native
    spend (from provenance) is always preserved. Mutates ``units`` in place.
    """
    from services.fx_service import REPORTING_CURRENCY  # noqa: PLC0415

    fx_cache: dict = {}   # native_currency → {iso_date: rate}

    def _rate_map(ccy):
        if ccy == REPORTING_CURRENCY:
            return {}
        if ccy not in fx_cache:
            fx_cache[ccy] = _fx_rate_map(start, end, ccy, REPORTING_CURRENCY,
                                         strict=strict)
        return fx_cache[ccy]

    for u in units:
        prov = u.get("_provenance") or {}
        native_ccy = prov.get("native_currency")
        daily = u.get("_daily_micros") or {}

        # Unverified currency lineage — withhold USD, keep native, mark reason.
        if not prov.get("proven") or native_ccy in (None, "mixed"):
            u["spend_usd"] = None
            u["fx_complete"] = False
            if native_ccy == "mixed":
                u["currency_status"] = CS_MIXED
            elif native_ccy is None:
                u["currency_status"] = CS_LEGACY_UNVERIFIED
            else:
                u["currency_status"] = CS_UNPROVEN_SOURCE
            continue

        if not daily:
            # Proven lineage but no per-date cost to convert → USD unavailable.
            u["spend_usd"] = None
            u["fx_complete"] = False
            u["currency_status"] = CS_FX_INCOMPLETE
            continue

        usd, complete, _missing = _convert_daily_micros(
            daily, native_ccy, REPORTING_CURRENCY, _rate_map(native_ccy))
        u["spend_usd"] = usd if complete else None
        u["fx_complete"] = complete
        u["currency_status"] = (
            CS_VERIFIED_SAME if native_ccy == REPORTING_CURRENCY
            else CS_VERIFIED if complete else CS_FX_INCOMPLETE)


def _unit_verified_usd(u: dict) -> bool:
    """A unit contributes to verified monetary KPIs only when its USD is a
    genuine FX-complete converted amount (never a legacy/withheld None)."""
    return bool(u.get("fx_complete")) and u.get("spend_usd") is not None


def _monetary_summary(units: list) -> dict:
    """Aggregate the three-state monetary picture (PR-ADS-145 §1/§2) over a set
    of units. USD/native subtotals include ONLY verified FX-complete units, so
    a handful of legacy rows can never suppress a verified subtotal or inflate
    it. Never fabricates zero for an unverified row."""
    from services.fx_service import REPORTING_CURRENCY  # noqa: PLC0415

    total = len(units)
    verified_ccy = unverified_ccy = fx_complete = fx_incomplete = 0
    verified_usd = 0.0
    verified_native = 0.0
    currencies: set = set()
    for u in units:
        prov = u.get("_provenance") or {}
        native_ccy = prov.get("native_currency")
        if prov.get("proven") and native_ccy not in (None, "mixed"):
            verified_ccy += 1
            currencies.add(native_ccy)
            if _unit_verified_usd(u):
                fx_complete += 1
                verified_usd += float(u["spend_usd"])
                nat = prov.get("spend_native")
                if nat is not None:
                    verified_native += float(nat)
            else:
                fx_incomplete += 1
        else:
            unverified_ccy += 1

    if total == 0:
        status = MON_COMPLETE            # verified-empty window
    elif fx_complete == 0:
        status = MON_UNAVAILABLE         # nothing verified & FX-complete
    elif unverified_ccy == 0 and fx_incomplete == 0:
        status = MON_COMPLETE            # every row verified + FX-complete
    else:
        status = MON_PARTIAL

    pct = round(fx_complete / total * 100.0, 2) if total else 100.0
    native_currency = next(iter(currencies)) if len(currencies) == 1 else None
    return {
        "total_units": total,
        "verified_currency_units": verified_ccy,
        "unverified_currency_units": unverified_ccy,
        "fx_complete_units": fx_complete,
        "fx_incomplete_units": fx_incomplete,
        "verified_native_spend": _round2(verified_native) if fx_complete else (
            0.0 if total == 0 else None),
        "verified_usd_spend": _round2(verified_usd) if fx_complete else (
            0.0 if total == 0 else None),
        "native_currency": native_currency,
        "reporting_currency": REPORTING_CURRENCY,
        "monetary_completeness_status": status,
        "monetary_population_pct": pct,
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


def _representative_term(variants: set, normalized: str) -> str:
    """The raw variant shown for a unit whose fact rows differ only by case or
    whitespace (PR-ADS-153D §9).

    Deterministic so the same population always renders the same label: prefer a
    variant that already equals the normalized form, otherwise the
    lexicographically first. The other variants are never discarded — they ride
    along on the row as ``search_term_variants`` display evidence.
    """
    if not variants:
        return normalized
    if normalized in variants:
        return normalized
    return sorted(variants)[0]


def _sum_opt(a, b):
    """None-aware sum: None + x = x; None + None = None (never fabricate 0)."""
    if a is None:
        return b
    if b is None:
        return a
    return a + b


# ── PR-ADS-173A — the compact population unit ───────────────────────────────
#
# A population is one unit per normalized term × canonical campaign: ≈ 100,000
# of them for the account at ``all_time``. Each used to be a dict of 25 keys
# holding ten ``set`` objects, a per-date dict and a provenance dict — about
# 2.4 KB — so the units alone were ≈ 250 MB of the web process (docs/54).
#
# ``_Unit`` holds the SAME fields under the SAME keys and is read and written
# through the same ``unit["key"]`` / ``unit.get("key")`` protocol, so every
# filter, sort, KPI, facet and row builder below is the code it was. What
# changed is only how a unit is stored:
#
#   * fixed slots instead of a hash table per unit;
#   * the set-valued fields are frozensets, and equal ones are ONE object
#     shared by every unit of the population (most units carry the same
#     ``{"GBP"}``, the same ``{"google_ads_api"}``, the same campaign label);
#   * a unit with a single raw spelling stores that string, not a set of it;
#   * the per-date native amounts are one flat tuple, not a dict;
#   * ``_provenance`` is derived on read by ``_assess_currency_provenance``
#     from the fields it has always been derived from, instead of stored.
#
# Nothing is rounded, dropped, summarised or approximated: a value read from a
# ``_Unit`` equals the value the dict held. A set-valued field read from a
# ``_Unit`` is immutable, so a stray in-place ``.add()`` fails loudly rather
# than changing a set that other units share.

_EMPTY_FROZENSET: frozenset = frozenset()

#: Set-valued unit fields stored as (shared) frozensets.
_UNIT_SET_KEYS = frozenset({
    "currency_codes", "source_systems", "junk_categories", "matched_patterns",
    "ad_groups", "keywords", "match_types", "source_labels", "campaign_ids",
})

#: Fields stored as they are, one slot each, under their own key.
_UNIT_PLAIN_KEYS = (
    "search_term", "search_term_normalized", "campaign_key", "campaign_name",
    "mapping_status", "spend_usd", "spend_raw", "clicks", "impressions",
    "conversions", "row_count", "cost_micros", "first_seen", "last_seen",
    "any_flagged", "any_unreviewed",
    "currency_codes", "source_systems", "junk_categories", "matched_patterns",
    "ad_groups", "keywords", "match_types", "source_labels", "campaign_ids",
    # Set by the passes that follow the merge; ABSENT until then, exactly as a
    # dict key would be.
    "fx_complete", "currency_status", "_waste_flag", "_waste_evidence",
    "sql_attribution_coverage",
)
#: The per-unit SQL-attribution state. One state dict is the answer for very
#: many units (every unit of a mapping status, when the CRM carries no query),
#: so a unit REFERENCES its state instead of copying six values out of it.
_UNIT_SQL_KEYS = ("attributed_sqls", "sql_attribution_status",
                  "sql_attribution_source", "sql_candidate_count",
                  "sql_ambiguity_reason", "sql_contact_keys")
_UNIT_SQL_KEYS_SET = frozenset(_UNIT_SQL_KEYS)
#: Keys whose storage differs from their value (see ``_Unit._special``).
_UNIT_SPECIAL_KEYS = ("search_term_variants", "_daily_micros", "_provenance",
                      *_UNIT_SQL_KEYS)


class _Unit:
    """One merged term × canonical-campaign unit, stored compactly and read as
    the mapping it has always been (PR-ADS-173A)."""

    __slots__ = _UNIT_PLAIN_KEYS + ("_variants", "_dm", "_sql")

    def __init__(self, search_term, search_term_normalized, campaign_key,
                 campaign_name, mapping_status):
        self.search_term = search_term
        self.search_term_normalized = search_term_normalized
        self.campaign_key = campaign_key
        self.campaign_name = campaign_name
        self.mapping_status = mapping_status
        self.spend_usd = None
        self.spend_raw = None
        self.clicks = 0
        self.impressions = 0
        self.conversions = None
        self.row_count = 0
        self.cost_micros = None
        self.first_seen = None
        self.last_seen = None
        self.any_flagged = False
        self.any_unreviewed = False
        self.currency_codes = _EMPTY_FROZENSET
        self.source_systems = _EMPTY_FROZENSET
        self.junk_categories = _EMPTY_FROZENSET
        self.matched_patterns = _EMPTY_FROZENSET
        self.ad_groups = _EMPTY_FROZENSET
        self.keywords = _EMPTY_FROZENSET
        self.match_types = _EMPTY_FROZENSET
        self.source_labels = _EMPTY_FROZENSET
        self.campaign_ids = _EMPTY_FROZENSET
        # None = no annotation attached — what an absent key reads as.
        self._waste_flag = None
        self._waste_evidence = None
        self._variants = None      # None | one raw spelling | frozenset of them
        self._dm = None            # None | (iso, micros, iso, micros, …)

    # ── read ────────────────────────────────────────────────────────────────
    def __getitem__(self, key):
        try:
            return getattr(self, key)
        except AttributeError:
            return self._special(key)
        except TypeError:
            raise KeyError(key) from None

    def get(self, key, default=None):
        try:
            return getattr(self, key)
        except AttributeError:
            try:
                return self._special(key)
            except KeyError:
                return default
        except TypeError:
            return default

    def _special(self, key):
        if key == "search_term_variants":
            variants = self._variants
            if variants is None:
                return _EMPTY_FROZENSET
            if type(variants) is frozenset:
                return variants
            return frozenset((variants,))
        if key == "_daily_micros":
            flat = self._dm
            return dict(zip(flat[::2], flat[1::2])) if flat else {}
        if key == "_provenance":
            return _assess_currency_provenance(self)
        if key in _UNIT_SQL_KEYS_SET:
            try:
                value = self._sql[key]
            except AttributeError:
                raise KeyError(key) from None
            if key == "sql_contact_keys":
                return list(value or [])
            return value
        raise KeyError(key)

    def __contains__(self, key) -> bool:
        try:
            self[key]
        except KeyError:
            return False
        return True

    def keys(self):
        """The keys that are SET — what a dict unit's ``keys()`` would list —
        so ``{**unit, …}`` copies a unit as it always did."""
        out = [k for k in _UNIT_PLAIN_KEYS if hasattr(self, k)
               and not (k in ("_waste_flag", "_waste_evidence")
                        and getattr(self, k) is None)]
        out.extend(k for k in _UNIT_SPECIAL_KEYS if k in self)
        return out

    # ── write ───────────────────────────────────────────────────────────────
    def __setitem__(self, key, value) -> None:
        if key in _UNIT_SET_KEYS:
            if type(value) is not frozenset:
                value = frozenset(value)
            setattr(self, key, value)
        elif key == "search_term_variants":
            variants = value if type(value) is frozenset else frozenset(value)
            if not variants:
                self._variants = None
            elif len(variants) == 1:
                (self._variants,) = variants
            else:
                self._variants = variants
        elif key == "_daily_micros":
            flat: list = []
            for iso, micros in (value or {}).items():
                flat.extend((iso, micros))
            self._dm = tuple(flat) if flat else None
        elif key in _UNIT_SQL_KEYS_SET:
            # One field set on its own: this unit's private copy of its state.
            state = dict(getattr(self, "_sql", None) or {})
            state[key] = value
            self._sql = state
        elif key in _UNIT_PLAIN_KEYS_SET:
            setattr(self, key, value)
        else:
            # A field this unit does not have. Storing it nowhere and carrying
            # on would lose a value silently.
            raise KeyError(f"search-term unit has no field {key!r}")

    def set_sql_attribution(self, state: dict) -> None:
        """Attach an attribution state by REFERENCE. ``state`` must carry all
        six ``_UNIT_SQL_KEYS`` (the caller normalizes one that does not), so a
        read of any of them is the read of a key that is present."""
        self._sql = state

    def add_daily_micros(self, iso: str, micros: int) -> None:
        """``_daily_micros[iso] += micros`` on the flat per-date store."""
        flat = self._dm
        if flat is None:
            self._dm = (iso, micros)
            return
        for i in range(0, len(flat), 2):
            if flat[i] == iso:
                self._dm = flat[:i + 1] + (flat[i + 1] + micros,) + flat[i + 2:]
                return
        self._dm = flat + (iso, micros)


_UNIT_PLAIN_KEYS_SET = frozenset(_UNIT_PLAIN_KEYS)


def _add_to_set_field(unit, key: str, values, intern=None) -> None:
    """``unit[key] |= values`` for a dict unit (a mutable set, updated in
    place as always) or a compact ``_Unit`` (a frozenset, replaced by the
    union — shared through ``intern`` when one is given)."""
    if not values:
        return
    current = unit.get(key)
    if isinstance(current, set):
        current.update(values)
        return
    if current is None:
        if isinstance(unit, dict):
            unit[key] = set(values)
            return
        current = _EMPTY_FROZENSET
    merged = current.union(values)
    if len(merged) != len(current):
        unit[key] = intern(merged) if intern is not None else merged


def _merge_group(unit, g: dict, intern=None) -> None:
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
    _add_to_set_field(unit, "currency_codes", g.get("currency_codes"), intern)
    _add_to_set_field(unit, "source_systems", g.get("source_systems"), intern)
    fs, ls = g.get("first_seen"), g.get("last_seen")
    if fs is not None and (unit["first_seen"] is None or fs < unit["first_seen"]):
        unit["first_seen"] = fs
    if ls is not None and (unit["last_seen"] is None or ls > unit["last_seen"]):
        unit["last_seen"] = ls
    unit["any_flagged"] = unit["any_flagged"] or bool(g.get("any_flagged"))
    unit["any_unreviewed"] = unit["any_unreviewed"] or bool(g.get("any_unreviewed"))
    for k in ("junk_categories", "matched_patterns", "ad_groups", "keywords",
              "match_types"):
        _add_to_set_field(unit, k, g.get(k), intern)
    raw_term = g.get("search_term")
    if raw_term:
        _add_to_set_field(unit, "search_term_variants", (raw_term,))
    if g.get("campaign_name"):
        _add_to_set_field(unit, "source_labels", (g["campaign_name"],), intern)
    if g.get("campaign_id") is not None and str(g["campaign_id"]).strip():
        _add_to_set_field(unit, "campaign_ids",
                          (str(g["campaign_id"]).strip(),), intern)


def _unit_state(unit: dict) -> str:
    """Factual tri-state for a merged unit, with the PR-ADS-145 §4 precedence:

      1. durable ``search_terms.is_flagged_waste = true`` → Flagged waste
      2. safely campaign-scoped confirmed ``waste_terms`` evidence → Flagged waste
      3. durable ``is_flagged_waste = false`` (all rows reviewed) → Reviewed clean
      4. otherwise (any unreviewed row, or no evidence) → Needs review

    Absence from ``waste_terms`` NEVER makes a term clean — only an explicit
    durable ``false`` on every underlying row does."""
    if unit.get("any_flagged"):
        return STATE_FLAGGED
    if unit.get("_waste_flag"):
        return STATE_FLAGGED
    if unit.get("any_unreviewed"):
        return STATE_NEEDS_REVIEW
    return STATE_CLEAN


def _unit_safe_labels(unit: dict, aliases_by_id: dict) -> set:
    """The approved label set that safely identifies this unit's Google Ads
    campaign identity: canonical display name + approved aliases + the durable
    source labels. ``not_google_ads`` units contribute nothing (their evidence
    is never attached to a Google Ads campaign)."""
    if unit.get("mapping_status") == "not_google_ads":
        return set()
    labels = set(unit.get("source_labels") or set())
    if unit.get("campaign_name"):
        labels.add(unit["campaign_name"])
    if unit.get("mapping_status") == "mapped":
        labels |= set(aliases_by_id.get(unit.get("campaign_key"), set()))
    return {normalize_campaign_name(x) for x in labels if x}


def _globally_ambiguous_norms(norm_to_ids: dict, aliases_by_id: dict) -> set:
    """Normalized campaign names/aliases that resolve to MORE THAN ONE canonical
    campaign identity. ``waste_terms`` rows carry only a campaign_name (no
    campaign_id), so such a name can never be attributed to a single campaign —
    evidence bearing it must never be bridged (PR-ADS-145 §4 identity safety)."""
    norm_ids: dict = {}
    for norm, ids in (norm_to_ids or {}).items():
        if norm:
            norm_ids.setdefault(norm, set()).update(str(i) for i in ids)
    for cid, labels in (aliases_by_id or {}).items():
        for lbl in labels:
            n = normalize_campaign_name(lbl)
            if n:
                norm_ids.setdefault(n, set()).add(str(cid))
    return {n for n, ids in norm_ids.items() if len(ids) > 1}


def _attach_waste_evidence(units: list, start, end, identity_by_label,
                           aliases_by_id, norm_to_ids) -> dict:
    """Attach safely campaign-scoped ``waste_terms`` evidence to units so a term
    the monthly waste detection already confirmed shows as Flagged waste rather
    than Needs review (PR-ADS-145 §4). Mutates units in place.

    Returns the JOIN CONTRACT outcome (PR-ADS-153D §24):
    ``{available, annotation_rows, attached, legacy_unresolved}``. An annotation
    row that could not be attributed to exactly one canonical campaign is
    reported as ``legacy_unresolved`` — it is never guessed onto a unit, and
    never silently dropped either, because a reviewer needs to know that
    historical evidence exists which the current identifiers cannot place.

    Safety (no fuzzy, no cross-campaign borrowing). ``waste_terms`` rows key on
    (search_term, campaign_name) — they carry NO campaign_id — so evidence is
    bridged onto a unit only when its campaign name/alias identifies exactly one
    campaign, both locally and globally:
      * only ``mapped`` units are enriched — an ``unmatched`` (Mapping review) or
        ``not_google_ads`` unit has no confirmed Google Ads identity to bind to;
      * the label must UNIQUELY identify this unit among all units for the same
        term (a display name shared by two units for the term is ambiguous and
        attaches to NEITHER); and
      * the label must not be GLOBALLY ambiguous — a normalized name shared by
        more than one canonical campaign id can never be attributed to one
        campaign from the name alone, even if only one such campaign happens to
        have the term in the selected window.
    Ambiguous either way ⇒ the unit stays Needs review.
    """
    import db.search_term_repository as st_repo  # noqa: PLC0415

    # Query by every raw variant (that is what waste_terms stores), but key the
    # join on the NORMALIZED term so an annotation written against one casing
    # still reaches the unit that merged all casings.
    term_set: set = set()
    for u in units:
        term_set.update(v for v in (u.get("search_term_variants") or ()) if v)
        if u.get("search_term"):
            term_set.add(u["search_term"])
    terms = sorted(term_set)
    del term_set
    if not terms:
        return {"available": True, "annotation_rows": 0, "attached": 0,
                "legacy_unresolved": 0}
    evidence = st_repo.fetch_waste_evidence_for_terms(terms)
    if not evidence.get("available"):
        return {"available": False, "annotation_rows": None, "attached": 0,
                "legacy_unresolved": None}
    # (search_term, norm(campaign_name)) → latest evidence row.
    ev_by_key: dict = {}
    unresolved_keys: set = set()
    for r in (evidence.get("rows") or []):
        norm = normalize_campaign_name(r.get("campaign_name"))
        if norm:
            ev_by_key[(normalize_search_term(r.get("search_term")), norm)] = r
        else:
            # No campaign label at all — the row can never be placed on a
            # canonical campaign. Counted, never guessed.
            unresolved_keys.add((r.get("search_term"), None, id(r)))
    annotation_rows = len(evidence.get("rows") or [])
    attached_keys: set = set()

    ambiguous_norms = _globally_ambiguous_norms(norm_to_ids, aliases_by_id)

    # Group units by term so we can detect same-name ambiguity within a term.
    # ``_unit_safe_labels`` for EVERY unit (mapped + unmatched) feeds the local
    # ambiguity denominator, but only mapped units are eligible for attachment.
    #
    # PR-ADS-173A: only terms an annotation row names can attach anything (the
    # lookup below is keyed on the term), so only THEIR units are grouped —
    # every unit of such a term, in every campaign, which is the whole
    # ambiguity denominator. The outcome is the one grouping every term gave.
    annotated_terms = {key[0] for key in ev_by_key}
    by_term: dict = {}
    if annotated_terms:
        for u in units:
            if u["search_term_normalized"] in annotated_terms:
                by_term.setdefault(u["search_term_normalized"], []).append(u)

    for term, term_units in by_term.items():
        for u in term_units:
            if u.get("mapping_status") != "mapped":
                continue
            safe_labels = _unit_safe_labels(u, aliases_by_id)
            if not safe_labels:
                continue
            others = [o for o in term_units if o is not u]
            # A label shared with another unit for this term, OR globally shared
            # by more than one canonical campaign id, is ambiguous.
            unique_labels = {
                lbl for lbl in safe_labels
                if lbl not in ambiguous_norms
                and not any(lbl in _unit_safe_labels(o, aliases_by_id)
                            for o in others)
            }
            if not unique_labels:
                continue
            match = None
            matched_key = None
            for lbl in sorted(unique_labels):
                match = ev_by_key.get((term, lbl))
                if match is not None:
                    matched_key = (term, lbl)
                    break
            if match is None:
                continue
            attached_keys.add(matched_key)
            u["_waste_flag"] = True
            u["_waste_evidence"] = {
                "junk_category": match.get("junk_category"),
                "matched_pattern": match.get("matched_pattern"),
                "crm_junk_confirmed": match.get("crm_junk_confirmed"),
                "classification_date": match.get("run_date"),
                "classification_source": "waste_terms (scheduled waste detection)",
            }

    # Every annotation row that no safe join could place. These are historical
    # evidence whose stored identifiers are too weak to attribute to one
    # canonical campaign — surfaced as a named count so the gap is visible.
    legacy_unresolved = (len(set(ev_by_key.keys()) - attached_keys)
                         + len(unresolved_keys))
    return {
        "available": True,
        "annotation_rows": annotation_rows,
        "attached": len(attached_keys),
        "legacy_unresolved": legacy_unresolved,
    }


#: Which units a population holds (PR-ADS-172A).
#:
#: ``complete``            every term × campaign unit in the window. What the
#:                         Terms, Patterns and Flagged pages are built on.
#: ``flagged_candidates``  only the units of terms that durable evidence can
#:                         flag (see ``db.search_term_repository.
#:                         fetch_search_term_aggregates``). Every FLAGGED unit, its
#:                         complete metrics, the annotation join and the truth
#:                         state are identical to the complete population's;
#:                         units that cannot be flagged are simply not loaded.
POPULATION_COMPLETE = "complete"
POPULATION_FLAGGED_CANDIDATES = "flagged_candidates"
POPULATIONS = (POPULATION_COMPLETE, POPULATION_FLAGGED_CANDIDATES)


def _unavailable_population(reason: str | None = None) -> dict:
    pop = {"available": False, "units": [], "source": {},
           "identity_available": False, "canonical": {"available": False},
           "aliases_by_id": {}, "currency_info": _monetary_summary([])}
    if reason is not None:
        pop["unavailable_reason"] = reason
    return pop


def _build_population(start, end, *, flagged_candidates: bool = False,
                      normalized_terms=None) -> dict:
    """Fetch + merge the selected-window population at (search_term,
    canonical-campaign) grain. Returns {available, units, source, identity_available,
    canonical (spend result), aliases_by_id, currency_info}.

    ``flagged_candidates`` (PR-ADS-172A) narrows the read IN SQL to the terms
    that can belong to a flagged unit; the merge below is the same code either
    way, so a unit that is loaded is the unit the complete population holds.

    PR-ADS-173A:

    * ``normalized_terms`` narrows the read the same way to the units of the
      given normalized terms (a term drawer), when the reader offers it.
    * Every ``search_terms`` / ``waste_terms`` statement of the build — grouped
      rows, source totals, per-date costs, annotations — runs on ONE
      REPEATABLE READ, READ ONLY snapshot (``population_snapshot``).
    * The grouped rows and the per-date costs are STREAMED into the units
      (``sink=``) when the reader offers it, so neither result set is ever
      held as a list of dicts; the units themselves are compact (``_Unit``).
      A reader that only returns rows gets the same merge over those rows.
    """
    import db.search_term_repository as st_repo  # noqa: PLC0415

    with st_repo.population_snapshot():
        return _build_population_in_snapshot(
            start, end, flagged_candidates=flagged_candidates,
            normalized_terms=normalized_terms)


def _build_population_in_snapshot(start, end, *, flagged_candidates: bool,
                                  normalized_terms) -> dict:
    import db.revenue_repository as revenue_repo  # noqa: PLC0415
    import db.search_term_repository as st_repo  # noqa: PLC0415

    # Canonical campaign identity, read once — and only once the grouped read
    # has shown itself available (its first row, or its result), so an
    # unavailable population still reads nothing else, as before.
    context: dict = {}

    def _identity():
        if not context:
            canonical = revenue_repo.fetch_canonical_campaign_spend(start, end)
            identity = revenue_repo.fetch_campaign_identity(canonical.get("customer_id"))
            spend_by_id, norm_to_ids = _spend_index(canonical)
            identity_by_label, aliases_by_id = _identity_label_index(identity)
            context.update(
                canonical=canonical, identity=identity, spend_by_id=spend_by_id,
                norm_to_ids=norm_to_ids, identity_by_label=identity_by_label,
                aliases_by_id=aliases_by_id)
        return context

    # (campaign_key → normalized term → unit). Equal frozensets, campaign keys
    # and dates are stored once for the whole population.
    units_by_campaign: dict = {}
    unit_list: list = []          # in the order the units were first seen
    shared: dict = {}
    intern = lambda value: shared.setdefault(value, value)  # noqa: E731

    def _unit_for(term, campaign_id, campaign_name, *, create: bool):
        ctx = _identity()
        status, key, display = _resolve_campaign_identity(
            campaign_id, campaign_name, ctx["spend_by_id"], ctx["norm_to_ids"],
            ctx["identity_by_label"])
        # PR-ADS-153D: the merge key is the NORMALIZED term, so the aggregation
        # grain is exactly the durable identity grain. Grouping on the raw string
        # while identifying on the normalized one made "Freight JOBS" and
        # "freight jobs" two rows that shared one identity — the table showed two
        # rows, the KPI counted one term, and the Action Queue kept one item
        # carrying only one variant's spend.
        norm_term = normalize_search_term(term)
        by_term = units_by_campaign.get(key)
        if by_term is None:
            if not create:
                return None
            by_term = units_by_campaign[intern(key)] = {}
        unit = by_term.get(norm_term)
        if unit is None and create:
            if norm_term == term:
                norm_term = term              # one string, not two equal ones
            # Display label; the full raw set rides along in
            # search_term_variants and is resolved once merging is done.
            unit = by_term[norm_term] = _Unit(
                term, norm_term, intern(key), display, status)
            unit_list.append(unit)
        return unit

    def _merge_group_row(g: dict) -> None:
        term = g.get("search_term")
        if not term:
            return
        unit = _unit_for(term, g.get("campaign_id"), g.get("campaign_name"),
                         create=True)
        if g.get("first_seen") is not None:
            g["first_seen"] = intern(g["first_seen"])
        if g.get("last_seen") is not None:
            g["last_seen"] = intern(g["last_seen"])
        if g.get("conversions") is not None:
            # Few distinct values (most rows report 0.0): one float object each.
            g["conversions"] = intern(g["conversions"])
        _merge_group(unit, g, intern)

    iso_of: dict = {}

    def _merge_daily_cost(term, campaign_name, campaign_id, source_date, micros) -> None:
        # Attach per-SOURCE-DATE native cost to each unit so FX converts at
        # each day's own rate (PR-ADS-144 §2) — routed through the SAME identity
        # resolution so two same-named campaign ids never share a day's spend,
        # and the same normalized grain, so a day's cost for "Freight JOBS"
        # lands on the same unit as "freight jobs".
        if not term or micros is None:
            return
        unit = _unit_for(term, campaign_id, campaign_name, create=False)
        if unit is None:
            return
        iso = iso_of.get(source_date)
        if iso is None:
            iso = iso_of[source_date] = (
                source_date.isoformat() if hasattr(source_date, "isoformat")
                else str(source_date))
        unit.add_daily_micros(iso, int(micros))

    read_groups = st_repo.fetch_search_term_aggregates
    group_kwargs: dict = {}
    if flagged_candidates:
        group_kwargs["flagged_candidates"] = True
    if normalized_terms is not None and st_repo.reader_accepts(
            read_groups, "normalized_terms"):
        group_kwargs["normalized_terms"] = normalized_terms
    if st_repo.reader_accepts(read_groups, "sink"):
        group_kwargs["sink"] = _merge_group_row
    agg = st_repo.fetch_search_term_aggregates(start, end, **group_kwargs)
    if not agg.get("available"):
        # PR-ADS-177C: keep the scope's reason (e.g. no configured Google Ads
        # account) — the retired /api/search-terms* routes stated it, and the
        # live routes that replace them must too.
        return _unavailable_population(agg.get("reason"))

    # A reader that returned its rows instead of streaming them: the same merge.
    # The rows are consumed here and nothing after reads them (PR-ADS-172A).
    for g in agg.get("rows") or []:
        _merge_group_row(g)
    g = None
    source_totals = agg.get("source") or {}
    narrowed_terms = agg.get("search_terms")
    del agg
    ctx = _identity()

    read_costs = st_repo.fetch_search_term_daily_costs
    cost_kwargs: dict = {}
    if narrowed_terms is not None or flagged_candidates:
        # The SAME exact term list the aggregates were narrowed to.
        cost_kwargs["search_terms"] = narrowed_terms or []
    if st_repo.reader_accepts(read_costs, "sink"):
        cost_kwargs["sink"] = _merge_daily_cost
    elif "search_terms" in cost_kwargs:
        # The per-date amounts folded per (term, campaign) by the repository
        # rather than returned as one dict per (unit, day).
        cost_kwargs["folded"] = True
    daily_costs = st_repo.fetch_search_term_daily_costs(start, end, **cost_kwargs)
    if daily_costs.get("available") is False:
        # PR-ADS-172A — the per-date cost read FAILED. Carrying on would leave
        # every unit without a day to convert and publish it, in an available
        # response, as `fx_incomplete`: a true withholding with a false reason
        # (no FX rate is missing — the cost was never read). A population whose
        # cost could not be read is unavailable, and says so.
        logger.warning("[search-term-evidence] per-date cost read unavailable; "
                       "the population is withheld rather than published as "
                       "FX-incomplete")
        return _unavailable_population("search_term_daily_costs_unavailable")
    for (term, campaign_name, campaign_id), days in (
            daily_costs.get("daily_micros") or {}).items():
        for iso, micros in days.items():
            _merge_daily_cost(term, campaign_name, campaign_id, iso, micros)
    for d in (daily_costs.get("rows") or []):
        _merge_daily_cost(d.get("search_term"), d.get("campaign_name"),
                          d.get("campaign_id"), d.get("source_date"),
                          d.get("cost_micros"))
    d = None
    del daily_costs

    # Assess provenance and perform INDEPENDENT per-unit FX conversion — an
    # unproven legacy unit never poisons a verified unit (PR-ADS-145 §2).
    del units_by_campaign, shared
    for u in unit_list:
        u["search_term"] = _representative_term(
            u["search_term_variants"], u["search_term_normalized"])
    try:
        _fx_convert_population(unit_list, start, end, strict=True)
    except FxRatesUnavailable as exc:
        # PR-ADS-172A — the rate READ failed. Publishing these units as
        # `fx_incomplete` would state that a rate is missing; none was read.
        logger.warning("[search-term-evidence] FX rate read unavailable (%s); the "
                       "population is withheld rather than published as "
                       "FX-incomplete", exc)
        return _unavailable_population("fx_rates_unavailable")

    # Attach safely campaign-scoped waste_terms classification evidence so a
    # newly imported term that the monthly waste detection already confirmed shows as
    # Flagged waste rather than Needs review (PR-ADS-145 §4).
    annotation_join = _attach_waste_evidence(
        unit_list, start, end, ctx["identity_by_label"], ctx["aliases_by_id"],
        ctx["norm_to_ids"])

    return {
        "available": True,
        "units": unit_list,
        "source": source_totals,
        "identity_available": bool(ctx["identity"].get("available")),
        "canonical": ctx["canonical"],
        "aliases_by_id": ctx["aliases_by_id"],
        # Window-level three-state monetary picture (all units).
        "currency_info": _monetary_summary(unit_list),
        # PR-ADS-153D §24 — how the durable waste ANNOTATIONS joined the
        # canonical facts, including rows too weakly identified to place.
        "annotation_join": annotation_join,
    }


def _unit_classification(unit: dict) -> dict:
    """Per-unit classification evidence with its source (PR-ADS-145 §4). State
    comes from _unit_state; the source discloses WHICH durable evidence drove
    it so the UI never implies 'clean by absence'."""
    ev = unit.get("_waste_evidence") or {}
    state = _unit_state(unit)
    if unit.get("any_flagged"):
        source = "durable_flag"          # search_terms.is_flagged_waste = true
    elif unit.get("_waste_flag"):
        source = "waste_terms"           # safe monthly waste-detection evidence
    elif state == STATE_CLEAN:
        source = "durable_reviewed_clean"  # every underlying row explicit false
    else:
        source = "unclassified"          # needs review — NOT clean by absence
    junk = set(unit.get("junk_categories") or set())
    patterns = set(unit.get("matched_patterns") or set())
    if ev.get("junk_category"):
        junk.add(ev["junk_category"])
    if ev.get("matched_pattern"):
        patterns.add(ev["matched_pattern"])
    return {
        "state": state,
        "classification_source": source,
        "classification_date": ev.get("classification_date"),
        "junk_categories": sorted(junk),
        "matched_patterns": sorted(patterns),
        "crm_junk_confirmed": ev.get("crm_junk_confirmed"),
        "confidence": ("confirmed" if source in ("durable_flag", "waste_terms")
                       else "reviewed" if source == "durable_reviewed_clean"
                       else "unreviewed"),
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
    # Raw variants that merged into this unit, shown only when they differ from
    # the display label — the evidence that nothing was silently collapsed.
    variants = sorted(v for v in (unit.get("search_term_variants") or set())
                      if v and v != unit["search_term"])
    return {
        "search_term": unit["search_term"],
        "search_term_normalized": unit.get("search_term_normalized"),
        "search_term_variants": variants,
        "campaign_key": unit["campaign_key"],
        "campaign_name": display,
        "mapping_status": unit["mapping_status"],
        "aliases": aliases,
        "state": _unit_state(unit),
        "spend_usd": _round2(spend),
        "spend_native": _round2(prov.get("spend_native")),
        "native_currency": prov.get("native_currency"),
        "reporting_currency": "USD",
        # fx_complete / currency_status reflect the ACTUAL per-date conversion
        # result (set by _fx_convert_population), NOT merely provenance
        # availability (PR-ADS-144 §4). A proven-lineage unit with a missing
        # source-date rate is fx_complete = False.
        "fx_complete": bool(unit.get("fx_complete", False)),
        "currency_status": unit.get(
            "currency_status",
            "proven" if prov.get("proven")
            else prov.get("quarantine_reason") or "unknown"),
        # PR-ADS-145 §3: the 78 legacy rows are visible with monetary fields
        # unavailable and explicitly marked — never assigned GBP/USD.
        "legacy_currency_unverified":
            unit.get("currency_status") == CS_LEGACY_UNVERIFIED,
        "clicks": clicks,
        "impressions": unit["impressions"],
        "conversions": _round2(unit["conversions"]),
        "cpc_usd": cpc,
        "first_seen": _iso(unit["first_seen"]),
        "last_seen": _iso(unit["last_seen"]),
        "junk_categories": sorted(unit["junk_categories"]),
        "matched_patterns": sorted(unit["matched_patterns"]),
        # Per-row classification evidence + which durable source drove the state
        # (PR-ADS-145 §4). Absence of waste evidence is never rendered as clean.
        "classification_source": _unit_classification(unit)["classification_source"],
        "crm_junk_confirmed": (unit.get("_waste_evidence") or {}).get("crm_junk_confirmed"),
        "source_rows": unit["row_count"],
        # PR-ADS-146C — HubSpot Attributed SQLs (direct query evidence only).
        "attributed_sqls": unit.get("attributed_sqls"),
        "sql_attribution_status": unit.get("sql_attribution_status", "unavailable"),
        "sql_attribution_source": unit.get("sql_attribution_source"),
        "sql_attribution_coverage": unit.get("sql_attribution_coverage"),
        "sql_ambiguity_reason": unit.get("sql_ambiguity_reason"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Filters / sorts
# ─────────────────────────────────────────────────────────────────────────────


def _unit_matches_text(u: dict, needle: str) -> bool:
    """Free-text match against the display term AND every raw variant, so a
    search for the exact casing a user saw in Google Ads still finds the merged
    unit."""
    if needle in (u.get("search_term") or "").lower():
        return True
    if needle in (u.get("search_term_normalized") or ""):
        return True
    return any(needle in (v or "").lower()
               for v in (u.get("search_term_variants") or set()))


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
        if needle and not _unit_matches_text(u, needle):
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


def _unit_sort_key(sort: str):
    def _cpc(u):
        if u["spend_usd"] is not None and u["clicks"] > 0:
            return float(u["spend_usd"]) / u["clicks"]
        return None

    def base(u):
        return (u["search_term_normalized"], u["campaign_key"])

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
        # Attributed SQLs: highest first; unavailable/null count always last.
        "attributed_sqls": lambda u: (_null_last_desc(u.get("attributed_sqls")), *base(u)),
    }
    return keyers.get(sort or "spend", keyers["spend"])


def _sort_units(units: list, sort: str) -> list:
    return sorted(units, key=_unit_sort_key(sort))


def _sorted_page(units: list, sort: str, offset: int, limit: int) -> list:
    """``_sort_units(units, sort)[offset:offset + limit]`` without building a
    sort key for every unit and keeping them all at once (PR-ADS-173A).

    ``heapq.nsmallest(n, …, key=…)`` is defined as ``sorted(…, key=…)[:n]``,
    ties included (it is stable), and holds only the ``n`` smallest keys. For a
    page deep enough that most of the population precedes it, the plain sort
    is used — the same result either way.
    """
    stop = offset + limit
    if stop * 4 >= len(units):
        return sorted(units, key=_unit_sort_key(sort))[offset:stop]
    return heapq.nsmallest(stop, units, key=_unit_sort_key(sort))[offset:]


# ── HubSpot SQL attribution (PR-ADS-146C §5) ─────────────────────────────────
def _st_unit_key(u: dict) -> str:
    """Stable term × canonical-campaign key for SQL attribution.

    Uses the NORMALIZED term so this key partitions the population exactly as
    the durable identity does — two casings of one query can never be handed to
    attribution as two separate units."""
    return f"{u.get('campaign_key')}\x00{u.get('search_term_normalized')}"


def _search_term_sql_attribution(pop: dict, start, end) -> dict:
    """Attribute qualified SQL contacts to term × campaign units. Search-term
    attribution requires a directly persisted user query; the durable leads table
    has none, so every unit resolves UNAVAILABLE (—), never a fabricated 0.
    Defensive — a failure yields an unavailable verdict, never a page break."""
    try:
        from services.platform_sql_attribution_service import (  # noqa: PLC0415
            attribute_search_terms, fetch_and_resolve_contacts,
        )
        contacts_res = fetch_and_resolve_contacts(start, end)
        available = bool(contacts_res.get("available"))
        contacts = contacts_res.get("contacts") or []
        if not any(c.get("search_term_norm") for c in contacts):
            # PR-ADS-173A — no contact carries a query (the durable `leads`
            # table persists none), so no contact can be matched to ANY unit:
            # the reconciliation, coverage, completeness and audit blocks are
            # the ones an empty unit list produces, and every unit's own state
            # is decided by its mapping status alone. That is asked for per
            # STATUS, from the attribution service's own rule, instead of
            # copying ≈ 100,000 units into dicts to compute ≈ 100,000 equal
            # answers. The moment one contact carries a query this branch is
            # not taken and every unit is attributed individually, below.
            from services.platform_sql_attribution_service import (  # noqa: PLC0415
                unit_state_without_text_evidence,
            )
            attr = attribute_search_terms(
                contacts, [], available=available,
                leads_have_search_term=bool(contacts_res.get("leads_have_search_term")))
            states: dict = {}

            def _state_for(mapping_status):
                state = states.get(mapping_status)
                if state is None:
                    state = states[mapping_status] = unit_state_without_text_evidence(
                        mapping_status, available=available)
                return state

            attr["unit_state_by_mapping_status"] = _state_for
            attr["contacts"] = contacts
            return attr
        # A generator: the attribution service reads each unit once to build
        # its own working rows, so the units are not also copied into a list
        # of dicts here first (PR-ADS-173A).
        units_in = ({
            "unit_key": _st_unit_key(u),
            "campaign_id": (u.get("campaign_key") if u.get("mapping_status") == "mapped" else None),
            "search_term": u.get("search_term"),
            "mapping_status": u.get("mapping_status"),
        } for u in (pop.get("units") or []))
        attr = attribute_search_terms(
            contacts, units_in, available=available,
            leads_have_search_term=bool(contacts_res.get("leads_have_search_term")))
        attr["contacts"] = contacts
        return attr
    except MemoryError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[search-term-evidence] SQL attribution failed: %s", exc)
        return {"by_unit": {}, "reconciliation": {}, "coverage": {},
                "audit": {}, "available": False, "contacts": [],
                "population_has_text": False}


def _apply_search_term_sql(units: list, attr: dict) -> None:
    """Attach per-unit attribution to units so filter/sort/row see it."""
    by = attr.get("by_unit") or {}
    # PR-ADS-173A: present when no contact carries a query — the state is then
    # a function of the unit's mapping status (see _search_term_sql_attribution).
    state_for_status = attr.get("unit_state_by_mapping_status")
    cov = (attr.get("coverage") or {}).get("coverage_pct")
    for u in units:
        if state_for_status is not None:
            st = state_for_status(u.get("mapping_status"))
        else:
            st = by.get(_st_unit_key(u)) or {
                "attributed_sqls": None, "sql_attribution_status": "unavailable",
                "sql_attribution_source": None, "sql_ambiguity_reason": None,
                "sql_candidate_count": None, "sql_contact_keys": []}
        if type(u) is _Unit:
            # The same six values, by reference to the state that carries them
            # (PR-ADS-173A) — a state missing a key is completed with None
            # first, which is what `st.get(key)` assigned below.
            if not _UNIT_SQL_KEYS_SET <= st.keys():
                st = {k: st.get(k) for k in _UNIT_SQL_KEYS}
            u.set_sql_attribution(st)
            u["sql_attribution_coverage"] = cov
            continue
        u["attributed_sqls"] = st.get("attributed_sqls")
        u["sql_attribution_status"] = st.get("sql_attribution_status")
        u["sql_attribution_source"] = st.get("sql_attribution_source")
        u["sql_attribution_coverage"] = cov
        u["sql_candidate_count"] = st.get("sql_candidate_count")
        u["sql_ambiguity_reason"] = st.get("sql_ambiguity_reason")
        u["sql_contact_keys"] = st.get("sql_contact_keys") or []


def _filter_units_sql(units: list, sql_state: str | None) -> list:
    if not sql_state or sql_state == "all":
        return units
    if sql_state == "has_sql":
        return [u for u in units if u.get("sql_attribution_status") == "attributed"
                and (u.get("attributed_sqls") or 0) > 0]
    if sql_state == "known_zero":
        return [u for u in units if u.get("sql_attribution_status") == "known_zero"]
    if sql_state == "unavailable":
        return [u for u in units if u.get("sql_attribution_status")
                in ("unavailable", "mapping_review", "partial_attribution")]
    return units


def _search_term_sql_block(attr: dict) -> dict:
    """Response-level SQL-attribution audit + reconciliation (§11/§13)."""
    from services.platform_sql_attribution_service import (  # noqa: PLC0415
        SQL_ATTRIBUTION_METHOD_SEARCH_TERM, SQL_DATE_FIELD, SQL_DEDUP_KEY,
        SQL_DEFINITION, SQL_SOURCE,
    )
    recon = attr.get("reconciliation") or {}
    cov = attr.get("coverage") or {}
    audit = attr.get("audit") or {}
    comp = attr.get("completeness") or {}
    sta = audit.get("search_term_attribution") or {}
    return {
        "sql_source": SQL_SOURCE,
        "sql_definition": SQL_DEFINITION,
        "sql_date_field": SQL_DATE_FIELD,
        "sql_dedup_key": SQL_DEDUP_KEY,
        "sql_attribution_method": SQL_ATTRIBUTION_METHOD_SEARCH_TERM,
        "sql_attribution_available": bool(attr.get("available")),
        "exact_query_evidence_available": bool(attr.get("population_has_text")),
        "sql_attribution_coverage_pct": cov.get("coverage_pct"),
        # §4 — DISTINCT counts: exact-query evidence vs uniquely attributed.
        "sql_contacts_with_exact_search_term": sta.get("sql_contacts_with_exact_search_term"),
        "uniquely_attributed_search_term_sql_contacts": sta.get("uniquely_attributed_search_term_sql_contacts"),
        "sql_attributed_count": recon.get("uniquely_attributed_sql_contacts"),
        "sql_ambiguous_count": recon.get("ambiguous_sql_contacts"),
        "sql_unattributed_count": recon.get("unattributed_sql_contacts"),
        "sql_total_contacts": recon.get("total_sql_contacts"),
        "sql_row_sum": recon.get("row_sql_sum"),
        "sql_reconciliation_status": recon.get("reconciliation_status"),
        # §3 completeness / zero-proof.
        "sql_contacts_with_campaign_identity": comp.get("sql_contacts_with_campaign_identity"),
        "sql_contacts_missing_campaign_identity": comp.get("sql_contacts_missing_campaign_identity"),
        "sql_contacts_missing_search_term": comp.get("sql_contacts_missing_search_term"),
        "sql_attribution_completeness_status": comp.get("sql_attribution_completeness_status"),
        "zero_proof_available": comp.get("zero_proof_available"),
        "search_term_attribution": sta,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Coverage + audit
# ─────────────────────────────────────────────────────────────────────────────


def _coverage_block(mon: dict, canonical: dict) -> dict:
    """VERIFIED-ROW reporting coverage (PR-ADS-145 §1): FX-safe VERIFIED
    search-term USD ÷ FX-safe canonical campaign USD. The numerator is the
    verified FX-complete subtotal (``verified_usd_spend``) — never the legacy
    raw ``spend_usd`` column and never a partial figure presented as complete.
    Unavailable whenever no verified FX-complete rows exist, or canonical USD is
    missing / FX-incomplete / zero. When the monetary population is partial the
    coverage ``scope`` discloses that unverified rows are excluded.
    """
    verified_usd = mon.get("verified_usd_spend")
    status = mon.get("monetary_completeness_status")
    canonical_usd = (canonical.get("total_spend_usd")
                     if canonical.get("available") else None)
    canonical_fx_complete = canonical.get("fx_complete", False)
    if (verified_usd is None or mon.get("fx_complete_units", 0) <= 0
            or canonical_usd is None or not canonical_fx_complete
            or float(canonical_usd) <= 0):
        return {
            "status": "unavailable",
            "scope": "unavailable",
            "canonical_spend_usd": _round2(canonical_usd),
            "verified_search_term_spend_usd": _round2(verified_usd),
            "coverage_pct": None,
            "excluded_unverified_units": mon.get("unverified_currency_units"),
            "excluded_fx_incomplete_units": mon.get("fx_incomplete_units"),
            "note": ("Coverage requires at least one verified FX-complete "
                     "search-term row and an FX-complete canonical campaign USD "
                     "total for the same window."),
        }
    scope = "complete" if status == MON_COMPLETE else "verified_only"
    note = ("Completeness diagnostic only — verified search-term spend is not "
            "expected to reconcile exactly with canonical spend.")
    if scope == "verified_only":
        # The verified numerator excludes BOTH legacy (unverified-currency) rows
        # and FX-incomplete rows — disclose each so "excludes 0 legacy" can never
        # misrepresent an FX-incompleteness exclusion.
        note = ("Verified-row reporting coverage — EXCLUDES "
                f"{mon.get('unverified_currency_units')} unverified legacy row(s) "
                f"and {mon.get('fx_incomplete_units')} FX-incomplete row(s). "
                + note)
    return {
        "status": "ok",
        "scope": scope,
        "canonical_spend_usd": _round2(canonical_usd),
        "verified_search_term_spend_usd": _round2(verified_usd),
        "coverage_pct": round(float(verified_usd) / float(canonical_usd) * 100.0, 2),
        "excluded_unverified_units": mon.get("unverified_currency_units"),
        "excluded_fx_incomplete_units": mon.get("fx_incomplete_units"),
        "note": note,
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
                               sql_state: str | None = None,
                               now: datetime | None = None) -> dict[str, Any]:
    """Complete selected-window Search Term Universe payload. Read-only.

    KPI values are computed from the COMPLETE filtered population (never the
    returned page); rows are server-side filtered, sorted and paginated.
    Raises EvidenceWindowError for an unknown window and SearchTermQueryError
    for invalid filters (caller maps both to HTTP 400).
    """
    _validate_filters(state, sort, TERM_SORTS)
    if sql_state and sql_state not in TERM_SQL_STATES:
        raise SearchTermQueryError(
            f"Unsupported sql_state '{sql_state}'. Valid: {', '.join(TERM_SQL_STATES)}.")
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
            "unavailable_reason": pop.get("unavailable_reason"),
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
    # §5 — attribute qualified SQL contacts to term × campaign units (window-scoped,
    # filter-independent). Direct persisted query only; unavailable is never zero.
    sql_attr = _search_term_sql_attribution(pop, start, end)
    _apply_search_term_sql(units, sql_attr)
    filtered = _filter_units(units, q=q, campaign=campaign, state=state,
                             junk_category=junk_category, min_spend=min_spend)
    filtered = _filter_units_sql(filtered, sql_state)

    # ── Complete-population KPIs (never page-scoped) ──
    # State counts use the PR-ADS-145 §4/§5 precedence (durable-true OR safe
    # waste_terms evidence → flagged; absence is never clean). Monetary KPIs use
    # the per-FILTER three-state summary so a few legacy rows never suppress the
    # verified subtotal.
    state_counts = {STATE_FLAGGED: 0, STATE_CLEAN: 0, STATE_NEEDS_REVIEW: 0}
    clicks_total = 0
    for u in filtered:
        state_counts[_unit_state(u)] += 1
        clicks_total += u["clicks"]

    mon = _monetary_summary(filtered)
    coverage = _coverage_block(mon, pop["canonical"])
    kpis = {
        "reported_terms": len(filtered),
        "unique_search_terms": len({u["search_term_normalized"] for u in filtered}),
        # Verified Search-Term Spend — the FX-complete verified subtotal only.
        "verified_spend_usd": mon["verified_usd_spend"],
        "verified_spend_native": mon["verified_native_spend"],
        # Back-compat aliases (older keys) → same verified subtotal.
        "reported_spend_usd": mon["verified_usd_spend"],
        "reported_spend_native": mon["verified_native_spend"],
        "native_currency": mon["native_currency"],
        "reporting_currency": mon["reporting_currency"],
        "monetary": mon,                       # full three-state completeness block
        "monetary_status": mon["monetary_completeness_status"],
        "clicks": clicks_total,
        "flagged_waste": state_counts[STATE_FLAGGED],
        "reviewed_clean": state_counts[STATE_CLEAN],
        "needs_review": state_counts[STATE_NEEDS_REVIEW],
        "coverage": coverage,
    }

    # The page is sorted and sliced over the COMPLETE filtered population —
    # only the page's rows are serialized.
    total = len(filtered)
    offset = (page - 1) * page_size
    aliases_by_id = pop["aliases_by_id"]
    rows = [_unit_row(u, aliases_by_id)
            for u in _sorted_page(filtered, sort, offset, page_size)]
    facets = _facets(units)
    audit = _audit_block(base, pop, coverage_status=coverage["status"],
                         state_counts=state_counts,
                         population_count=len(filtered))
    sql_block = _search_term_sql_block(sql_attr)
    # PR-ADS-173A: everything the population contributes is computed above, so
    # it is released BEFORE the canonical reconciliation below — an independent
    # read with its own working set — instead of being held beside it.
    u = None
    del pop, units, filtered, sql_attr

    return {
        **base,
        "kpis": kpis,
        "rows": rows,
        "pagination": {
            "total_count": total,
            "returned_count": len(rows),
            "page": page,
            "page_size": page_size,
            "has_more": offset + len(rows) < total,
        },
        "facets": facets,
        "filters": {**_echo_filters(q, campaign, state, junk_category, min_spend, sort),
                    "sql_state": sql_state},
        "platform_date_field": "source_date",
        "sql_date_field": "contact_created_at",
        "sql_attribution": sql_block,
        # PR-ADS-152 §6: canonical SQL-scope reconciliation. Search Terms attribute
        # only exact persisted queries; the underlying SQL population is the
        # campaign-attributable subset, disclosed here with its explicit scope.
        "sql_reconciliation": _canonical_st_reconciliation(window, now),
        "audit": audit,
    }


def _canonical_st_reconciliation(window, now) -> dict:
    """Canonical campaign-attributable SQL-scope reconciliation for Search Terms
    (evidence window). Defensive — never breaks the page."""
    try:
        from services import canonical_contact_outcome_service as _canon  # noqa: PLC0415
        return _canon.page_reconciliation(
            _canon.WINDOW_EVIDENCE, window, _canon.SCOPE_CAMPAIGN_ATTRIBUTABLE, now=now)
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001
        return {}


def _safe_base(window: str, now: datetime | None = None) -> dict:
    """Base fields for a last-resort unavailable payload — tolerates an
    unknown/unresolvable window (the handler only reaches this on unexpected
    errors, never as a substitute for the 400 path)."""
    try:
        _, _, base = _base(window, now)
        return base
    except MemoryError:
        raise
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
        "verified_spend_usd": None, "verified_spend_native": None,
        "reported_spend_usd": None, "reported_spend_native": None,
        "native_currency": None, "reporting_currency": "USD",
        "monetary": _monetary_summary([]) | {"monetary_completeness_status": "unavailable"},
        "monetary_status": "unavailable",
        "clicks": None,
        "flagged_waste": None, "reviewed_clean": None, "needs_review": None,
        "coverage": {"status": "unavailable", "scope": "unavailable",
                     "canonical_spend_usd": None,
                     "verified_search_term_spend_usd": None,
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


# ─────────────────────────────────────────────────────────────────────────────
# Flagged / Waste view (PR-ADS-153D)
# ─────────────────────────────────────────────────────────────────────────────
# This REPLACES the standalone Flagged Waste Terms page. It is deliberately part
# of this module rather than a new service, because the old page's core defect
# was having a SECOND spend truth: it summed ``waste_terms`` rows across run
# snapshots, so one term observed by five weekly runs counted five times and its
# spend was multiplied by five.
#
# Here every metric comes from the SAME canonical population the Terms tab uses
# — ``search_terms`` facts merged at (search term × canonical campaign) grain,
# deduplicated by the unique fact index. ``waste_terms`` contributes ONLY
# classification annotations (reason, matched rule, CRM-junk confirmation), and
# never a number that is summed.


def _flagged_priority(unit: dict, review_state: str, high_spend_usd: float,
                      *, review_available: bool = True) -> dict:
    """Deterministic, fully explainable review priority. No AI score.

    Every component is a stated rule with a stated weight, and every applied
    component is echoed back in ``reasons`` so the ordering can be justified to
    the person being asked to act on it.

    Deliberately NOT a component: unavailable SQL attribution. "We could not
    check whether this term produced qualified outcomes" is not evidence of
    waste, and letting it raise priority would launder an unknown into a signal
    (§13). Only a PROVEN zero counts.
    """
    score = 0
    reasons: list[dict] = []

    def _add(points: int, code: str, detail: str) -> None:
        nonlocal score
        score += points
        reasons.append({"code": code, "points": points, "detail": detail})

    spend = unit.get("spend_usd")
    if spend is not None:
        spend = float(spend)
        if spend >= high_spend_usd:
            _add(40, "high_spend",
                 f"Spend ${spend:,.2f} is at or above the ${high_spend_usd:,.2f} "
                 "review threshold")
        elif spend > 0:
            # Proportional, capped, so a large term can never be out-ranked by
            # an accumulation of trivial ones.
            _add(min(20, int(spend * 20 / high_spend_usd)) if high_spend_usd else 0,
                 "spend_magnitude", f"Spend ${spend:,.2f} in the selected window")

    reason = primary_reason(unit.get("junk_categories"))
    from analysis.waste_reason_taxonomy import (  # noqa: PLC0415
        CLEAR_DISQUALIFYING_REASONS,
    )
    if reason["reason"] in CLEAR_DISQUALIFYING_REASONS:
        _add(25, "clear_disqualifying_intent",
             f"Flag reason '{reason['reason_label']}' is disqualifying on its face")

    # Proven zero only. `known_zero` means attribution was available and found
    # no qualified outcome; `unavailable` deliberately scores nothing.
    if unit.get("sql_attribution_status") == "known_zero":
        _add(15, "proven_zero_qualified_outcome",
             "Attribution available for this term and found no qualified SQL")

    rows = int(unit.get("row_count") or 0)
    if rows >= 2:
        _add(min(10, rows), "repeated_occurrences",
             f"Observed on {rows} canonical fact rows in the window")

    # Only when we can actually SEE that nobody has reviewed it. During a
    # review-store outage we do not know, and scoring an unknown as "never
    # reviewed" would promote terms a human had already dealt with.
    if review_available and normalize_review_state(review_state) == LOCAL_STATE_UNREVIEWED:
        _add(10, "never_reviewed", "No human decision recorded yet")

    score = max(0, min(100, score))
    band = "high" if score >= 60 else "medium" if score >= 30 else "low"
    return {"priority_score": score, "priority_band": band,
            "priority_reasons": reasons}


def _flagged_row(unit: dict, aliases_by_id: dict, review: dict | None,
                 high_spend_usd: float, *, review_available: bool = True) -> dict:
    """One flagged-view row: canonical facts + annotation + local decision."""
    base = _unit_row(unit, aliases_by_id)
    identity = unit_identity(unit)
    review = review or {}
    state = (normalize_review_state(review.get("review_state"))
             if review_available else None)
    classification = _unit_classification(unit)
    # Reason evidence must include the waste_terms ANNOTATION, not only the
    # durable search_terms.junk_category. A term flagged purely by the monthly
    # classification run has no durable category of its own, so reading the raw
    # column alone rendered its reason as "unmapped" even though the annotation
    # named it. _unit_classification is the one place that merges both.
    raw_reasons = classification["junk_categories"]
    reasons = classify_reasons(raw_reasons)
    primary = primary_reason(raw_reasons)
    priority = _flagged_priority({**unit, "junk_categories": set(raw_reasons)},
                                 state, high_spend_usd,
                                 review_available=review_available)

    return {
        **base,
        "term_identity": identity,
        # Why it is flagged — canonical vocabulary, raw evidence preserved.
        "flag_reason": primary["reason"],
        "flag_reason_label": primary["reason_label"],
        "flag_reasons": reasons,
        "flag_reason_unmapped": any(r["unmapped"] for r in reasons),
        "raw_junk_categories": raw_reasons,
        "flag_source": classification["classification_source"],
        "flag_confidence": classification["confidence"],
        # Flag history from the durable review record (§25). Absent history is
        # None — never a fabricated timestamp.
        "first_flagged_at": review.get("first_flagged_at"),
        "latest_flagged_at": review.get("latest_flagged_at"),
        # Local review decision.
        **review_state_payload(state, available=review_available),
        "review_note": review.get("review_note") if review_available else None,
        "reviewed_at": review.get("reviewed_at") if review_available else None,
        "reviewed_by": review.get("reviewed_by") if review_available else None,
        # Action state: still flagged AND no finished human decision.
        # None — not True — when the review store is unreadable: whether this
        # term needs action is exactly what we could not determine, and
        # defaulting to True would reopen resolved and kept terms.
        "action_needed": requires_action(state) if review_available else None,
        **priority,
    }


#: The fields of a flagged row that the view's filters, sorts, KPIs, facets and
#: identity invariant read (PR-ADS-173A). Everything else on a row is only ever
#: SERIALIZED, so it is built for the rows of the returned page alone.
_FLAGGED_INDEX_KEYS = (
    "term_identity", "search_term", "campaign_key", "campaign_name",
    "mapping_status", "review_state", "flag_reason", "flag_reason_label",
    "spend_usd", "clicks", "last_seen", "attributed_sqls",
    "sql_attribution_status", "action_needed", "priority_score",
)


class _FlaggedIndexRow(dict):
    """The ``_FLAGGED_INDEX_KEYS`` of one full flagged row.

    Each value is copied out of the row ``_flagged_row`` built — never
    recomputed — so a filter, sort or KPI reads exactly what it read from the
    full row. A read of ANY OTHER key raises instead of answering ``None``: if
    a filter or KPI ever starts reading a field that is not carried here, it
    fails loudly rather than silently treating every row as lacking it.
    """

    __slots__ = ()

    def __missing__(self, key):
        raise KeyError(f"flagged index row does not carry {key!r} — add it to "
                       "_FLAGGED_INDEX_KEYS")

    def get(self, key, default=None):  # noqa: ARG002 - strict on purpose
        return self[key]


def _flagged_truth_state(pop: dict, sql_attr: dict, *, available: bool) -> dict:
    """Truth state for the flagged view. Never collapses into zero.

    reconciled  canonical facts present AND SQL attribution resolvable
    partial     Google Ads facts present but CRM attribution incomplete, or
                durable annotations exist that could not be safely joined
    mismatch    an internal invariant failed
    unavailable the canonical fact source could not be read
    """
    if not available:
        return {"status": TRUTH_UNAVAILABLE,
                "reasons": ["canonical_search_term_facts_unavailable"]}

    reasons: list[str] = []
    units = pop.get("units") or []
    flagged = [u for u in units if _unit_state(u) == STATE_FLAGGED]

    # Invariant: a flagged unit must carry classification evidence explaining it.
    # A flag with no reason at all would be an unexplainable classification,
    # which this page must never render as normal (§8).
    unexplained = [u for u in flagged
                   if not (u.get("junk_categories")
                           or (u.get("_waste_evidence") or {}).get("junk_category"))]
    if unexplained:
        reasons.append(f"flagged_without_reason_evidence:{len(unexplained)}")
    if reasons:
        return {"status": TRUTH_MISMATCH, "reasons": reasons}

    if not sql_attr.get("available"):
        reasons.append("sql_attribution_unavailable")
    join = pop.get("annotation_join") or {}
    if join.get("available") is False:
        reasons.append("waste_annotations_unavailable")
    elif (join.get("legacy_unresolved") or 0) > 0:
        reasons.append(f"legacy_unresolved_annotations:{join['legacy_unresolved']}")

    if reasons:
        return {"status": TRUTH_PARTIAL, "reasons": reasons}
    return {"status": TRUTH_RECONCILED, "reasons": []}


def _flagged_sort_key(sort: str):
    def _null_last(v):
        return (v is None, -(float(v)) if v is not None else 0)

    def _desc_date(value):
        """Descending sort key for an ISO-8601 date/timestamp string.

        A string cannot be negated, so the zero-padded, fixed-width ISO digits
        are read as one integer and negated — monotonic, and correct for both
        ``2026-07-02`` and a full timestamp.
        """
        digits = "".join(ch for ch in str(value or "") if ch.isdigit())
        return -int(digits) if digits else 0

    def _term(u):
        return (u.get("search_term") or "").lower()

    keyers = {
        "priority": lambda u: (-int(u.get("priority_score") or 0), _term(u)),
        "spend": lambda u: (_null_last(u.get("spend_usd")), _term(u)),
        "clicks": lambda u: (-int(u.get("clicks") or 0), _term(u)),
        # Most-recent first, nulls last — the same direction as the Terms tab,
        # so "Last seen" cannot mean opposite things on two tabs of one page.
        "last_seen": lambda u: ((u.get("last_seen") is None),
                                _desc_date(u.get("last_seen")), _term(u)),
        "term": _term,
        "attributed_sqls": lambda u: (_null_last(u.get("attributed_sqls")), _term(u)),
    }
    return keyers.get(sort or "priority", keyers["priority"])


def build_flagged_search_terms(
    window: str, *, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE,
    q: str | None = None, campaign: str | None = None,
    review_state: str | None = None, flag_reason: str | None = None,
    min_spend: float | None = None, sql_state: str | None = None,
    sort: str = "priority", high_spend_usd: float = 100.0,
    population: str = POPULATION_COMPLETE,
    now: datetime | None = None) -> dict[str, Any]:
    """The canonical Flagged / Waste view. Read-only.

    ``population`` (PR-ADS-172A). ``complete`` (default) builds every term ×
    campaign unit in the window, which the Flagged page's audit block
    reconciles row-for-row against the source. ``flagged_candidates`` loads
    only the units durable evidence can flag: the flagged rows, their metrics,
    ``total_count``, the KPIs, the facets, the annotation join and the truth
    state are the same values, computed by the same code, without holding the
    window's unflagged terms. The response then says so in ``population_scope``
    and its ``audit`` block reconciles that narrowed population.

    A term appears here because DURABLE evidence says so — either
    ``search_terms.is_flagged_waste = true`` or safely campaign-scoped
    ``waste_terms`` evidence (the PR-ADS-145 §4 precedence). It is NEVER derived
    from ``spend > 0 AND sqls = 0``: that rule would classify every term whose
    attribution is merely unavailable as waste (§8, §13).

    Every metric is the canonical ``search_terms`` fact, deduplicated by the
    unique fact key — repeated ingestion of the same source-date fact cannot
    change any number here (§10).

    Raises ``EvidenceWindowError`` / ``SearchTermQueryError`` for bad input.
    """
    if sort not in FLAGGED_SORTS:
        raise SearchTermQueryError(
            f"Unsupported sort '{sort}'. Valid: {', '.join(FLAGGED_SORTS)}.")
    if review_state is not None and review_state not in LOCAL_REVIEW_STATES:
        raise SearchTermQueryError(
            f"Unsupported review_state '{review_state}'. "
            f"Valid: {', '.join(LOCAL_REVIEW_STATES)}.")
    if flag_reason is not None and flag_reason not in ALL_REASONS:
        raise SearchTermQueryError(
            f"Unsupported flag_reason '{flag_reason}'. Valid: {', '.join(ALL_REASONS)}.")
    if sql_state and sql_state not in TERM_SQL_STATES:
        raise SearchTermQueryError(
            f"Unsupported sql_state '{sql_state}'. Valid: {', '.join(TERM_SQL_STATES)}.")
    try:
        page = max(1, int(page))
        page_size = max(1, min(MAX_PAGE_SIZE, int(page_size)))
    except (TypeError, ValueError) as exc:
        raise SearchTermQueryError("Invalid pagination.") from exc

    if population not in POPULATIONS:
        raise SearchTermQueryError(
            f"Unsupported population '{population}'. Valid: {', '.join(POPULATIONS)}.")

    start, end, base = _base(window, now)
    pop, sql_attr, population = _flagged_population(start, end, population)
    if population != POPULATION_COMPLETE:
        base = {**base, "population_scope": population}

    echo_filters = {
        "q": q, "campaign": campaign, "review_state": review_state,
        "flag_reason": flag_reason, "min_spend": min_spend,
        "sql_state": sql_state, "sort": sort,
    }

    if not pop["available"]:
        return {
            **base, "view": "flagged", "db_unavailable": True,
            "unavailable_reason": pop.get("unavailable_reason"),
            "kpis": _empty_flagged_kpis(), "rows": [],
            "pagination": {"total_count": None, "returned_count": 0,
                           "page": page, "page_size": page_size,
                           "has_more": False},
            "facets": {"campaigns": [], "flag_reasons": [],
                       "review_states": list(LOCAL_REVIEW_STATES)},
            "filters": echo_filters,
            "truth_state": _flagged_truth_state(pop, {}, available=False),
            "annotation_join": pop.get("annotation_join") or {"available": False},
        }

    units = pop["units"]
    _apply_search_term_sql(units, sql_attr)

    # ── Mismatch quarantine (PR-ADS-153D §32) ────────────────────────────────
    # A mismatch means an internal invariant failed, so NOTHING derived from
    # this population can be trusted as a decision metric. Warning and then
    # rendering the numbers anyway is worse than useless: the reader sees a
    # caution they cannot act on next to figures that look authoritative.
    #
    # So the payload carries null KPIs, no actionable rows, and no facets or
    # pagination the UI could operate. The evidence needed to DIAGNOSE the
    # break travels in a separate `quarantine` block that the normal table and
    # KPI renderers cannot consume.
    truth_state = _flagged_truth_state(pop, sql_attr, available=True)
    if truth_state["status"] == TRUTH_MISMATCH:
        return _quarantined_flagged_payload(base, pop, truth_state, echo_filters,
                                            page, page_size)

    # The flagged population: durable evidence only.
    flagged_units = [u for u in units if _unit_state(u) == STATE_FLAGGED]

    # PR-ADS-173A: the audit block is the last reader of the units that are
    # NOT flagged. It is computed here and those units are released before one
    # full row is built per flagged unit, instead of being held beside them.
    audit = _audit_block(base, pop, coverage_status="ok")
    aliases_by_id = pop["aliases_by_id"]
    annotation_join = pop.get("annotation_join") or {}
    u = None
    del pop, units

    # Durable local decisions, joined by canonical identity.
    reviews, review_available = _fetch_reviews_for_units(flagged_units)

    def _full_row(u) -> dict:
        return _flagged_row(u, aliases_by_id, reviews.get(unit_identity(u)),
                            high_spend_usd, review_available=review_available)

    # PR-ADS-173A: one full row is built per flagged unit, as before, but only
    # the fields the filters / sort / KPIs / facets read are KEPT for all of
    # them (`_FlaggedIndexRow`). The full rows of the returned page are built
    # again below, by the same call on the same unit.
    rows_all: list = []
    unit_by_identity: dict = {}
    for u in flagged_units:
        full = _full_row(u)
        rows_all.append(_FlaggedIndexRow(
            (k, full[k]) for k in _FLAGGED_INDEX_KEYS))
        unit_by_identity[full["term_identity"]] = u
    full = None

    # HARD INVARIANT (PR-ADS-153D §9). One row per durable identity, always.
    # If the aggregation grain ever drifts from the identity grain again, the
    # table and the KPI count silently disagree — the table shows N rows and
    # the KPI counts the identities. Fail loudly here instead.
    if len(rows_all) != len({r["term_identity"] for r in rows_all}):
        raise SearchTermIdentityError(
            "flagged population has %d rows but %d durable identities — the "
            "aggregation grain and the identity grain have diverged"
            % (len(rows_all), len({r["term_identity"] for r in rows_all})))

    if review_state and not review_available:
        raise SearchTermQueryError(
            "review_state filter is unavailable — the local review store could "
            "not be read, so filtering by review state would return a silently "
            "wrong subset rather than an empty one")

    filtered = _filter_flagged_rows(rows_all, q=q, campaign=campaign,
                                    review_state=review_state,
                                    flag_reason=flag_reason,
                                    min_spend=min_spend, sql_state=sql_state)
    ordered = sorted(filtered, key=_flagged_sort_key(sort))

    # KPIs over the COMPLETE filtered population, never the returned page.
    kpi_identities = {r["term_identity"] for r in filtered}
    kpi_units = [u for u in flagged_units if unit_identity(u) in kpi_identities]
    mon = _monetary_summary(kpi_units)
    kpis = _flagged_kpis(filtered, mon, sql_attr,
                         review_available=review_available)

    total = len(ordered)
    offset = (page - 1) * page_size
    page_rows = [_full_row(unit_by_identity[r["term_identity"]])
                 for r in ordered[offset:offset + page_size]]

    return {
        **base,
        "view": "flagged",
        # Symmetric with the quarantine payload: one flag every consumer can
        # check instead of each re-deriving whether the data may be acted on.
        "actionable": True,
        "filters_enabled": True,
        # A review decision cannot be recorded against a store we cannot read.
        "review_actions_enabled": review_available,
        "kpis": kpis,
        "rows": page_rows,
        "pagination": {
            "total_count": total, "returned_count": len(page_rows),
            "page": page, "page_size": page_size,
            "has_more": offset + len(page_rows) < total,
        },
        "facets": _flagged_facets(rows_all),
        "filters": echo_filters,
        "platform_date_field": "source_date",
        "sql_date_field": "contact_created_at",
        "sql_attribution": _search_term_sql_block(sql_attr),
        "truth_state": truth_state,
        "annotation_join": annotation_join,
        "review_state_available": review_available,
        "canonical_fact_source": {
            "table": "search_terms",
            # PR-ADS-156-F3 §4: the ACCOUNT leads the declared grain, because it
            # leads the index. This payload told readers the key was
            # account-less while `idx_search_terms_unique_fact` had already been
            # rebuilt with `customer_id` — a contract describing a key that no
            # longer exists is worse than none, because people act on it.
            "grain": ("customer_id + source_date + campaign_name + campaign_id "
                      "+ ad_group + keyword + match_type + search_term"),
            "dedup_key": "idx_search_terms_unique_fact",
            "account_scoped": True,
            "unit_grain": "search_term × canonical campaign identity",
            "identity": "analysis/search_term_identity.term_identity_key",
        },
        "annotation_source": {
            "table": "waste_terms",
            "role": ("classification annotation only — reason, matched rule and "
                     "CRM-junk confirmation. Never a source of spend, clicks or "
                     "impressions."),
        },
        "governance": {
            "read_only": True,
            "google_ads_mutations": False,
            "negative_keywords_applied": False,
        },
        "audit": audit,
    }


def _flagged_population(start, end, population: str) -> tuple[dict, dict | None, str]:
    """``(population, sql_attribution, population_actually_built)`` for the
    flagged view. ``sql_attribution`` is None when the population is unavailable.

    The narrowed ``flagged_candidates`` population is sufficient for everything
    the flagged view publishes EXCEPT search-term SQL attribution once the CRM
    carries an exact user query: a contact is then attributed to a unit only
    when exactly ONE unit matches it, and the attributed share is counted over
    every unit — including ones that are not flagged. That cannot be decided
    from the candidates alone, so in that case the complete population is built
    instead and the response reports ``complete``. While no contact carries a
    query (``leads`` persists none today), every unit's attribution is decided
    by its own mapping status alone and the narrowed population changes nothing.
    """
    if population == POPULATION_FLAGGED_CANDIDATES:
        pop = _build_population(start, end, flagged_candidates=True)
        if not pop["available"]:
            return pop, None, population
        sql_attr = _search_term_sql_attribution(pop, start, end)
        if not sql_attr.get("population_has_text"):
            return pop, sql_attr, population
        del pop, sql_attr
    pop = _build_population(start, end)
    if not pop["available"]:
        return pop, None, POPULATION_COMPLETE
    return pop, _search_term_sql_attribution(pop, start, end), POPULATION_COMPLETE


def _quarantined_flagged_payload(base, pop, truth_state, echo_filters,
                                 page, page_size) -> dict[str, Any]:
    """A mismatch response: diagnosis only, never decision metrics.

    Every KPI is null and no row is returned, so there is nothing for the normal
    UI to render. The `quarantine` block names what broke and lists the affected
    terms for an operator, in a shape the KPI/table renderers do not read.

    ``actionable`` is False so every downstream consumer — filters, pagination,
    review buttons — can refuse in one check rather than each inventing its own
    rule.
    """
    units = pop.get("units") or []
    flagged = [u for u in units if _unit_state(u) == STATE_FLAGGED]
    unexplained = [u for u in flagged
                   if not (u.get("junk_categories")
                           or (u.get("_waste_evidence") or {}).get("junk_category"))]
    return {
        **base,
        "view": "flagged",
        "actionable": False,
        "kpis": _empty_flagged_kpis(),
        "rows": [],
        "pagination": {"total_count": None, "returned_count": 0,
                       "page": page, "page_size": page_size,
                       "has_more": False},
        # No facets: a filter built from quarantined data would imply the data
        # behind it can be explored.
        "facets": {"campaigns": [], "flag_reasons": [],
                   "review_states": list(LOCAL_REVIEW_STATES)},
        "filters": echo_filters,
        "filters_enabled": False,
        "review_actions_enabled": False,
        "truth_state": truth_state,
        "annotation_join": pop.get("annotation_join") or {},
        "review_state_available": False,
        "quarantine": {
            "reason": "internal_invariant_failed",
            "detail": ("Flagged terms carry no reason evidence, so the flagged "
                       "contract cannot be explained. Counts, rows and actions "
                       "are withheld rather than shown as normal values."),
            "truth_reasons": truth_state.get("reasons") or [],
            "affected_term_count": len(unexplained),
            # Diagnostic sample for an operator — deliberately NOT a row shape,
            # so it cannot be rendered as the normal table.
            "affected_terms_sample": sorted(
                {(u.get("search_term_normalized") or u.get("search_term") or "")
                 for u in unexplained})[:20],
        },
        "governance": {
            "read_only": True,
            "google_ads_mutations": False,
            "negative_keywords_applied": False,
        },
    }


def _fetch_reviews_for_units(units: list) -> tuple[dict, bool]:
    """Durable review rows for a set of canonical units, plus AVAILABILITY.

    Returns ``(rows_by_identity, available)``. The two are reported separately
    because an empty map is ambiguous on its own: it means both "nobody has
    reviewed anything" and "the review store could not be read". Collapsing
    them would let the page present an outage as a verified all-unreviewed
    state (PR-ADS-153D §32 — unavailable is never a value).

    On an outage NOTHING is inferred about any term's review state. Each row
    reports ``review_state: None``, ``review_state_status: "unavailable"`` and
    ``action_needed: None``; the ``never_reviewed`` priority component is not
    applied; the review-state filter is refused rather than returning a silently
    wrong subset; and review actions are disabled.

    Existing decisions are therefore neither reopened nor treated as
    ``unreviewed``. Defaulting an unreadable store to ``unreviewed`` would
    silently resurrect terms a human had already resolved or kept, and inventing
    ``keep`` would silently drop outstanding work — the flagged view instead
    says the review store could not be read.
    """
    try:
        from db import search_term_review_repository as review_repo  # noqa: PLC0415

        fetched = review_repo.fetch_reviews_for_identities(
            [unit_identity(u) for u in units])
        return (fetched.get("rows") or {}), bool(fetched.get("available"))
    except MemoryError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[search-term-evidence] review state unavailable: %s", exc)
        return {}, False


def _filter_flagged_rows(rows: list, *, q=None, campaign=None, review_state=None,
                         flag_reason=None, min_spend=None, sql_state=None) -> list:
    out = rows
    if q:
        needle = str(q).strip().lower()
        out = [r for r in out
               if needle in (r.get("search_term") or "").lower()
               or needle in (r.get("campaign_name") or "").lower()]
    if campaign:
        out = [r for r in out if r.get("campaign_key") == campaign
               or r.get("campaign_name") == campaign]
    if review_state:
        # Every row reports review_state None during an outage, so this filter
        # would return an empty list that reads as a real "no terms in that
        # state". The caller refuses the filter instead (see below).
        out = [r for r in out if r.get("review_state") == review_state]
    if flag_reason:
        out = [r for r in out if r.get("flag_reason") == flag_reason]
    if min_spend not in (None, ""):
        threshold = float(min_spend)
        # A unit whose spend is UNAVAILABLE is not proven to be below the
        # threshold, so it is never silently filtered out by one.
        out = [r for r in out if r.get("spend_usd") is None
               or float(r["spend_usd"]) >= threshold]
    if sql_state and sql_state != "all":
        if sql_state == "has_sql":
            out = [r for r in out if r.get("sql_attribution_status") == "attributed"
                   and (r.get("attributed_sqls") or 0) > 0]
        elif sql_state == "known_zero":
            out = [r for r in out if r.get("sql_attribution_status") == "known_zero"]
        elif sql_state == "unavailable":
            out = [r for r in out if r.get("sql_attribution_status")
                   in ("unavailable", "mapping_review", "partial_attribution")]
    return out


def _flagged_kpis(rows: list, mon: dict, sql_attr: dict,
                  *, review_available: bool = True) -> dict:
    """The four canonical flagged KPIs (§11). No vanity metrics.

    ``sql_evidence`` is the sum of SAFELY ATTRIBUTED lifecycle SQLs only. When
    no row has proven attribution it is ``None`` (Unavailable), never 0 — the
    distinction between "no attributable SQLs" and "we could not attribute" is
    the whole point of §13.
    """
    attributed = [r for r in rows if r.get("sql_attribution_status") == "attributed"]
    known_zero = [r for r in rows if r.get("sql_attribution_status") == "known_zero"]
    unavailable = [r for r in rows if r.get("sql_attribution_status")
                   not in ("attributed", "known_zero")]
    sql_evidence = (sum(int(r.get("attributed_sqls") or 0) for r in attributed)
                    if (attributed or known_zero) else None)
    return {
        # Unique durable identities currently matching the flagged contract.
        "flagged_terms": len({r["term_identity"] for r in rows}),
        # Canonical Google Ads spend for those terms, FX-verified subtotal only.
        "flagged_spend_usd": mon["verified_usd_spend"],
        "flagged_spend_native": mon["verified_native_spend"],
        "native_currency": mon["native_currency"],
        "reporting_currency": mon["reporting_currency"],
        "monetary": mon,
        "monetary_status": mon["monetary_completeness_status"],
        "clicks": sum(int(r.get("clicks") or 0) for r in rows),
        # Search-term-attributable lifecycle SQLs — a strict attribution SUBSET,
        # never a naked "SQLs" claim (§12).
        "sql_evidence": sql_evidence,
        "sql_evidence_label": "Search-term-attributable SQLs",
        "sql_evidence_available": bool(attributed or known_zero),
        "terms_with_attribution_unavailable": len(unavailable),
        "terms_with_proven_zero_sqls": len(known_zero),
        "sql_attribution_available": bool(sql_attr.get("available")),
        # Flagged terms with no finished human decision. None — not 0 — when
        # the review store is unreadable: "we could not check" is not "none".
        "review_needed": (sum(1 for r in rows if r.get("action_needed"))
                          if review_available else None),
        "review_state_status": ("available" if review_available else "unavailable"),
    }


def _empty_flagged_kpis() -> dict:
    return {
        "flagged_terms": None, "flagged_spend_usd": None,
        "flagged_spend_native": None, "native_currency": None,
        "reporting_currency": "USD", "monetary": None, "monetary_status": None,
        "clicks": None, "sql_evidence": None,
        "sql_evidence_label": "Search-term-attributable SQLs",
        "sql_evidence_available": False,
        "terms_with_attribution_unavailable": None,
        "terms_with_proven_zero_sqls": None,
        "sql_attribution_available": False,
        "review_needed": None,
        "review_state_status": "unavailable",
    }


def _flagged_facets(rows: list) -> dict:
    campaigns: dict = {}
    reasons: dict = {}
    for r in rows:
        key = r.get("campaign_key")
        if key and key not in campaigns:
            campaigns[key] = {"campaign_key": key,
                              "campaign_name": r.get("campaign_name"),
                              "mapping_status": r.get("mapping_status")}
        reason = r.get("flag_reason")
        if reason:
            reasons[reason] = {"reason": reason,
                               "reason_label": r.get("flag_reason_label")}
    return {
        "campaigns": sorted(campaigns.values(),
                            key=lambda c: (c["campaign_name"] or "").lower()),
        "flag_reasons": sorted(reasons.values(), key=lambda r: r["reason"]),
        "review_states": list(LOCAL_REVIEW_STATES),
    }


class _UnitRows(Sequence):
    """The rows of an export: ``_unit_row`` of each unit, in order, serialized
    WHEN READ (PR-ADS-173A).

    An export is the complete filtered population — never a page — so its rows
    used to be ≈ 100,000 full row dicts held at once to be written out one
    after another. This is the same sequence (same length, same rows, same
    order; indexable, sliceable, comparable to a list) that builds each row as
    the consumer reaches it, so a CSV writer holds one row at a time.
    """

    __slots__ = ("_units", "_aliases_by_id")

    def __init__(self, units: list, aliases_by_id: dict):
        self._units = units
        self._aliases_by_id = aliases_by_id

    def __len__(self) -> int:
        return len(self._units)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [_unit_row(u, self._aliases_by_id) for u in self._units[index]]
        return _unit_row(self._units[index], self._aliases_by_id)

    def __iter__(self):
        aliases_by_id = self._aliases_by_id
        for u in self._units:
            yield _unit_row(u, aliases_by_id)

    def __eq__(self, other) -> bool:
        if isinstance(other, (list, _UnitRows)):
            return len(self) == len(other) and all(
                a == b for a, b in zip(self, other))
        return NotImplemented

    __hash__ = None

    def __repr__(self) -> str:
        return f"<{len(self)} search-term export rows>"


def build_search_term_export(window: str, *, q=None, campaign=None, state=None,
                             junk_category=None, min_spend=None, sort="spend",
                             sql_state=None, now: datetime | None = None) -> dict[str, Any]:
    """COMPLETE server-filtered dataset for export (no pagination) — the export
    is the full filtered population at term×campaign grain, so it is never a
    silently truncated page. Read-only.

    PR-ADS-173A: ``rows`` is a ``_UnitRows`` sequence — every row, in order,
    serialized as it is read — rather than a list holding them all."""
    _validate_filters(state, sort, TERM_SORTS)
    if sql_state and sql_state not in TERM_SQL_STATES:
        raise SearchTermQueryError(
            f"Unsupported sql_state '{sql_state}'. Valid: {', '.join(TERM_SQL_STATES)}.")
    start, end, base = _base(window, now)
    pop = _build_population(start, end)
    if not pop["available"]:
        return {**base, "db_unavailable": True, "rows": [], "complete": False}
    _apply_search_term_sql(pop["units"], _search_term_sql_attribution(pop, start, end))
    filtered = _filter_units(pop["units"], q=q, campaign=campaign, state=state,
                             junk_category=junk_category, min_spend=min_spend)
    filtered = _sort_units(_filter_units_sql(filtered, sql_state), sort)
    return {**base, "db_unavailable": False, "complete": True,
            "rows": _UnitRows(filtered, pop["aliases_by_id"])}


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
    # Match on the normalized term so a drawer opened from any raw variant (or
    # an old bookmark carrying a different casing) resolves to the one unit.
    norm_term = normalize_search_term(term)

    # PR-ADS-173A — the drawer publishes ONE term. Everything below reads the
    # units of that normalized term only: the matches, the units it compares
    # labels with (units whose display term is this term — the same normalized
    # term by construction), and the annotation join, which is decided among
    # the units of one term. So only those units are loaded — every raw
    # spelling, every campaign — instead of the account's whole window.
    #
    # The one reader that needs more is search-term SQL attribution once the
    # CRM carries an exact user query: a contact is then attributed to a unit
    # only when exactly ONE unit of the population matches it. In that case
    # the complete population is built, as the flagged view does (172A).
    pop = _build_population(start, end, normalized_terms={norm_term})
    if not pop["available"]:
        return {**base, "db_unavailable": True, "term": None}

    def _matching(population):
        return [u for u in population["units"]
                if u["search_term_normalized"] == norm_term
                and (campaign_key is None or u["campaign_key"] == campaign_key)]

    matches = _matching(pop)
    if not matches:
        return {**base, "db_unavailable": False, "_not_found": True, "term": None}
    sql_attr = _search_term_sql_attribution(pop, start, end)
    if sql_attr.get("population_has_text"):
        del sql_attr, matches
        pop = _build_population(start, end)
        if not pop["available"]:
            return {**base, "db_unavailable": True, "term": None}
        matches = _matching(pop)
        if not matches:
            return {**base, "db_unavailable": False, "_not_found": True,
                    "term": None}
        sql_attr = _search_term_sql_attribution(pop, start, end)

    aliases_by_id = pop["aliases_by_id"]
    if len(matches) == 1:
        unit = matches[0]
    else:
        # No campaign_key given and the term spans campaigns — merge a combined
        # view (facts only; identity block lists every campaign separately).
        unit = {
            "search_term": term,
            "search_term_normalized": norm_term,
            # Union of every campaign's raw variants for this term.
            "search_term_variants": {v for m in matches
                                     for v in (m.get("search_term_variants") or set())},
            "campaign_key": None,
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

    # ── Identity-safety scoping (PR-ADS-144 §3, hardened) ──────────────────
    # waste_terms stores campaign_name but NOT campaign_id, and a null-id daily
    # row can be reached through a shared display name. So the drawer only uses
    # a name fallback when the selected unit's labels UNIQUELY identify one
    # campaign among all units for THIS search term; otherwise it scopes by
    # campaign_id alone and withholds name-derived classification proof.
    selected = matches[0] if len(matches) == 1 else None
    labels_unique = False
    if selected is not None:
        sel_labels = set(selected.get("source_labels") or set())
        others = [u for u in pop["units"]
                  if u["search_term"] == term
                  and u["campaign_key"] != selected["campaign_key"]]
        shares_label = any(sel_labels & set(o.get("source_labels") or set())
                           for o in others)
        labels_unique = bool(sel_labels) and not shares_label

    if selected is not None:
        ids = sorted(selected.get("campaign_ids") or set())
        names = sorted(selected.get("source_labels") or set())
        if ids:
            # id-first; null-id name fallback ONLY when the label uniquely
            # identifies this campaign (else another id shares the name).
            daily = st_repo.fetch_search_term_daily_for_campaign(
                start, end, term, campaign_id=ids[0],
                campaign_names=(names if labels_unique else None))
        else:
            # No-id (unmatched / legacy) unit → STRICTLY its null-id rows, so it
            # can never pull an id-bearing campaign that shares its display name.
            daily = st_repo.fetch_search_term_daily_for_campaign(
                start, end, term, campaign_id=None,
                campaign_names=(names or None), null_id_only=True)
    else:
        # Combined multi-campaign view (no campaign_key) → the term's full series.
        daily = st_repo.fetch_search_term_daily_for_campaign(start, end, term)

    # Classification proof — available ONLY for a single, unambiguously-named
    # campaign. When the label is shared (or no single campaign is selected, or
    # there is no safe label), the proof is withheld — NEVER borrowed from
    # another campaign and NEVER a search-term-only waste_terms query.
    classification_safe = selected is not None and labels_unique
    if classification_safe:
        classification = st_repo.fetch_latest_waste_classification(
            term, campaign_names=sorted(selected["source_labels"]))
    else:
        classification = {"available": True, "row": None}
    cls_row = classification.get("row") if classification.get("available") else None
    cls_proof_status = "available" if classification_safe else "unavailable"
    cls_proof_reason = None if classification_safe else (
        "campaign identity ambiguous in waste_terms (waste_terms stores "
        "campaign_name but not campaign_id; the selected campaign's label is "
        "not unique for this term)" if selected is not None
        else "no single campaign selected")

    campaigns_context = [{
        "campaign_key": m["campaign_key"],
        "campaign_name": m["campaign_name"],
        "mapping_status": m["mapping_status"],
        "aliases": _unit_row(m, aliases_by_id)["aliases"],
        "source_labels": sorted(m["source_labels"]),
        "spend_usd": _round2(m["spend_usd"]),
        "clicks": m["clicks"],
    } for m in matches]

    # §8 — HubSpot SQL attribution for this term. Direct persisted query only; the
    # CRM stores a keyword, not the user query, so this is normally unavailable.
    _apply_search_term_sql(matches, sql_attr)
    sql_block = _search_term_drawer_sql_block(matches, sql_attr)

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
            # state / junk / matched pattern come from the durable search_terms
            # unit itself (campaign-id-safe), so they are always correct for the
            # selected campaign. CRM-junk confirmations / date / source come from
            # waste_terms (name-only) and are withheld unless the label is
            # unambiguous for this campaign.
            "state": _unit_state(unit),
            "junk_categories": sorted(unit["junk_categories"]),
            "matched_patterns": sorted(unit["matched_patterns"]),
            "crm_junk_confirmed": (cls_row or {}).get("crm_junk_confirmed"),
            "classification_date": (cls_row or {}).get("run_date"),
            "classification_source": ("waste_terms (latest analysis run)"
                                      if cls_row else None),
            # Which durable source drove the state (PR-ADS-145 §4) + confidence.
            "state_source": _unit_classification(unit)["classification_source"],
            "confidence": _unit_classification(unit)["confidence"],
            "proof_status": cls_proof_status,
            "proof_reason": cls_proof_reason,
            "semantics": CLASSIFICATION_SEMANTICS,
        },
        "platform_activity": {
            "conversions": _round2(unit["conversions"]),
            "disclosure": PLATFORM_CONVERSION_DISCLOSURE,
        },
        "sql_attribution": sql_block,
        "daily": {
            "available": bool(daily.get("available")),
            "rows": _convert_daily_series(daily.get("rows") or [], start, end),
            "reporting_currency": (pop.get("currency_info") or {}).get(
                "reporting_currency", "USD"),
            "note": ("Only dates the source actually reported are shown — "
                     "missing dates are never fabricated as zero. Each day's "
                     "USD is converted at that day's own FX rate; days without "
                     "a rate show native only."),
        },
    }


def _search_term_drawer_sql_block(matches: list, attr: dict) -> dict:
    """HubSpot SQL attribution block for the search-term drawer. Explains plainly
    when term-level attribution is unavailable because the CRM record holds only a
    keyword, not the actual user query."""
    from services.platform_sql_attribution_service import (  # noqa: PLC0415
        contact_details_for_keys,
    )
    keys: list = []
    for m in matches:
        keys.extend(m.get("sql_contact_keys") or [])
    contacts = contact_details_for_keys(attr.get("contacts") or [], keys)
    statuses = {m.get("sql_attribution_status") for m in matches}
    if "attributed" in statuses:
        status = "attributed"
    elif "known_zero" in statuses:
        status = "known_zero"
    elif statuses == {"mapping_review"}:
        status = "mapping_review"
    else:
        status = "unavailable"
    count = sum(m.get("attributed_sqls") or 0 for m in matches) if status == "attributed" else None
    cov = attr.get("coverage") or {}
    exact_available = bool(attr.get("population_has_text"))
    explanation = None
    if status == "unavailable":
        explanation = ("Search-term-level SQL attribution is unavailable: the CRM "
                       "record stores only a HubSpot keyword, not the actual user "
                       "search query, so a qualified contact cannot be tied to an "
                       "exact search term. A keyword is never treated as a query.")
    return {
        "attributed_sqls": count,
        "sql_attribution_status": status,
        "sql_attribution_source": (matches[0].get("sql_attribution_source") if matches else None),
        "sql_attribution_coverage_pct": cov.get("coverage_pct"),
        "campaign_identity_status": (matches[0].get("mapping_status") if matches else None),
        "exact_query_evidence_available": exact_available,
        "explanation": explanation,
        "available": bool(attr.get("available")),
        "contacts": contacts,
        "contact_count": len(contacts),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Patterns tab (derived from the SAME population)
# ─────────────────────────────────────────────────────────────────────────────


class _SlotRecord:
    """A fixed-field record read and written like the dict it replaces
    (PR-ADS-173A): ``record["field"]``, ``record.get("field")``. An unknown
    field is a ``KeyError`` on read and on write, never a silent no-op."""

    __slots__ = ()

    def __getitem__(self, key):
        try:
            return getattr(self, key)
        except (AttributeError, TypeError):
            raise KeyError(key) from None

    def get(self, key, default=None):
        try:
            return getattr(self, key)
        except (AttributeError, TypeError):
            return default

    def __setitem__(self, key, value) -> None:
        try:
            setattr(self, key, value)
        except (AttributeError, TypeError):
            raise KeyError(key) from None


class _TermUnit(_SlotRecord):
    """One UNIQUE search term for pattern math — the per-term dict the Patterns
    tab has always aggregated, as slots. ``campaign_keys`` / ``campaign_names``
    are frozensets shared between terms that ran in the same campaigns. (The
    dict also kept a ``campaign_rows`` list of the term's units; nothing read
    it, and it pinned every unit of the population in memory for as long as
    the terms lived, so it is not carried.)"""

    __slots__ = ("search_term", "spend_usd", "clicks", "impressions",
                 "conversions", "any_flagged", "any_unreviewed", "any_waste_flag",
                 "has_unverified_spend", "last_seen", "campaign_keys",
                 "campaign_names")

    def __init__(self, search_term):
        self.search_term = search_term
        self.spend_usd = None
        self.clicks = 0
        self.impressions = 0
        self.conversions = None
        self.any_flagged = False
        self.any_unreviewed = False
        self.any_waste_flag = False
        self.has_unverified_spend = False
        self.last_seen = None
        self.campaign_keys = _EMPTY_FROZENSET
        self.campaign_names = _EMPTY_FROZENSET


def _term_units_for_patterns(units: list, *, q=None, campaign=None, state=None,
                             min_spend=None) -> list:
    """Apply the shared filters then unify to UNIQUE search-term units (a term
    contributes once to pattern math, no matter how many campaigns it ran in)."""
    return _unify_term_units(_filter_units(units, q=q, campaign=campaign,
                                           state=state, min_spend=min_spend))


def _unify_term_units(filtered: list) -> list:
    by_term: dict = {}
    shared: dict = {}
    for u in filtered:
        t = by_term.get(u["search_term"])
        if t is None:
            t = by_term[u["search_term"]] = _TermUnit(u["search_term"])
        # Only verified FX-complete USD contributes to pattern spend; an
        # unverified underlying unit flags the term's spend as partial rather
        # than counting a fabricated zero (PR-ADS-145 §6).
        if _unit_verified_usd(u):
            t.spend_usd = _sum_opt(t.spend_usd, u["spend_usd"])
        else:
            t.has_unverified_spend = True
        t.clicks += u["clicks"]
        t.impressions += u["impressions"]
        t.conversions = _sum_opt(t.conversions, u["conversions"])
        t.any_flagged = t.any_flagged or u["any_flagged"]
        t.any_waste_flag = t.any_waste_flag or bool(u.get("_waste_flag"))
        t.any_unreviewed = t.any_unreviewed or u["any_unreviewed"]
        ls = u["last_seen"]
        if ls is not None and (t.last_seen is None or ls > t.last_seen):
            t.last_seen = ls
        if u["campaign_key"] not in t.campaign_keys:
            merged = t.campaign_keys | {u["campaign_key"]}
            t.campaign_keys = shared.setdefault(merged, merged)
        if u["campaign_name"] and u["campaign_name"] not in t.campaign_names:
            merged = t.campaign_names | {u["campaign_name"]}
            t.campaign_names = shared.setdefault(merged, merged)
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


class _Pattern(_SlotRecord):
    """One n-gram's aggregate over the unique terms that contain it. (The dict
    this replaces also kept a ``_units`` list of those terms; nothing read
    it.)"""

    __slots__ = ("pattern", "n", "terms", "flagged_terms", "clean_terms",
                 "needs_review_terms", "spend_usd", "clicks", "conversions",
                 "verified_spend_terms", "unverified_spend_terms",
                 "campaign_keys", "campaign_names")

    def __init__(self, pattern, n):
        self.pattern = pattern
        self.n = n
        self.terms = 0
        self.flagged_terms = 0
        self.clean_terms = 0
        self.needs_review_terms = 0
        self.spend_usd = None
        self.clicks = 0
        self.conversions = None
        self.verified_spend_terms = 0
        self.unverified_spend_terms = 0
        self.campaign_keys = _EMPTY_FROZENSET
        self.campaign_names = _EMPTY_FROZENSET


def _term_grams(search_term: str, n: int) -> set:
    return {phrase for phrase, _ in build_ngrams(tokenize_search_term(search_term), {n})}


def _build_patterns(term_units: list, n: int) -> tuple[dict, dict]:
    """Aggregate patterns over unique term units. Returns (patterns, membership)
    where membership maps search_term → the distinct patterns it joined (a
    tuple of the patterns' own key strings, PR-ADS-173A)."""
    patterns: dict = {}
    membership: dict = {}
    shared: dict = {}
    for t in term_units:
        grams = _term_grams(t["search_term"], n)
        if not grams:
            continue
        # Inherit the corrected term state: durable-true OR safe waste_terms
        # evidence → flagged (PR-ADS-145 §6).
        st = (STATE_FLAGGED if (t["any_flagged"] or t.get("any_waste_flag"))
              else STATE_NEEDS_REVIEW if t["any_unreviewed"] else STATE_CLEAN)
        joined: list = []
        for phrase in grams:
            p = patterns.get(phrase)
            if p is None:
                p = patterns[phrase] = _Pattern(phrase, n)
            joined.append(p.pattern)
            p.terms += 1
            p.flagged_terms += 1 if st == STATE_FLAGGED else 0
            p.clean_terms += 1 if st == STATE_CLEAN else 0
            p.needs_review_terms += 1 if st == STATE_NEEDS_REVIEW else 0
            # Pattern spend aggregates only verified FX-complete term amounts;
            # a term with unverified underlying spend marks the pattern partial
            # (PR-ADS-145 §6) — never a fabricated zero.
            if t["spend_usd"] is not None:
                p.spend_usd = _sum_opt(p.spend_usd, t["spend_usd"])
                p.verified_spend_terms += 1
            if t.get("has_unverified_spend"):
                p.unverified_spend_terms += 1
            p.clicks += t["clicks"]
            p.conversions = _sum_opt(p.conversions, t["conversions"])
            if not p.campaign_keys.issuperset(t["campaign_keys"]):
                merged = p.campaign_keys.union(t["campaign_keys"])
                p.campaign_keys = shared.setdefault(merged, merged)
            if not p.campaign_names.issuperset(t["campaign_names"]):
                merged = p.campaign_names.union(t["campaign_names"])
                p.campaign_names = shared.setdefault(merged, merged)
        membership[t["search_term"]] = tuple(joined)
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
            **base, "db_unavailable": True,
            "unavailable_reason": pop.get("unavailable_reason"), "rows": [],
            "kpis": {"patterns_found": None, "terms_analysed": None,
                     "patterns_with_flagged": None,
                     "patterns_needing_review": None,
                     "reported_spend_represented_usd": None,
                     "spend_status": "unavailable"},
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
    # PR-ADS-173A: the facets and the audit block are the only other readers of
    # the units. Both are computed here, and the units are then released before
    # the patterns are aggregated instead of being held beside them.
    facets = _facets(pop["units"])
    audit = _audit_block(base, pop,
                         coverage_status=_coverage_block(
                             pop.get("currency_info") or _monetary_summary([]),
                             pop["canonical"])["status"])
    del pop
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
    overlapping_terms = 0
    for t, grams in membership.items():
        # A term's grams are distinct and each pattern is one row, so the
        # number of surviving ROWS a term belongs to is the number of its grams
        # that survived.
        joined = sum(1 for g in grams if g in surviving_patterns)
        if joined:
            surviving_terms.add(t)
            if joined > 1:
                overlapping_terms += 1
    contributing = [t for t in term_units
                    if t["search_term"] in surviving_terms]
    unique_spend = None
    for t in contributing:
        unique_spend = _sum_opt(unique_spend, t["spend_usd"])
    if unique_spend is None and not contributing:
        unique_spend = 0.0
    total_memberships = sum(p["terms"] for p in rows)

    # Whether the surviving pattern population has any unverified underlying
    # spend → the represented-spend KPI is a verified subtotal, not complete.
    kpi_partial = any(t.get("has_unverified_spend") for t in contributing)
    # When there is NO verified FX-complete spend at all (every contributing
    # term is unverified, so unique_spend stayed None), the represented-spend
    # KPI is Unavailable — never a null presented as merely "partial".
    if unique_spend is None:
        spend_status = MON_UNAVAILABLE
    elif kpi_partial:
        spend_status = MON_PARTIAL
    else:
        spend_status = MON_COMPLETE
    kpis = {
        "patterns_found": len(rows),
        "terms_analysed": len(contributing),
        "patterns_with_flagged": sum(1 for p in rows if p["flagged_terms"] > 0),
        "patterns_needing_review": sum(1 for p in rows
                                       if p["needs_review_terms"] > 0),
        # Verified FX-complete unique-underlying-term spend only.
        "reported_spend_represented_usd": _round2(unique_spend),
        "spend_status": spend_status,
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
        # Pattern spend is a verified subtotal when some underlying terms are
        # unverified — never a fabricated zero, never presented as complete.
        "spend_partial": p["unverified_spend_terms"] > 0,
        "verified_spend_terms": p["verified_spend_terms"],
        "unverified_spend_terms": p["unverified_spend_terms"],
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
        "facets": facets,
        "audit": {
            **audit,
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

    # PR-ADS-173A: membership depends on the term's text alone, so it is
    # decided per unit FIRST and only the member terms are unified — the same
    # members, in the same order, as unifying every term of the window and then
    # keeping the ones that contain the pattern.
    is_member: dict = {}
    member_units = []
    for u in _filter_units(pop["units"], q=q, campaign=campaign, state=state,
                           min_spend=min_spend):
        term_text = u["search_term"]
        hit = is_member.get(term_text)
        if hit is None:
            hit = is_member[term_text] = pattern in _term_grams(term_text, n)
        if hit:
            member_units.append(u)
    del pop, is_member
    members = _unify_term_units(member_units)
    del member_units
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
