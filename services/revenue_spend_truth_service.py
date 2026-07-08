"""
Canonical Google Ads spend-truth helper (PR-ADS-140)

A single low-level helper that derives the canonical Google Ads campaign-daily
spend truth for a business window. Both the Revenue Decision Mart and Revenue by
Source read Google Ads spend from THIS helper, so Revenue by Source can never
show a different Google Ads spend number than the mart's canonical top-line.

Before this helper, Revenue by Source took its Google Ads spend from the geo
(country) table (repo.fetch_campaign_country_spend), while the mart used the
canonical campaign-daily spend. The two disagreed — the page said one all-time
spend at the top and a smaller geo-derived number in the Google Ads section. That
split-brain is exactly what this helper removes.

Doctrine (never loosened):
  - Canonical Google Ads campaign-daily spend is the ONLY source-level Google Ads
    spend denominator. The geo/country table is diagnostic and is NEVER used here.
  - Native currency is GBP; USD reporting spend is shown ONLY when FX is complete.
  - No fake $0. Missing/unsafe coverage or FX yields Unavailable (None).
  - ROAS is withheld whenever the USD denominator is not FX-safe.
  - Read-only. No writes to Google Ads or HubSpot.

Import safety: this helper imports build_revenue_attribution (the shared
revenue-attribution contract) — NOT the Revenue Decision Mart. The mart imports
build_revenue_by_source, so importing the mart here would be circular; importing
the lower-level attribution contract is not.
"""

from __future__ import annotations

from datetime import datetime

from services.revenue_attribution_service import build_revenue_attribution

GEO_SPEND_NOTE = (
    "Geo spend is diagnostic and is not used as the source-level Google Ads "
    "denominator."
)

# Spend-truth states for the Google Ads source group. Each maps to a distinct,
# honest presentation state — never a fabricated $0 / 0.00x.
STATE_VERIFIED = "verified"                       # USD spend > 0 + ROAS available
STATE_VERIFIED_ZERO = "verified_zero_spend"       # verified $0 spend — ROAS unavailable
STATE_FX_INCOMPLETE = "fx_incomplete"             # native GBP exists; USD/ROAS withheld
STATE_COVERAGE_INCOMPLETE = "coverage_incomplete"  # canonical coverage unverified
STATE_SOURCE_UNAVAILABLE = "source_unavailable"    # no canonical Google Ads spend source


def _round2(value):
    """Round to 2 dp, passing None straight through (never fabricate a 0)."""
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def derive_google_ads_spend_truth(source_health: dict | None) -> dict:
    """Derive the canonical Google Ads spend-truth proof block from source_health.

    Pure — no I/O. Mirrors the mart's spend_truth derivation (same source_health
    fields) so the two contracts can never diverge, scoped to exactly what the
    Revenue by Source page needs to prove its Google Ads spend number.
    """
    health = source_health or {}

    spend_source = health.get("spend_source")
    canonical = spend_source == "canonical_google_ads_api"
    native_currency = (health.get("spend_native_currency")
                       or health.get("canonical_currency") or "GBP")
    native_total = _round2(health.get("spend_native_total"))
    usd_total = _round2(health.get("spend_usd_total"))
    roas_available_raw = bool(health.get("campaign_roas_available"))

    # FX coverage -> verified | incomplete | unavailable (never silently OK).
    fx_cov = health.get("fx_coverage_status")
    if fx_cov == "complete":
        fx_status = "verified"
    elif fx_cov == "incomplete":
        fx_status = "incomplete"
    else:  # not_evaluated | unavailable | None
        fx_status = "unavailable"

    # Canonical campaign spend coverage: complete only when the durable ledger
    # verifies the whole window (PR-ADS-129). Unknown/incomplete → not verified,
    # so ROAS stays withheld (never assumed complete, never a fake $0).
    campaign_cov = health.get("campaign_spend_coverage_status")
    spend_cov = health.get("spend_coverage_status")
    coverage_complete = campaign_cov == "complete" or (
        campaign_cov is None and spend_cov == "complete")

    if not canonical or native_total is None:
        state = STATE_SOURCE_UNAVAILABLE
    elif not coverage_complete:
        state = STATE_COVERAGE_INCOMPLETE
    elif fx_status != "verified":
        state = STATE_FX_INCOMPLETE
    elif not usd_total or usd_total <= 0:
        # Verified coverage + verified FX, but the window genuinely spent $0. ROAS
        # has no denominator, so it is a distinct honest state — never a "ROAS
        # available" label sitting next to an unavailable ROAS.
        state = STATE_VERIFIED_ZERO
    else:
        state = STATE_VERIFIED

    _verified_states = (STATE_VERIFIED, STATE_VERIFIED_ZERO)
    # USD spend is exposed when the denominator is fully FX-safe — including a
    # verified $0 (a real, verified zero, matching the mart's top-line usd_spend).
    usd_spend = usd_total if state in _verified_states else None
    # Native GBP evidence is kept for the FX-safe states AND fx-incomplete (so the
    # page can still prove native GBP spend when USD/ROAS are withheld). For an
    # unverified coverage / missing source, even native is Unavailable (never a
    # partial total dressed up as complete).
    native_spend = (native_total
                    if state in _verified_states + (STATE_FX_INCOMPLETE,) else None)

    if state == STATE_COVERAGE_INCOMPLETE:
        spend_coverage_status = "incomplete"
    elif state == STATE_SOURCE_UNAVAILABLE:
        spend_coverage_status = "unavailable"
    else:  # verified / verified_zero / fx_incomplete — canonical coverage complete
        spend_coverage_status = "verified"

    return {
        "state": state,
        "google_ads_spend_source": (
            "canonical_campaign_daily_spend" if canonical else "unavailable"),
        "native_currency": native_currency,
        "native_spend": native_spend,
        "usd_spend": usd_spend,
        "fx_status": fx_status,
        "spend_coverage_status": spend_coverage_status,
        # ROAS is available ONLY with a positive USD denominator — a verified $0
        # window has no ROAS (divide-by-zero), so roas_available is False there.
        "roas_available": roas_available_raw and state == STATE_VERIFIED,
        # The geo/country table is never the source-level Google Ads denominator.
        "geo_spend_used": False,
        "geo_spend_note": GEO_SPEND_NOTE,
    }


def build_google_ads_spend_truth(window: str, now: datetime | None = None) -> dict:
    """Canonical Google Ads spend truth for a window (reads the shared contract).

    Reads the SAME shared revenue-attribution contract the Revenue Decision Mart
    reads, so the Google Ads spend number matches the mart exactly.

    Raises ValueError for an unsupported window (propagated from the resolver).
    """
    core = build_revenue_attribution(window, now=now)
    return derive_google_ads_spend_truth(core.get("source_health") or {})
