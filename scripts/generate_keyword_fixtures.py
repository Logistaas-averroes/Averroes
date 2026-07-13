#!/usr/bin/env python3
"""Generate currency-truth-safe screenshot fixtures for the Keyword Evidence page.

Drives the REAL services.keyword_evidence_service with mocked durable
repositories so the fixtures reflect genuine per-source-date FX conversion,
latest-observed quality and factual review signals — never hand-authored
numbers. Writes scripts/keyword_evidence_fixtures.json (consumed by
scripts/screenshot_keywords.py).

The population is deliberately Partial (verified GBP units + 2 legacy
unverified-currency units + 1 FX-incomplete unit) and mixes strong/medium/weak
and unavailable quality so the screenshots show truthful states.

Usage: python scripts/generate_keyword_fixtures.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NOW = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)
WINDOW_START = date(2026, 6, 14)
WINDOW_END = date(2026, 7, 13)
GBP_USD = 1.27
FX_SKIP = date(2026, 7, 6)   # the ONE date with no FX rate → an FX-incomplete unit
CUST = "3059734490"

# (keyword, campaign, campaign_id, ad_group, ad_group_id, criterion_id, match,
#  native_gbp, clicks, impressions, conv, status, qs, exp_ctr, ad_rel, lp)
_KW = [
    ("freight forwarding software", "Brand - UK", "1", "Core Terms", "101", "1001", "BROAD", 412.36, 138, 3140, 6.0, "ENABLED", 9, "ABOVE_AVERAGE", "ABOVE_AVERAGE", "AVERAGE"),
    ("tms software for freight forwarders", "Brand - UK", "1", "Core Terms", "101", "1002", "PHRASE", 268.10, 74, 1180, 3.0, "ENABLED", 8, "ABOVE_AVERAGE", "AVERAGE", "AVERAGE"),
    ("cargowise alternative", "Competitor - Global", "3", "Competitor", "301", "3001", "EXACT", 154.20, 47, 610, 2.0, "ENABLED", 7, "AVERAGE", "AVERAGE", "AVERAGE"),
    ("freight software", "Gulf", "2", "Generic", "201", "2001", "BROAD", 122.45, 61, 2230, 1.0, "ENABLED", 4, "BELOW_AVERAGE", "AVERAGE", "BELOW_AVERAGE"),
    ("freight forwarder dubai", "Gulf", "2", "Generic", "201", "2002", "PHRASE", 96.42, 52, 1745, 2.0, "ENABLED", 5, "AVERAGE", "AVERAGE", "AVERAGE"),
    ("logistics software", "Competitor - Global", "3", "Competitor", "301", "3002", "BROAD", 77.85, 38, 1290, 1.0, "ENABLED", 3, "BELOW_AVERAGE", "BELOW_AVERAGE", "AVERAGE"),
    ("best tms 2026", "Brand - UK", "1", "Core Terms", "101", "1003", "EXACT", 73.47, 29, 402, 1.0, "ENABLED", 8, "ABOVE_AVERAGE", "AVERAGE", "AVERAGE"),
    ("shipping software small business", "Brand - UK", "1", "Longtail", "102", "1004", "PHRASE", 62.99, 22, 618, 0.0, "ENABLED", 6, "AVERAGE", "AVERAGE", "AVERAGE"),
    ("freight forwarding erp", "Competitor - Global", "3", "Competitor", "301", "3003", "PHRASE", 56.07, 12, 240, 1.0, "ENABLED", 7, "AVERAGE", "ABOVE_AVERAGE", "AVERAGE"),
    ("cargo software", "Gulf", "2", "Generic", "201", "2003", "BROAD", 48.51, 24, 980, 0.0, "PAUSED", 4, "BELOW_AVERAGE", "AVERAGE", "AVERAGE"),
    ("what is freight forwarding", "Gulf", "2", "Generic", "201", "2004", "BROAD", 15.75, 9, 502, 0.0, "ENABLED", None, None, None, None),  # quality unavailable
    ("logistaas login", "Brand - UK", "1", "Brand", "103", "1005", "EXACT", 5.46, 15, 96, 0.0, "ENABLED", 10, "ABOVE_AVERAGE", "ABOVE_AVERAGE", "ABOVE_AVERAGE"),
]

# FX-incomplete unit: verified GBP currency, but its only reported cost lands on
# the ONE window date without an FX rate → USD withheld for THIS unit only.
_FX_INCOMPLETE = ("freight forwarding qatar", "Brand - UK", "1", "Longtail", "102", "1006", "PHRASE", 33.20, 14, 355, 1.0, "ENABLED", 6, "AVERAGE", "AVERAGE", "AVERAGE")

# Legacy units with no provable currency lineage — preserved, monetary withheld.
_LEGACY = [
    ("freight software legacy", "Old Windsor Camp", None, "Legacy", None, "9001", "BROAD", 4, 120),
    ("cargo forwarding legacy", "Old Windsor Camp", None, "Legacy", None, "9002", "BROAD", 4, 96),
]


def _agg_rows():
    rows = []
    for (kw, camp, cid, ag, agid, crit, mt, gbp, clk, imp, conv, status, qs, ectr, adr, lp) in _KW + [_FX_INCOMPLETE]:
        # One verified keyword has UNAVAILABLE platform conversions (NULL) to
        # exercise the "Platform conversions unavailable" signal + partial
        # conversion coverage — never shown as a fabricated 0.
        conv_val = None if crit == "1004" else float(conv)
        rows.append({
            "customer_id": CUST, "campaign_id": cid, "campaign_name": camp,
            "ad_group_id": agid, "ad_group_name": ag, "criterion_id": crit,
            "keyword_text": kw, "match_type": mt, "criterion_status": status,
            "cost_micros": int(round(gbp * 1_000_000)),
            "clicks": clk, "impressions": imp, "conversions": conv_val,
            "first_seen": WINDOW_START, "last_seen": date(2026, 7, 12),
            "fact_rows": 20, "currency_codes": ["GBP"],
            "source_systems": ["google_ads_api"],
            "quality_score": qs, "expected_ctr": ectr, "ad_relevance": adr,
            "landing_page_experience": lp,
            "quality_observed_at": "2026-07-13T05:30:00Z" if qs is not None else None,
        })
    for (kw, camp, cid, ag, agid, crit, mt, clk, imp) in _LEGACY:
        rows.append({
            "customer_id": CUST, "campaign_id": cid, "campaign_name": camp,
            "ad_group_id": agid, "ad_group_name": ag, "criterion_id": crit,
            "keyword_text": kw, "match_type": mt, "criterion_status": "ENABLED",
            "cost_micros": 2_000_000, "clicks": clk, "impressions": imp,
            "conversions": 0.0, "first_seen": WINDOW_START,
            "last_seen": date(2026, 7, 9), "fact_rows": 6,
            "currency_codes": [], "source_systems": [],
            "quality_score": None, "expected_ctr": None, "ad_relevance": None,
            "landing_page_experience": None, "quality_observed_at": None,
        })
    return rows


def _fetch_keyword_aggregates(start, end):
    rows = _agg_rows()
    return {"available": True, "rows": rows, "source": {
        "fact_rows": sum(r["fact_rows"] for r in rows),
        "unique_criteria": len(rows),
        "cost_micros_total": sum(r["cost_micros"] or 0 for r in rows),
        "clicks_total": sum(r["clicks"] for r in rows),
        "impressions_total": sum(r["impressions"] for r in rows),
        "conversions_total": sum(r["conversions"] or 0 for r in rows),
        "distinct_source_dates": 30,
        "min_source_date": WINDOW_START, "max_source_date": date(2026, 7, 12),
        "currency_codes": ["GBP"], "source_systems": ["google_ads_api"]}}


def _fetch_keyword_daily_costs(start, end):
    rows = []
    for (kw, camp, cid, ag, agid, crit, mt, gbp, *_r) in _KW:
        rows.append({"customer_id": CUST, "campaign_id": cid, "ad_group_id": agid,
                     "criterion_id": crit, "source_date": date(2026, 7, 12),
                     "cost_micros": int(round(gbp * 1_000_000)),
                     "currency_codes": ["GBP"], "source_systems": ["google_ads_api"]})
    # FX-incomplete unit reports ONLY on the date with no FX rate.
    (kw, camp, cid, ag, agid, crit, mt, gbp, *_r) = _FX_INCOMPLETE
    rows.append({"customer_id": CUST, "campaign_id": cid, "ad_group_id": agid,
                 "criterion_id": crit, "source_date": FX_SKIP,
                 "cost_micros": int(round(gbp * 1_000_000)),
                 "currency_codes": ["GBP"], "source_systems": ["google_ads_api"]})
    return {"available": True, "rows": rows}


def _fetch_keyword_daily_for_criterion(start, end, *, criterion_id, campaign_id=None,
                                       ad_group_id=None, customer_id=None):
    match = next((k for k in _KW if k[5] == criterion_id), None)
    if not match:
        return {"available": True, "rows": []}
    gbp = match[7]
    out = []
    for i, dd in enumerate([7, 8, 9, 10, 11, 12]):
        frac = [0.12, 0.20, 0.14, 0.18, 0.24, 0.12][i]
        out.append({"source_date": date(2026, 7, dd).isoformat(),
                    "cost_micros": int(round(gbp * frac * 1_000_000)),
                    "clicks": int(match[8] * frac), "impressions": int(match[9] * frac),
                    "conversions": 0.0, "currency_codes": ["GBP"],
                    "source_systems": ["google_ads_api"]})
    return {"available": True, "rows": out}


def _fx_rates(start, end, base, quote):
    rates = {}
    d = start or WINDOW_START
    while d <= end:
        if d != FX_SKIP:
            rates[d.isoformat()] = GBP_USD
        d += timedelta(days=1)
    return {"available": True, "rates": rates, "provider": "test"}


def _canonical(start, end):
    return {"available": True, "customer_id": CUST, "currency_code": "GBP",
            "reporting_currency": "USD", "fx_complete": True, "fx_missing_days": 0,
            "total_spend": 2600.0, "total_spend_usd": 3302.0, "campaign_count": 3,
            "rows": [
                {"campaign_id": "1", "campaign_name": "Brand - UK"},
                {"campaign_id": "2", "campaign_name": "Gulf"},
                {"campaign_id": "3", "campaign_name": "Competitor - Global"}]}


def _identity(customer_id=None):
    return {"available": True, "mappings": [
        {"external_campaign_label": "brand - uk", "campaign_id": "1",
         "match_method": "manual", "canonical_campaign_name": "Brand - UK"},
        {"external_campaign_label": "gulf", "campaign_id": "2",
         "match_method": "exact_normalized", "canonical_campaign_name": "Gulf"},
        {"external_campaign_label": "competitor - global", "campaign_id": "3",
         "match_method": "manual", "canonical_campaign_name": "Competitor - Global"}]}


def main() -> int:
    import db.keyword_repository as kr
    import db.revenue_repository as rr
    import services.keyword_evidence_service as svc

    with patch.object(kr, "fetch_keyword_aggregates", _fetch_keyword_aggregates), \
         patch.object(kr, "fetch_keyword_daily_costs", _fetch_keyword_daily_costs), \
         patch.object(kr, "fetch_keyword_daily_for_criterion", _fetch_keyword_daily_for_criterion), \
         patch.object(rr, "fetch_canonical_campaign_spend", _canonical), \
         patch.object(rr, "fetch_campaign_identity", _identity), \
         patch.object(rr, "fetch_fx_rates", _fx_rates):
        listing = svc.build_keyword_evidence("30d", now=NOW)
        drawer_rich = svc.build_keyword_drawer("30d", "1|101|1001", now=NOW)
        drawer_qna = svc.build_keyword_drawer("30d", "2|201|2004", now=NOW)  # quality unavailable

    base_path = os.path.join(ROOT, "scripts", "campaign_evidence_fixtures.json")
    base = json.load(open(base_path, encoding="utf-8"))
    fixtures = {
        "/auth/me": base["/auth/me"],
        "/api/config/ui-thresholds": base["/api/config/ui-thresholds"],
        "/api/datasets/freshness": {"datasets": [{
            "source": "google_ads_api", "dataset": "keyword_facts",
            "canonical_status": "fresh_with_data", "severity": "ok",
            "rows_in_window": 296, "latest_source_date": "2026-07-12",
            "status": "success", "last_successful_sync_at": "2026-07-13T05:30:00Z",
            "stale_threshold_days": 3, "reason": "Synced this morning.",
            "next_action": None}],
            "summary": {"total": 1, "success": 1, "failed": 0, "running": 0, "unknown": 0},
            "canonical_summary": {"fresh_with_data": 1}, "db_unavailable": False},
        "/api/keyword-evidence": listing,
        "/api/keyword-evidence/detail": {
            "1|101|1001": drawer_rich,
            "2|201|2004": drawer_qna,
        },
    }
    out_path = os.path.join(ROOT, "scripts", "keyword_evidence_fixtures.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(fixtures, fh, indent=1, default=str)
    print(f"wrote {out_path}")
    print("KPIs:", json.dumps(listing["kpis"]["monetary"], default=str))
    print("keywords_reported:", listing["kpis"]["keywords_reported"],
          "| verified:", listing["kpis"]["verified_spend_usd"],
          "| status:", listing["kpis"]["monetary_status"])
    print("quality_evidence:", listing["kpis"]["quality_evidence"])
    sigs = {}
    for r in listing["rows"]:
        sigs[r["review_signal"]] = sigs.get(r["review_signal"], 0) + 1
    print("signals:", sigs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
