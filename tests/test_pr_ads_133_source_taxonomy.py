"""
PR-ADS-133 — Revenue by Source Channel + Platform Taxonomy.

Turns Revenue by Source from a flat group summary into a group → channel →
platform attribution page, with a per-platform drawer proving the clients/deals
behind each platform.

Guarantees exercised:
  - the taxonomy classifier is exhaustive and deterministic;
  - only Google Ads / Paid Search is spend-connected + ROAS-eligible; every other
    source is revenue-only (never a fabricated $0 / 0.00x);
  - unknown / missing sources are Unclassified — never defaulted to Organic;
  - the nested summary aggregates leads/SQLs/customers/revenue per channel/platform
    and reconciles to the group totals;
  - the detail endpoint filters by group/channel/platform, is read-only, keeps a
    missing amount null and missing labels Unavailable;
  - the frontend renders the hierarchy + drawer with no undefined/null/fake-$0;
  - no Google Ads / HubSpot / budget / bid writes anywhere.
"""

import os
import sys
from datetime import date, datetime

import pytest

from tests.canonical_ledger_fixtures import (  # noqa: E402
    from_source_rows, patch_canonical_ledger,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
SERVER = open(os.path.join(ROOT, "api", "server.py"), encoding="utf-8").read()
REPO_SRC = open(os.path.join(ROOT, "db", "revenue_repository.py"), encoding="utf-8").read()
CSS = open(os.path.join(ROOT, "static", "styles.css"), encoding="utf-8").read()


def _at(d):
    return datetime.fromisoformat(d + "T12:00:00+00:00")


def _slice(hay, marker, span=2600):
    i = hay.find(marker)
    assert i != -1, f"{marker} not found"
    return hay[i:i + span]


def _js_fn(hay, marker):
    """The whole top-level JS function starting at ``marker`` (to its closing
    ``}`` at column 0) — robust to a body that grows, unlike a fixed span."""
    i = hay.find(marker)
    assert i != -1, f"{marker} not found"
    j = hay.find("\n}\n", i)
    assert j != -1, f"end of {marker} not found"
    return hay[i:j + 3]


def _repo_fn_body(name):
    i = REPO_SRC.find(f"def {name}")
    assert i != -1, f"{name} not found in revenue_repository.py"
    j = REPO_SRC.find("\ndef ", i + 1)
    return REPO_SRC[i:(j if j != -1 else len(REPO_SRC))]


# ══════════════════════════ 1. Classifier taxonomy ══════════════════════════

from analysis.source_classification import classify_source_taxonomy  # noqa: E402


@pytest.mark.parametrize("primary,detail,group,channel,platform", [
    ("Paid Search", "google", "google_ads", "paid_search", "google_ads"),
    ("Paid Search", "adwords", "google_ads", "paid_search", "google_ads"),
    ("Paid Social", "LinkedIn", "other_paid", "paid_social", "linkedin"),
    ("Paid Social", "Facebook", "other_paid", "paid_social", "meta"),
    ("Paid Social", "Instagram", "other_paid", "paid_social", "meta"),
    ("Paid Social", "Meta", "other_paid", "paid_social", "meta"),
    ("Paid Social", "mystery", "other_paid", "paid_social", "unknown_paid_social"),
    ("Organic Social", "LinkedIn", "organic", "organic_social", "linkedin"),
    ("Organic Social", "Facebook", "organic", "organic_social", "meta"),
    ("Organic Social", "unknown", "organic", "organic_social", "unknown_organic_social"),
    ("Organic Search", None, "organic", "organic_search_direct", "organic_search"),
    ("Direct Traffic", None, "organic", "organic_search_direct", "direct_traffic"),
    ("Referrals", None, "organic", "referrals_partners", "referrals"),
    ("Offline Sources", "Resellers", "organic", "referrals_partners", "resellers"),
    ("Offline Sources", "Referrals", "organic", "referrals_partners", "referrals"),
    ("Offline Sources", "Direct Email", "organic", "direct_email", "direct_email"),
    ("Direct Email", None, "organic", "direct_email", "direct_email"),
    ("Offline Sources", "SalesNash", "other_paid", "salesnash", "salesnash"),
    ("Offline Sources", "Events", "other_paid", "events", "events"),
    ("Email Marketing", "HubSpot", "other_paid", "email_marketing", "hubspot_email"),
    ("Other Campaigns", "utm blast", "other_paid", "other_campaigns", "utm"),
    ("Offline Sources", "Migration import", "offline", "imported_crm", "offline_migration"),
    ("Other Offline Sources", None, "offline", "imported_crm", "offline_migration"),
])
def test_taxonomy_rules(primary, detail, group, channel, platform):
    out = classify_source_taxonomy(primary, detail)
    assert out["source_group"] == group
    assert out["source_channel"] == channel
    assert out["source_platform"] == platform


@pytest.mark.parametrize("primary", ["", None, "  ", "tiktok ads", "podcast", "mystery"])
def test_missing_or_unknown_is_unclassified_not_organic(primary):
    out = classify_source_taxonomy(primary, None)
    assert out["source_group"] == "unclassified"
    assert out["source_channel"] == "needs_review"
    assert out["source_group"] != "organic"


def test_only_google_paid_search_is_spend_connected():
    g = classify_source_taxonomy("Paid Search", "google")
    assert g["spend_connected"] is True and g["period_ratio_available"] is True
    for primary, detail in [("Paid Social", "LinkedIn"), ("Organic Social", "Meta"),
                            ("Organic Search", None), ("Email Marketing", "HubSpot"),
                            ("Offline Sources", "SalesNash"), ("Referrals", None),
                            ("Offline Sources", "Migration"), ("mystery", None)]:
        out = classify_source_taxonomy(primary, detail)
        assert out["spend_connected"] is False, f"{primary}/{detail} must not be spend-connected"
        assert out["period_ratio_available"] is False, f"{primary}/{detail} must not be period-ratio-eligible"


def test_taxonomy_carries_labels_and_reason():
    out = classify_source_taxonomy("Organic Social", "LinkedIn")
    assert out["source_group_label"] == "Organic"
    assert out["source_channel_label"] == "Organic Social"
    assert out["source_platform_label"] == "LinkedIn"
    assert out["classification_reason"] == "hubspot_original_source_organic_social_linkedin"
    assert out["attribution_quality"] == "known"


# ══════════════════════════ 2. Nested backend summary ══════════════════════════

def _patch_sources(monkeypatch, *, lead_rows=None, revenue_rows=None, spend_rows=None):
    import db.revenue_repository as repo
    monkeypatch.setattr(repo, "fetch_source_leads",
                        lambda s, e: {"available": True, "rows": lead_rows or []})
    # PR-ADS-153E-B: won revenue is read from the canonical deal ledger.
    patch_canonical_ledger(monkeypatch, from_source_rows(revenue_rows or []))
    monkeypatch.setattr(repo, "fetch_campaign_country_spend",
                        lambda s, e: {"available": True, "rows": spend_rows or []})
    # PR-ADS-140: Google Ads source spend is the CANONICAL campaign spend truth (same
    # as the mart), not geo spend. Derive a verified truth from spend_rows (USD == sum)
    # so the spend/ROAS assertions hold; empty spend → source unavailable (never $0).
    import services.revenue_spend_truth_service as truth_svc
    _total = round(sum(float(r.get("spend") or 0) for r in (spend_rows or [])), 2)
    _truth = ({
        "state": "verified", "google_ads_spend_source": "canonical_campaign_daily_spend",
        "native_currency": "GBP", "native_spend": _total, "usd_spend": _total,
        "fx_status": "verified", "spend_coverage_status": "verified",
        "period_ratio_available": True, "geo_spend_used": False,
        "geo_spend_note": "Geo spend is diagnostic.",
    } if spend_rows else {
        "state": "source_unavailable", "google_ads_spend_source": "unavailable",
        "native_currency": "GBP", "native_spend": None, "usd_spend": None,
        "fx_status": "unavailable", "spend_coverage_status": "unavailable",
        "period_ratio_available": False, "geo_spend_used": False,
        "geo_spend_note": "Geo spend is diagnostic.",
    })
    monkeypatch.setattr(truth_svc, "build_google_ads_spend_truth",
                        lambda window, now=None: dict(_truth))
    import db.writers as w
    monkeypatch.setattr(w, "source_attribution_health_counts",
                        lambda: {"contacts_classified": 0, "deals_attributed": 0,
                                 "ambiguous_deals": 0, "unclassified_deals": 0})


def _group(out, key):
    return next(g for g in out["groups"] if g["group"] == key)


def _channel(group, key):
    return next(c for c in group["channels"] if c["channel"] == key)


def _platform(channel, key):
    return next(p for p in channel["platforms"] if p["platform"] == key)


def test_nested_group_channel_platform_aggregation(monkeypatch):
    from services.source_attribution_service import build_revenue_by_source
    _patch_sources(
        monkeypatch,
        lead_rows=[
            {"acquisition_group": "organic", "status_category": "qualified",
             "source_primary_raw": "Organic Social", "source_detail_raw": "LinkedIn"},
            {"acquisition_group": "organic", "status_category": "unknown",
             "source_primary_raw": "Organic Social", "source_detail_raw": "Facebook"},
        ],
        revenue_rows=[
            {"acquisition_group": "organic", "attribution_status": "attributed",
             "deal_amount_usd": 10000.0, "source_primary_raw": "Organic Social",
             "source_detail_raw": "LinkedIn"},
        ],
    )
    out = build_revenue_by_source("current_quarter", now=_at("2026-06-22"))
    org = _group(out, "organic")
    social = _channel(org, "organic_social")
    li = _platform(social, "linkedin")
    meta = _platform(social, "meta")
    # SQLs / customers / revenue aggregate per channel + platform.
    assert social["leads"] == 2 and social["sqls"] == 1 and social["closed_won_deals"] == 1
    assert social["won_revenue"] == 10000.0
    assert li["leads"] == 1 and li["sqls"] == 1 and li["won_revenue"] == 10000.0
    assert meta["leads"] == 1 and meta["sqls"] == 0 and meta["won_revenue"] == 0.0
    # Nested totals reconcile to the group total.
    assert org["leads"] == sum(c["leads"] for c in org["channels"])
    assert org["won_revenue"] == round(sum(c["won_revenue"] for c in org["channels"]), 2)


def test_google_gets_spend_roas_others_never_inherit(monkeypatch):
    from services.source_attribution_service import build_revenue_by_source
    _patch_sources(
        monkeypatch,
        revenue_rows=[
            {"acquisition_group": "google_ads", "attribution_status": "attributed",
             "deal_amount_usd": 5000.0, "source_primary_raw": "Paid Search",
             "source_detail_raw": "google"},
            {"acquisition_group": "other_paid", "attribution_status": "attributed",
             "deal_amount_usd": 2000.0, "source_primary_raw": "Paid Social",
             "source_detail_raw": "LinkedIn"},
        ],
        spend_rows=[{"spend": 2500.0}],
    )
    out = build_revenue_by_source("current_quarter", now=_at("2026-06-22"))
    g = _group(out, "google_ads")
    gp = _platform(_channel(g, "paid_search"), "google_ads")
    # PR-ADS-161E: the close-date ratio publishes as period revenue / ad spend;
    # attribution ROAS is never published (not one acquisition cohort).
    assert gp["spend"] == 2500.0 and gp["period_revenue_per_ad_spend"] == 2.0
    assert gp["roas"] is None
    assert gp["status"] == "Period revenue / ad spend available"
    # Paid Social LinkedIn never inherits Google spend / the period ratio.
    op = _group(out, "other_paid")
    li = _platform(_channel(op, "paid_social"), "linkedin")
    assert li["spend"] is None and li["period_revenue_per_ad_spend"] is None
    assert li["roas"] is None
    assert li["won_revenue"] == 2000.0
    assert li["status"] == "Revenue-only — no connected spend source"


def test_google_non_canonical_bucket_never_inherits_spend(monkeypatch):
    # PR-133 review: a row inside the Google Ads group that lacks raw source detail
    # falls into the group's "unspecified" bucket. It must NOT inherit Google's
    # spend/ROAS — only the canonical Paid Search → Google Ads bucket does.
    from services.source_attribution_service import build_revenue_by_source
    _patch_sources(
        monkeypatch,
        revenue_rows=[
            {"acquisition_group": "google_ads", "attribution_status": "attributed",
             "deal_amount_usd": 4000.0, "source_primary_raw": "Paid Search",
             "source_detail_raw": "google"},
            # google_ads group row with NO raw source → falls into unspecified.
            {"acquisition_group": "google_ads", "attribution_status": "attributed",
             "deal_amount_usd": 1000.0, "source_primary_raw": None, "source_detail_raw": None},
        ],
        spend_rows=[{"spend": 2000.0}],
    )
    out = build_revenue_by_source("current_quarter", now=_at("2026-06-22"))
    g = _group(out, "google_ads")
    # Group period ratio uses the whole group revenue; the canonical bucket ratio
    # uses ONLY its own won revenue / spend (4000 / 2000 = 2.0), not the group's
    # (5000/2000). Attribution ROAS is never published at any level (PR-ADS-161E).
    assert g["period_revenue_per_ad_spend"] == round(5000.0 / 2000.0, 2)
    assert g["roas"] is None
    canonical = _platform(_channel(g, "paid_search"), "google_ads")
    assert (canonical["spend"] == 2000.0
            and canonical["period_revenue_per_ad_spend"] == round(4000.0 / 2000.0, 2))
    assert canonical["roas"] is None
    unspecified_ch = _channel(g, "unspecified")
    assert (unspecified_ch["spend"] is None
            and unspecified_ch["period_revenue_per_ad_spend"] is None)
    assert unspecified_ch["roas"] is None
    up = _platform(unspecified_ch, "unspecified")
    assert up["spend"] is None and up["period_revenue_per_ad_spend"] is None
    assert up["roas"] is None
    assert up["status"] == "Needs review — source missing or unsafe"


def test_google_spend_null_when_source_unavailable(monkeypatch):
    # PR-133 follow-up (fix 3): when the Google spend source is unavailable / empty,
    # spend and ROAS are null (UI renders Unavailable) — never a fabricated $0.
    import db.revenue_repository as repo
    from services.source_attribution_service import build_revenue_by_source
    monkeypatch.setattr(repo, "fetch_source_leads", lambda s, e: {"available": True, "rows": []})
    patch_canonical_ledger(monkeypatch, from_source_rows([
        {"acquisition_group": "google_ads", "attribution_status": "attributed",
         "deal_amount_usd": 5000.0, "source_primary_raw": "Paid Search",
         "source_detail_raw": "google"}]))
    # Spend source reports unavailable.
    monkeypatch.setattr(repo, "fetch_campaign_country_spend",
                        lambda s, e: {"available": False, "rows": []})
    # PR-ADS-140: canonical Google Ads spend truth reports no source.
    import services.revenue_spend_truth_service as truth_svc
    monkeypatch.setattr(truth_svc, "build_google_ads_spend_truth", lambda window, now=None: {
        "state": "source_unavailable", "google_ads_spend_source": "unavailable",
        "native_currency": "GBP", "native_spend": None, "usd_spend": None,
        "fx_status": "unavailable", "spend_coverage_status": "unavailable",
        "period_ratio_available": False, "geo_spend_used": False, "geo_spend_note": "diagnostic"})
    import db.writers as w
    monkeypatch.setattr(w, "source_attribution_health_counts",
                        lambda: {"contacts_classified": 0, "deals_attributed": 0,
                                 "ambiguous_deals": 0, "unclassified_deals": 0})
    out = build_revenue_by_source("current_quarter", now=_at("2026-06-22"))
    g = _group(out, "google_ads")
    assert g["spend"] is None and g["roas"] is None   # never $0 / 0.00x
    assert g["period_revenue_per_ad_spend"] is None
    assert g["period_ratio_status"] == "unavailable_no_spend_source"
    gp = _platform(_channel(g, "paid_search"), "google_ads")
    assert gp["spend"] is None and gp["roas"] is None
    assert gp["period_revenue_per_ad_spend"] is None


def test_offline_platform_status_is_migration(monkeypatch):
    from services.source_attribution_service import build_revenue_by_source
    _patch_sources(
        monkeypatch,
        revenue_rows=[
            {"acquisition_group": "offline", "attribution_status": "attributed",
             "deal_amount_usd": 1000.0, "source_primary_raw": "Offline Sources",
             "source_detail_raw": "Migration import"},
        ],
    )
    out = build_revenue_by_source("current_quarter", now=_at("2026-06-22"))
    off = _group(out, "offline")
    pf = _platform(_channel(off, "imported_crm"), "offline_migration")
    assert pf["status"] == "Imported CRM records — no reliable source attribution"
    assert pf["spend"] is None and pf["roas"] is None
    assert pf["period_revenue_per_ad_spend"] is None


# ══════════════════════════ 3./4. (deleted) ══════════════════════════
# PR-ADS-168B deleted the Revenue by Source page, its drawer route
# (/api/revenue-performance/source-platform-detail → build_source_platform_detail
# + fetch_source_contact_details) and its renderers. The detail-endpoint and
# frontend hierarchy/drawer tests were about that code and went with it.


# ══════════════════════════ 5. Safety — no writes ══════════════════════════

def test_no_platform_writes_in_touched_files():
    files = [
        "analysis/source_classification.py",
        "services/source_attribution_service.py",
        "db/revenue_repository.py",
        "api/server.py",
        "static/app.js",
    ]
    for rel in files:
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read().lower()
        for forbidden in (".mutate(", "upload_offline", "offline_conversion",
                          "google_ads_upload", "upload_conversions",
                          "requests.post", "requests.put", "requests.patch", "requests.delete",
                          "set_budget", "set_bid", "pause_campaign", "enable_campaign",
                          "create_deal", "update_deal", "create_contact", "update_contact"):
            assert forbidden not in src, f"{rel} must not reference '{forbidden}'"


def test_taxonomy_build_does_not_touch_spend_or_fx_math():
    src = open(os.path.join(ROOT, "services", "source_attribution_service.py"),
              encoding="utf-8").read().lower()
    # The source page never recomputes FX, geo residual, or the mart denominator.
    for forbidden in ("fx_rate", "geo_residual", "country_residual",
                      "build_revenue_decision_mart", "cost_micros"):
        assert forbidden not in src, f"source service must not reference '{forbidden}'"
