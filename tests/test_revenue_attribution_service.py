"""
PR-ADS-108 — Revenue Attribution Durable Sources tests.

Validates the DB-first revenue-attribution contract:
  - durable DB sources (geo / leads / gclid_attribution) are used when available
  - local JSON missing does NOT mean empty if durable DB data exists
  - source_health diagnostics are present
  - "source missing" is distinct from "no revenue"
  - business windows drive the DB query date range
  - ROAS / CAC null safety; confidence labels; verdict classification
  - read-only (no external API / write calls); no Windsor wording
"""

import math
import os
import sys
from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.canonical_ledger_fixtures import (  # noqa: E402
    canonical_ledger_patch, from_legacy_deal_rows,
)

import services.revenue_attribution_service as svc
from services.revenue_attribution_service import (
    build_revenue_attribution,
    classify_verdict,
    compute_cac,
    compute_roas,
    confidence_from_tiers,
)

NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


# ── Durable DB fixtures ──────────────────────────────────────────────────────


def _spend_rows():
    return [
        {"campaign_name": "gulf", "country": "Saudi Arabia", "spend": 1000.0},
        {"campaign_name": "europa", "country": "France", "spend": 8000.0},
        {"campaign_name": "waste-campaign", "country": "Egypt", "spend": 300.0},
        {"campaign_name": "watchco", "country": "Jordan", "spend": 200.0},
    ]


def _lead_rows():
    return [
        {"campaign_name": "gulf", "country": "Saudi Arabia", "status_category": "qualified", "has_gclid": True},
        {"campaign_name": "gulf", "country": "Saudi Arabia", "status_category": "unknown", "has_gclid": False},
        {"campaign_name": "europa", "country": "France", "status_category": "qualified", "has_gclid": False},
        {"campaign_name": "watchco", "country": "Jordan", "status_category": "qualified", "has_gclid": False},
    ]


def _revenue_rows():
    return [
        # PR-ADS-153E-B: "matched" meant a GCLID matched a click, so the canonical
        # equivalent is a deal that actually carries a GCLID — which is what puts
        # it in the narrowest `gclid_attributable` scope.
        {"campaign_name": "gulf", "country": "Saudi Arabia", "deal_id": "d1", "deal_amount_usd": 10000.0, "match_status": "matched", "gclid": "gclid-d1"},
        {"campaign_name": "gulf", "country": "Saudi Arabia", "deal_id": "d2", "deal_amount_usd": 5000.0, "match_status": "url_fallback"},
        {"campaign_name": "europa", "country": "France", "deal_id": "d3", "deal_amount_usd": 2000.0, "match_status": "unmatched"},
    ]


def _canonical_from_spend(spend_rows):
    """Build a canonical campaign-spend payload mirroring geo spend per campaign.

    PR-ADS-118: Campaign ROAS is sourced from canonical Google Ads spend (never
    geo). Mirroring the geo totals into canonical — with a wide verified coverage
    chunk — lets these DB-path tests exercise ROAS under the new spend-truth
    contract (complete canonical coverage required).
    """
    by_camp = {}
    for r in spend_rows:
        name = r.get("campaign_name")
        by_camp[name] = by_camp.get(name, 0.0) + float(r.get("spend") or 0)
    rows = [{"campaign_id": str(i + 1), "campaign_name": name,
             "cost_micros": int(round(amt * 1_000_000)), "spend": amt}
            for i, (name, amt) in enumerate(by_camp.items())]
    total_micros = sum(r["cost_micros"] for r in rows)
    return {"available": True, "rows": rows, "total_cost_micros": total_micros,
            "total_spend": total_micros / 1_000_000, "campaign_count": len(rows),
            "customer_id": "C1", "currency_code": "USD",
            "coverage_start": "2000-01-01", "coverage_end": "2100-01-01"}


def total_or_zero(geo_total_obj):
    return int(geo_total_obj.get("total_cost_micros") or 0)


def _build_db(window="current_quarter", *, spend=None, leads=None, revenue=None,
              spend_cov=("2026-04-01", "2026-06-20"), revenue_cov=("2026-05-01", "2026-05-10"),
              canonical=None, coverage=None, geo_coverage=None):
    spend_rows = spend if spend is not None else _spend_rows()
    spend_obj = {"available": True, "rows": spend_rows,
                 "coverage_start": spend_cov[0], "coverage_end": spend_cov[1], "table": "geo"}
    leads_obj = {"available": True, "rows": leads if leads is not None else _lead_rows(),
                 "coverage_start": None, "coverage_end": None, "table": "leads",
                 "event_date_safe": True, "lead_event_date_field_available": True,
                 "missing_contact_created_at_count": 0,
                 "excluded_non_paid_count": 0, "excluded_pseudo_campaign_count": 0}
    revenue_obj = {"available": True, "rows": revenue if revenue is not None else _revenue_rows(),
                   "coverage_start": revenue_cov[0], "coverage_end": revenue_cov[1], "table": "gclid_attribution"}
    # Canonical spend mirrors geo so ROAS is verified from canonical, with a wide
    # verified coverage chunk (so any window is complete) unless overridden.
    canonical_obj = canonical if canonical is not None else _canonical_from_spend(spend_rows)
    coverage_obj = coverage if coverage is not None else {
        "available": True,
        "chunks": [{"chunk_start": "2000-01-01", "chunk_end": "2100-01-01", "status": "verified"}],
    }
    # PR-ADS-124: the canonical Google Ads geo table is the country spend SOURCE.
    # Mirror the geo spend into it (total + per-country) so the country table and
    # the reconciliation total come from the same canonical source.
    geo_total_obj, geo_country_obj = _geo_from_spend(spend_rows)
    # PR-ADS-153F: the durable geo coverage ledger is a MANDATORY input to the
    # country readiness gate — country spend is only trusted over a range that
    # was demonstrably fetched. Mirroring a wide verified chunk keeps these
    # DB-path fixtures describing a healthy account; pass `geo_coverage=[]` for
    # the never-fetched case.
    geo_coverage_obj = {"available": True, "chunks": (
        [{"chunk_start": "2000-01-01", "chunk_end": "2100-01-01", "status": "verified",
          "rows_written": 10, "cost_micros_total": total_or_zero(geo_total_obj)}]
        if geo_coverage is None else list(geo_coverage))}
    with patch("db.revenue_repository.fetch_geo_coverage", return_value=geo_coverage_obj), \
         patch("db.revenue_repository.fetch_campaign_country_spend", return_value=spend_obj), \
         patch("db.revenue_repository.fetch_lead_quality", return_value=leads_obj), \
         canonical_ledger_patch(from_legacy_deal_rows(revenue_obj.get("rows") or [])), \
         patch("db.revenue_repository.fetch_canonical_campaign_spend", return_value=canonical_obj), \
         patch("db.revenue_repository.fetch_spend_coverage", return_value=coverage_obj), \
         patch("db.revenue_repository.fetch_geo_daily_spend_total", return_value=geo_total_obj), \
         patch("db.revenue_repository.fetch_geo_daily_spend_by_country", return_value=geo_country_obj), \
         patch("db.revenue_repository.fetch_sync_state", return_value={"available": True, "datasets": {}}):
        return build_revenue_attribution(window, now=NOW)


def _geo_from_spend(spend_rows):
    """Mirror geo spend into the canonical geo table payloads (total + by-country).

    PR-ADS-124: ROAS by Country is sourced from the canonical Google Ads geo
    table. These payloads make canonical geo the active country source in the
    DB-path tests, with country names that join the legacy spend/deal countries.
    """
    by_country = {}
    for r in spend_rows:
        c = r.get("country")
        if not c:
            continue
        by_country[c] = by_country.get(c, 0.0) + float(r.get("spend") or 0)
    rows = [{"country_criterion_id": str(i + 1), "country_code": None,
             "country_name": country, "cost_micros": int(round(amt * 1_000_000)),
             "spend": amt, "spend_usd": None, "fx_complete": True}
            for i, (country, amt) in enumerate(by_country.items())]
    total_micros = sum(r["cost_micros"] for r in rows)
    total_obj = {"available": True, "has_rows": bool(rows),
                 "total_cost_micros": total_micros, "total_spend": total_micros / 1_000_000,
                 "rows_counted": len(rows), "country_count": len(rows),
                 "last_synced_at": "2026-06-30 10:00:00"}
    by_country_obj = {"available": True, "has_rows": bool(rows), "rows": rows,
                      "total_cost_micros": total_micros, "total_spend": total_micros / 1_000_000,
                      "total_spend_usd": None, "fx_complete": True,
                      "currency_code": "USD", "customer_id": "C1", "reporting_currency": "USD"}
    return total_obj, by_country_obj


def _db_unavailable():
    """Patch all repo functions to report the database is unavailable."""
    unavail = {"available": False, "rows": [], "coverage_start": None, "coverage_end": None}
    return (
        patch("db.revenue_repository.fetch_campaign_country_spend", return_value=dict(unavail, table="geo")),
        patch("db.revenue_repository.fetch_lead_quality", return_value=dict(unavail, table="leads")),
        canonical_ledger_patch([], available=False),
        patch("db.revenue_repository.fetch_sync_state", return_value={"available": False, "datasets": {}}),
    )


def _by(rows, field, value):
    for r in rows:
        if r.get(field) == value:
            return r
    return None


# ── DB path: contract + durable sourcing ─────────────────────────────────────


def test_db_path_used_when_database_available():
    result = _build_db()
    assert result["source_health"]["mode"] == "database"
    assert result["campaign_spend_source_status"] == "db"
    assert result["spend_source"] == "google_ads_api"
    # PR-ADS-153E-B: revenue provenance names the canonical ledger.
    assert result["revenue_source"] == "hubspot_deal_ledger"
    assert set(result["db_tables_used"]) == {"geo", "leads", "hubspot_deal_ledger"}


def test_local_json_missing_does_not_mean_empty_when_db_has_data():
    """No local JSON files exist in tests; DB data must still populate the dashboard."""
    result = _build_db()
    assert result["campaigns"], "campaigns should be populated from the DB"
    assert result["countries"], "countries should be populated from the DB"
    assert result["summary"]["spend"] == 9500


def test_db_campaign_rows():
    result = _build_db(window="all_time")  # no window filter ambiguity
    gulf = _by(result["campaigns"], "campaign_name", "gulf")
    assert gulf["spend"] == 1000
    assert gulf["leads"] == 2
    assert gulf["sqls"] == 1
    assert gulf["customers"] == 2
    assert gulf["won_revenue"] == 15000
    assert gulf["roas"] == 15.0
    assert gulf["confidence"] == "high"   # gclid + campaign scope -> tier_1 dominant
    assert gulf["verdict"] == "winner"


def test_db_country_rows_have_real_country_and_spend():
    result = _build_db(window="all_time")
    sa = _by(result["countries"], "country", "Saudi Arabia")
    assert sa["country_code"] == "SA"
    assert sa["spend"] == 1000           # real country spend from the geo table
    assert sa["top_campaign"] == "gulf"
    assert sa["verdict"] == "winner"
    assert result["country_spend_available"] is True
    assert result["geo_country_mapping_status"] == "available"


def test_db_summary():
    s = _build_db(window="all_time")["summary"]
    assert s["spend"] == 9500
    assert s["leads"] == 4
    assert s["sqls"] == 3
    assert s["customers"] == 3
    assert s["won_revenue"] == 17000


# ── source_health diagnostics ────────────────────────────────────────────────


def test_source_health_present_with_all_keys():
    sh = _build_db()["source_health"]
    for key in ("mode", "campaign_spend_status", "hubspot_contacts_status",
                "hubspot_deals_status", "attribution_status", "data_is_partial",
                "files_used", "db_tables_used", "warnings"):
        assert key in sh, f"source_health missing key: {key}"


def test_attribution_status_is_durable_gclid_when_revenue_present():
    result = _build_db()
    assert result["attribution_source_status"] == "hubspot_deal_ledger"
    assert result["source_health"]["attribution_status"] == "hubspot_deal_ledger"


# ── source-missing vs no-revenue ─────────────────────────────────────────────


def test_no_revenue_is_distinct_from_source_missing():
    """DB reachable + spend present but zero won deals = no-revenue, NOT missing."""
    result = _build_db(revenue=[])
    assert result["source_health"]["mode"] == "database"
    assert result["campaign_spend_source_status"] == "db"          # spend source present
    assert result["source_health"]["hubspot_deals_status"] == "db_empty"
    assert result["summary"]["won_revenue"] == 0
    assert result["campaigns"], "spend-only campaigns should still appear"


def test_source_unavailable_when_db_down_and_no_local_files():
    p1, p2, p3, p4 = _db_unavailable()
    with p1, p2, p3, p4:
        # No local JSON files exist in the test environment.
        result = build_revenue_attribution("current_quarter", now=NOW)
    assert result["source_health"]["mode"] == "source_unavailable"
    assert result["campaign_spend_source_status"] == "source_unavailable"
    # Exact missing dependency must be named.
    assert any("geo" in w and "ads_campaigns.json" in w for w in result["warnings"])


def test_local_json_fallback_labeled_when_db_down_but_files_present():
    p1, p2, p3, p4 = _db_unavailable()
    campaign_spend = {"rows": [{"campaign": "gulf", "date": "2026-05-01", "spend": 500}],
                      "status": "google_ads_api", "file": "data/ads_campaigns.json"}
    with p1, p2, p3, p4, \
         patch.object(svc, "_load_campaign_spend", return_value=campaign_spend), \
         patch.object(svc, "_load_geo_spend", return_value={"rows": [], "file": None}), \
         patch.object(svc, "_load_attributed_deals", return_value=[]), \
         patch.object(svc, "_load_contacts", return_value=[]), \
         patch.object(svc, "_attribution_source_status", return_value="hubspot_source_tags_only"):
        result = build_revenue_attribution("current_quarter", now=NOW)
    assert result["source_health"]["mode"] == "local_json_fallback"
    assert result["campaign_spend_source_status"] == "local_json_fallback"
    assert any("Database unavailable" in w for w in result["warnings"])


# ── Business window drives the DB date range ─────────────────────────────────


def test_business_window_passed_to_db_query():
    captured = {}

    def fake_spend(start, end):
        captured["start"] = start
        captured["end"] = end
        return {"available": True, "rows": [], "coverage_start": None, "coverage_end": None, "table": "geo"}

    with patch("db.revenue_repository.fetch_campaign_country_spend", side_effect=fake_spend), \
         patch("db.revenue_repository.fetch_lead_quality", return_value={"available": True, "rows": []}), \
         canonical_ledger_patch([]), \
         patch("db.revenue_repository.fetch_sync_state", return_value={"available": True, "datasets": {}}):
        build_revenue_attribution("current_quarter", now=NOW)
    assert captured["start"] == date(2026, 4, 1)   # Q2 start
    assert captured["end"] == date(2026, 6, 20)


def test_all_time_passes_none_start_to_db():
    captured = {}

    def fake_spend(start, end):
        captured["start"] = start
        return {"available": True, "rows": [], "coverage_start": None, "coverage_end": None, "table": "geo"}

    with patch("db.revenue_repository.fetch_campaign_country_spend", side_effect=fake_spend), \
         patch("db.revenue_repository.fetch_lead_quality", return_value={"available": True, "rows": []}), \
         canonical_ledger_patch([]), \
         patch("db.revenue_repository.fetch_sync_state", return_value={"available": True, "datasets": {}}):
        build_revenue_attribution("all_time", now=NOW)
    assert captured["start"] is None


def test_partial_data_flagged_when_canonical_coverage_incomplete():
    # PR-ADS-129: "partial" is driven by the canonical coverage LEDGER (missing
    # chunks), NOT the geo table's first-row date. Coverage chunks starting Apr 1
    # leave Jan–Mar of a YTD window uncovered → incomplete → partial.
    coverage = {"available": True, "chunks": [
        {"chunk_start": "2026-04-01", "chunk_end": "2100-01-01", "status": "verified"},
    ]}
    canonical = _canonical_from_spend(_spend_rows())
    canonical["coverage_start"] = "2026-04-04"
    result = _build_db(window="ytd", canonical=canonical, coverage=coverage)
    assert result["data_is_partial"] is True
    assert any("coverage incomplete" in w.lower() for w in result["warnings"])
    assert result["source_health"]["campaign_spend_coverage_status"] == "incomplete"


def test_verified_zero_before_first_spend_is_not_partial():
    # PR-ADS-129: first spend row starts Apr 4 but the whole YTD window is covered
    # by a VERIFIED coverage chunk (Jan–Apr fetched, verified zero) → coverage is
    # complete, NOT partial, and no spend-coverage warning is emitted.
    canonical = _canonical_from_spend(_spend_rows())
    canonical["coverage_start"] = "2026-04-04"
    result = _build_db(window="ytd", canonical=canonical)  # default wide verified chunk
    sh = result["source_health"]
    assert sh["campaign_spend_coverage_status"] == "complete"
    assert sh["campaign_spend_coverage_reason"] == "verified_zero_before_first_spend"
    assert not any("coverage incomplete" in w.lower() for w in result["warnings"])


def test_invalid_window_raises():
    with pytest.raises(ValueError):
        _build_db(window="60d")


# ── Pure helpers: null safety / confidence / verdict ─────────────────────────


def test_compute_roas_null_safety():
    assert compute_roas(1000, 0) is None
    assert compute_roas(0, 0) is None
    assert compute_roas(2000, 1000) == 2.0


def test_compute_cac_null_safety():
    assert compute_cac(1000, 0) is None
    assert compute_cac(0, 5) is None
    assert compute_cac(1000, 2) == 500.0


def test_no_infinity_or_nan_anywhere():
    result = _build_db(window="all_time")
    rows = result["campaigns"] + result["countries"] + [result["summary"]]
    for row in rows:
        for key in ("roas", "cac"):
            value = row.get(key)
            if value is not None:
                assert not math.isinf(value)
                assert not math.isnan(value)


def test_attribution_scope_confidence_mapping():
    # PR-ADS-153E-B: confidence is derived from the canonical attribution SCOPE
    # (analysis.revenue_scope), an ordered statement about how strong the
    # evidence is — not from `gclid_attribution.match_status`, which described
    # how a click id was joined to a campaign.
    assert svc._scope_to_tier("gclid_attributable") == "tier_1_gclid"
    assert svc._scope_to_tier("campaign_attributable") == "tier_2_source_tag"
    assert svc._scope_to_tier("google_ads_source") == "tier_3_spend_weighted"
    assert svc._scope_to_tier("all_source") == "tier_3_spend_weighted"
    assert svc._scope_to_tier(None) == "tier_3_spend_weighted"


def test_confidence_from_tiers_labels():
    assert confidence_from_tiers(["tier_1_gclid"]) == "high"
    assert confidence_from_tiers(["tier_2_source_tag"]) == "medium"
    assert confidence_from_tiers(["tier_3_spend_weighted"]) == "low"
    assert confidence_from_tiers([]) == "low"


def test_classify_verdict_all_branches():
    assert classify_verdict(1000, 5, 2, 5000, 5.0) == "winner"
    assert classify_verdict(1000, 3, 0, 0, None) == "watch"
    assert classify_verdict(1000, 0, 1, 200, 0.2) == "watch"
    assert classify_verdict(500, 0, 0, 0, None) == "waste"
    assert classify_verdict(10, 0, 0, 0, None) == "learning"
    assert classify_verdict(0, 0, 0, 0, None) == "learning"


def test_db_campaign_verdicts():
    result = _build_db(window="all_time")
    c = result["campaigns"]
    assert _by(c, "campaign_name", "gulf")["verdict"] == "winner"
    assert _by(c, "campaign_name", "europa")["verdict"] == "watch"
    assert _by(c, "campaign_name", "waste-campaign")["verdict"] == "waste"
    assert _by(c, "campaign_name", "watchco")["verdict"] == "watch"


def test_all_verdicts_in_allowed_set():
    result = _build_db(window="all_time")
    allowed = {"winner", "watch", "waste", "learning"}
    for row in result["campaigns"] + result["countries"]:
        assert row["verdict"] in allowed


# ── Read-only / no-Windsor guarantees ────────────────────────────────────────


def test_service_makes_no_external_or_write_calls():
    import inspect
    source = inspect.getsource(svc)
    for forbidden in ("import requests", "requests.post", "requests.put",
                      "requests.patch", "requests.delete", "google.ads",
                      "hubapi.com"):
        assert forbidden not in source
    assert "mutate" not in source.lower()


def test_repository_is_read_only():
    import inspect
    import db.revenue_repository as repo
    source = inspect.getsource(repo).upper()
    for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "TRUNCATE", "MUTATE"):
        assert forbidden not in source, f"repository must be read-only; found {forbidden}"


def test_no_windsor_wording_in_service_and_repository():
    import inspect
    import db.revenue_repository as repo
    assert "windsor" not in inspect.getsource(svc).lower()
    assert "windsor" not in inspect.getsource(repo).lower()
