#!/usr/bin/env python3
"""Generate currency-truth-safe screenshot fixtures for the Search Terms page.

Builds the /api/search-term-evidence family payloads by driving the REAL
services.search_term_evidence_service with mocked durable repositories, so the
fixtures reflect genuine per-source-date FX conversion (GBP native → USD at a
per-date rate) rather than hand-authored numbers. Writes
scripts/search_term_evidence_fixtures.json (consumed by
scripts/screenshot_search_terms.py).

Usage: python scripts/generate_search_term_fixtures.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NOW = datetime(2026, 7, 12, 9, 0, tzinfo=timezone.utc)
WINDOW_START = date(2026, 6, 13)
WINDOW_END = date(2026, 7, 12)
GBP_USD = 1.27   # single per-date rate across the window (kept constant here)

# (term, campaign_name, campaign_id, native_gbp, clicks, impressions, conv,
#  flagged, unreviewed, junk, pattern)
_TERMS = [
    ("freight forwarding software", "brand - uk", "1", 412.36, 118, 2140, 6.0, False, False, None, None),
    ("tms software for freight forwarders", "brand - uk", "1", 268.10, 64, 980, 3.0, False, False, None, None),
    ("freight software free download", "gulf", "2", 96.42, 41, 1230, 1.0, True, False, "free_seeker", "free"),
    ("freight forwarder jobs dubai", "gulf", "2", 88.75, 52, 1745, 2.0, True, False, "job_seeker", "jobs"),
    ("cargowise alternative", "competitor - global", "3", 154.20, 37, 610, 2.0, False, False, None, None),
    ("logistics course online", "gulf", "2", 61.30, 28, 890, 1.0, True, False, "student", "course"),
    ("best tms for freight forwarders 2026", "brand - uk", "1", 57.85, 19, 402, 1.0, False, True, None, None),
    ("shipping software small business", "brand - uk", "1", 49.60, 22, 618, 0.0, False, True, None, None),
    ("freight forwarding erp", "competitor - global", "3", 44.15, 12, 240, 1.0, False, False, None, None),
    ("what is freight forwarding", "gulf", "2", 12.40, 9, 502, 0.0, False, True, None, None),
    ("logistaas login", "brand - uk", "1", 4.30, 15, 96, 0.0, False, False, None, None),
    ("freight forwarding software qatar", "mena expansion", None, 38.20, 14, 355, 1.0, False, True, None, None),
]


def _agg_rows():
    rows = []
    for (term, camp, cid, gbp, clicks, impr, conv, flagged, unrev, junk, pat) in _TERMS:
        rows.append({
            "search_term": term, "campaign_name": camp, "campaign_id": cid,
            "spend_usd": gbp,  # legacy column = native at ingestion
            "cost_micros": int(round(gbp * 1_000_000)),
            "currency_codes": ["GBP"], "source_systems": ["google_ads_api"],
            "clicks": clicks, "impressions": impr, "conversions": conv,
            "row_count": 3, "first_seen": WINDOW_START, "last_seen": date(2026, 7, 11),
            "any_flagged": flagged, "any_unreviewed": unrev,
            "junk_categories": [junk] if junk else [],
            "matched_patterns": [pat] if pat else [],
            "ad_groups": ["Core"], "keywords": [term], "match_types": ["broad"],
        })
    return rows


def _agg():
    rows = _agg_rows()
    return {"available": True, "rows": rows, "source": {
        "row_count": sum(r["row_count"] for r in rows),
        "spend_usd_total": round(sum(r["spend_usd"] for r in rows), 2),
        "cost_micros_total": sum(r["cost_micros"] for r in rows),
        "clicks_total": sum(r["clicks"] for r in rows),
        "impressions_total": sum(r["impressions"] for r in rows),
        "conversions_total": sum(r["conversions"] for r in rows),
        "distinct_source_dates": 28,
        "min_source_date": WINDOW_START, "max_source_date": date(2026, 7, 11),
        "currency_codes": ["GBP"], "source_systems": ["google_ads_api"]}}


def _daily_costs(start, end):
    # One native-cost row per term on the window-end date (covered by FX below).
    rows = []
    for (term, camp, cid, gbp, *_rest) in _TERMS:
        rows.append({"search_term": term, "campaign_name": camp,
                     "campaign_id": cid, "source_date": end,
                     "cost_micros": int(round(gbp * 1_000_000)),
                     "currency_codes": ["GBP"], "source_systems": ["google_ads_api"]})
    return {"available": True, "rows": rows}


def _fx_rates(start, end, base, quote):
    rates = {}
    d = start or WINDOW_START
    while d <= end:
        rates[d.isoformat()] = GBP_USD
        d += timedelta(days=1)
    return {"available": True, "rates": rates, "provider": "test"}


def _canonical(start, end):
    # Canonical campaign USD, FX-complete → coverage computable.
    return {"available": True, "customer_id": "c1", "currency_code": "GBP",
            "reporting_currency": "USD", "fx_complete": True, "fx_missing_days": 0,
            "total_spend": 2140.0, "total_spend_usd": 3444.80, "campaign_count": 3,
            "rows": [
                {"campaign_id": "1", "campaign_name": "Brand - UK",
                 "spend": 1010.0, "spend_usd": 1282.7, "fx_complete": True},
                {"campaign_id": "2", "campaign_name": "Gulf",
                 "spend": 640.0, "spend_usd": 812.8, "fx_complete": True},
                {"campaign_id": "3", "campaign_name": "Competitor - Global",
                 "spend": 490.0, "spend_usd": 622.3, "fx_complete": True}]}


def _identity(customer_id=None):
    return {"available": True, "mappings": [
        {"external_campaign_label": "brand - uk", "campaign_id": "1",
         "match_method": "manual", "canonical_campaign_name": "Brand - UK"},
        {"external_campaign_label": "gulf", "campaign_id": "2",
         "match_method": "exact_normalized", "canonical_campaign_name": "Gulf"},
        {"external_campaign_label": "competitor - global", "campaign_id": "3",
         "match_method": "manual", "canonical_campaign_name": "Competitor - Global"}]}


def _daily_for_campaign(start, end, term, campaign_id=None, campaign_names=None):
    match = next((t for t in _TERMS if t[0] == term), None)
    if not match:
        return {"available": True, "rows": []}
    gbp = match[3]
    # Six reported dates within the window, each with GBP native + cost_micros.
    out = []
    for i, dd in enumerate([5, 6, 7, 9, 10, 11]):
        frac = [0.10, 0.22, 0.12, 0.19, 0.25, 0.12][i]
        out.append({"source_date": f"2026-07-{dd:02d}",
                    "spend_usd": round(gbp * frac, 2),
                    "cost_micros": int(round(gbp * frac * 1_000_000)),
                    "currency_codes": ["GBP"], "source_systems": ["google_ads_api"],
                    "clicks": int(match[4] * frac), "impressions": int(match[5] * frac),
                    "conversions": 0.0})
    return {"available": True, "rows": out}


def _classification(term, campaign_id=None, campaign_names=None):
    if term == "freight forwarder jobs dubai":
        return {"available": True, "row": {
            "search_term": term, "campaign_name": "gulf",
            "junk_category": "job_seeker", "matched_pattern": "jobs",
            "crm_junk_confirmed": 3, "run_date": "2026-07-10"}}
    return {"available": True, "row": None}


def main() -> int:
    import db.revenue_repository as rr
    import db.search_term_repository as st_repo
    import services.search_term_evidence_service as svc

    with patch.object(st_repo, "fetch_search_term_aggregates", lambda s, e: _agg()), \
         patch.object(st_repo, "fetch_search_term_daily_costs", _daily_costs), \
         patch.object(st_repo, "fetch_search_term_daily_for_campaign", _daily_for_campaign), \
         patch.object(st_repo, "fetch_latest_waste_classification", _classification), \
         patch.object(rr, "fetch_canonical_campaign_spend", _canonical), \
         patch.object(rr, "fetch_campaign_identity", _identity), \
         patch.object(rr, "fetch_fx_rates", _fx_rates):
        terms = svc.build_search_term_evidence("30d", now=NOW)
        term_drawer = svc.build_search_term_drawer(
            "30d", "freight forwarder jobs dubai", campaign_key="2", now=NOW)
        patterns = svc.build_search_pattern_evidence("30d", n=1, now=NOW)
        pattern_drawer = svc.build_search_pattern_drawer("30d", "freight", 1, now=NOW)

    base_path = os.path.join(ROOT, "scripts", "campaign_evidence_fixtures.json")
    base = json.load(open(base_path, encoding="utf-8"))
    fixtures = {
        "/auth/me": base["/auth/me"],
        "/api/config/ui-thresholds": base["/api/config/ui-thresholds"],
        "/api/datasets/freshness": {"datasets": [{
            "source": "google_ads_api", "dataset": "search_terms",
            "canonical_status": "fresh_with_data", "severity": "ok",
            "rows_in_window": terms["audit"]["window_end"] and 36,
            "latest_source_date": "2026-07-11", "status": "success",
            "last_successful_sync_at": "2026-07-12T05:30:00Z",
            "stale_threshold_days": 3, "reason": "Synced this morning.",
            "next_action": None}],
            "summary": {"total": 1, "success": 1, "failed": 0, "running": 0, "unknown": 0},
            "canonical_summary": {"fresh_with_data": 1}, "db_unavailable": False},
        "/api/search-term-evidence": terms,
        "/api/search-term-evidence/term": term_drawer,
        "/api/search-term-evidence/patterns": patterns,
        "/api/search-term-evidence/patterns/detail": pattern_drawer,
    }
    out_path = os.path.join(ROOT, "scripts", "search_term_evidence_fixtures.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(fixtures, fh, indent=1, default=str)
    print(f"wrote {out_path}")
    print("terms KPIs:", json.dumps(terms["kpis"], default=str))
    print("sample row spend_usd/native/fx:",
          {k: terms["rows"][0][k] for k in
           ("spend_usd", "spend_native", "native_currency", "fx_complete")})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
