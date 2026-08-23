"""
PR-ADS-118 — Google Ads Spend Truth Audit, Backfill, and Canonical Spend Repair.

Proves: cost_micros aggregation with no early rounding; idempotent upsert dedup;
window-correct canonical spend; PARTIAL (never $0) for missing chunks; Campaign
ROAS uses canonical (not geo); Country ROAS unavailable on geo/canonical
variance; zero-spend days inside a verified chunk allowed; missing chunks not
zero; customer id/currency preserved; no Google Ads writes; revenue unchanged;
revenue-page chrome stays hidden.

Service/DB tests skip gracefully where psycopg2 is absent (CI has it). None of
these import the google-ads SDK (the connector is reached via a patched seam).
"""

import os
import sys
from datetime import date, datetime, timezone

import pytest

from tests.canonical_ledger_fixtures import (  # noqa: E402
    from_legacy_deal_rows, patch_canonical_ledger,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
SERVER = open(os.path.join(ROOT, "api", "server.py"), encoding="utf-8").read()
SCHEMA = open(os.path.join(ROOT, "db", "schema.py"), encoding="utf-8").read()
WRITERS = open(os.path.join(ROOT, "db", "writers.py"), encoding="utf-8").read()
SPEND_SRC = open(os.path.join(ROOT, "services", "google_ads_spend_service.py"), encoding="utf-8").read()
CONN_SRC = open(os.path.join(ROOT, "connectors", "google_ads_direct.py"), encoding="utf-8").read()


def _at(d):
    return datetime.fromisoformat(d + "T12:00:00+00:00")


def _load_spend():
    try:
        import services.google_ads_spend_service as svc
        return svc
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"spend service import unavailable: {exc}")


# ════════════ 1. cost_micros aggregation: no early rounding ════════════


def test_no_early_rounding_in_aggregation(monkeypatch):
    svc = _load_spend()
    # 3 days × 333_333 micros = 999_999 micros = 0.999999 — must NOT round to 1.0.
    rows = [{"customer_id": "C1", "currency_code": "USD", "campaign_id": "1",
             "campaign_name": "a", "spend_date": f"2026-01-0{i}", "cost_micros": 333_333}
            for i in (1, 2, 3)]
    monkeypatch.setattr(svc, "fetch_daily_spend",
                        lambda a, b: {"customer_id": "C1", "rows": rows,
                                      "source_query_version": "v1"})
    out = svc.run_google_ads_spend_backfill(
        date_from="2026-01-01", date_to="2026-01-31", dry_run=True, chunk_months=1)
    assert out["summary"]["api_total_micros"] == 999_999
    assert out["summary"]["api_total_spend"] == 0.999999  # exact, not 1.0


# ════════════ 2. idempotent upsert — no double counting ════════════


def test_upsert_is_idempotent_by_unique_key():
    assert "ON CONFLICT (customer_id, campaign_id, spend_date) DO UPDATE" in WRITERS
    assert "UNIQUE (customer_id, campaign_id, spend_date)" in SCHEMA
    # Coverage upsert is keyed by chunk too.
    assert "ON CONFLICT (customer_id, chunk_start, chunk_end) DO UPDATE" in WRITERS


# ════════════ 3. window-correct canonical spend (Q1 → YTD/All Time) ════════════


def test_canonical_spend_windowed_by_spend_date():
    # The canonical read filters by spend_date over the window bounds.
    repo = open(os.path.join(ROOT, "db", "revenue_repository.py"), encoding="utf-8").read()
    fn = repo[repo.find("def fetch_canonical_campaign_spend"):repo.find("def fetch_spend_coverage")]
    assert "spend_date >= %s" in fn and "spend_date <= %s" in fn
    assert "google_ads_campaign_daily_spend" in fn


def test_audit_passes_window_bounds(monkeypatch):
    svc = _load_spend()
    seen = {}

    def cap(start, end):
        seen[(start, end)] = True
        return {"available": True, "rows": [], "total_cost_micros": 0,
                "campaign_count": 0, "customer_id": "C1", "currency_code": "USD",
                "coverage_start": None, "coverage_end": None}

    monkeypatch.setattr("db.revenue_repository.fetch_canonical_campaign_spend", cap)
    monkeypatch.setattr("db.revenue_repository.fetch_spend_coverage", lambda s, e, *_a, **_k: {"available": True, "chunks": []})
    monkeypatch.setattr("db.revenue_repository.fetch_geo_spend_total", lambda s, e: {"available": True, "total_spend": 0.0})
    monkeypatch.setattr(svc, "fetch_daily_spend", lambda a, b: {"rows": []})
    svc.build_google_ads_spend_audit("current_quarter", now=_at("2026-06-22"))
    svc.build_google_ads_spend_audit("ytd", now=_at("2026-06-22"))
    cq = (date(2026, 4, 1), date(2026, 6, 22))
    ytd = (date(2026, 1, 1), date(2026, 6, 22))
    assert cq in seen and ytd in seen and cq != ytd


# ════════════ 4 & 8. missing/failed chunk → PARTIAL, never $0 ════════════


def _patch_audit(monkeypatch, *, canonical=None, coverage=None, geo=None, api_rows=None):
    svc = _load_spend()
    monkeypatch.setattr("db.revenue_repository.fetch_canonical_campaign_spend",
                        lambda s, e, *_a, **_k: canonical or {"available": False, "rows": []})
    monkeypatch.setattr("db.revenue_repository.fetch_spend_coverage",
                        lambda s, e, *_a, **_k: {"available": True, "chunks": coverage or []})
    monkeypatch.setattr("db.revenue_repository.fetch_geo_spend_total",
                        lambda s, e: {"available": True, "total_spend": geo if geo is not None else 0.0})
    monkeypatch.setattr(svc, "fetch_daily_spend", lambda a, b: {"rows": api_rows or []})
    return svc


def test_missing_chunk_is_partial_not_zero(monkeypatch):
    # Local canonical has Q2 spend only; Q1 chunk was never fetched. YTD window.
    canonical = {"available": True, "rows": [{"campaign_id": "1", "campaign_name": "a",
                 "cost_micros": 19_100_000, "spend": 19.1}],
                 "total_cost_micros": 19_100_000, "total_spend": 19.1, "campaign_count": 1,
                 "customer_id": "C1", "currency_code": "USD",
                 "coverage_start": "2026-04-01", "coverage_end": "2026-06-22"}
    coverage = [{"chunk_start": "2026-04-01", "chunk_end": "2026-06-22", "status": "verified"}]
    svc = _patch_audit(monkeypatch, canonical=canonical, coverage=coverage, geo=19.1)
    out = svc.build_google_ads_spend_audit("ytd", now=_at("2026-06-22"))
    assert out["state"] == "PARTIAL"
    # The observed partial spend is shown — never silently zeroed.
    assert out["canonical_local_total"] == 19.1
    assert out["missing_chunks"], "missing Q1 range must be reported"


def test_failed_chunk_is_partial(monkeypatch):
    canonical = {"available": True, "rows": [], "total_cost_micros": 0, "total_spend": 0.0,
                 "campaign_count": 0, "customer_id": "C1", "currency_code": "USD",
                 "coverage_start": None, "coverage_end": None}
    coverage = [{"chunk_start": "2026-04-01", "chunk_end": "2026-06-22", "status": "failed"}]
    svc = _patch_audit(monkeypatch, canonical=canonical, coverage=coverage, geo=0.0, api_rows=[])
    out = svc.build_google_ads_spend_audit("current_quarter", now=_at("2026-06-22"))
    assert out["state"] == "PARTIAL"


# ════════════ 7. zero-spend days inside a verified chunk are allowed ════════════


def test_zero_spend_days_in_verified_chunk_allowed(monkeypatch):
    # Verified chunk covers the whole window but spend is genuinely 0 → VERIFIED.
    canonical = {"available": True, "rows": [], "total_cost_micros": 0, "total_spend": 0.0,
                 "campaign_count": 0, "customer_id": "C1", "currency_code": "USD",
                 "coverage_start": "2026-04-01", "coverage_end": "2026-06-22"}
    coverage = [{"chunk_start": "2026-04-01", "chunk_end": "2026-06-22", "status": "verified"}]
    svc = _patch_audit(monkeypatch, canonical=canonical, coverage=coverage, geo=0.0, api_rows=[])
    out = svc.build_google_ads_spend_audit("current_quarter", now=_at("2026-06-22"))
    assert out["state"] == "VERIFIED"


def test_coverage_analysis_distinguishes_missing_from_zero():
    svc = _load_spend()
    start, end = date(2026, 1, 1), date(2026, 3, 31)
    # Full verified coverage with zero spend → complete.
    full = svc.analyze_coverage(start, end, [{"chunk_start": "2026-01-01",
                                              "chunk_end": "2026-03-31", "status": "verified"}])
    assert full["complete"] is True
    # No chunks at all → NOT complete (missing, never treated as zero).
    none = svc.analyze_coverage(start, end, [])
    assert none["complete"] is False and none["missing_days"] == (end - start).days + 1


# ════════════ 5, 6, 9, 11. ROAS consumption via revenue-attribution ════════════


def _load_revattr():
    try:
        from services.revenue_attribution_service import build_revenue_attribution
        return build_revenue_attribution
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"service import unavailable: {exc}")


def _patch_revattr(monkeypatch, *, canonical, geo_rows, coverage, revenue_rows):
    leads = {"available": True, "rows": [], "event_date_safe": True,
             "lead_event_date_field_available": True, "missing_contact_created_at_count": 0,
             "excluded_non_paid_count": 0, "excluded_pseudo_campaign_count": 0,
             "coverage_start": None, "coverage_end": None}
    spend = {"available": True, "rows": geo_rows, "coverage_start": None, "coverage_end": None}
    revenue = {"available": True, "rows": revenue_rows, "coverage_start": None, "coverage_end": None}
    try:
        monkeypatch.setattr("db.revenue_repository.fetch_campaign_country_spend", lambda s, e: spend)
        monkeypatch.setattr("db.revenue_repository.fetch_lead_quality", lambda s, e: leads)
        monkeypatch.setattr("db.revenue_repository.fetch_won_revenue", lambda s, e: revenue)
        # PR-ADS-153E-B: closed-won revenue is read from the canonical deal
        # ledger, so the same fixture rows are stubbed there.
        patch_canonical_ledger(monkeypatch, from_legacy_deal_rows(revenue["rows"]))
        monkeypatch.setattr("db.revenue_repository.fetch_sync_state", lambda: {"available": True, "datasets": {}})
        monkeypatch.setattr("db.revenue_repository.revenue_integration_connected", lambda: True)
        monkeypatch.setattr("db.revenue_repository.fetch_canonical_campaign_spend", lambda s, e, *_a, **_k: canonical)
        monkeypatch.setattr("db.revenue_repository.fetch_spend_coverage", lambda s, e, *_a, **_k: {"available": True, "chunks": coverage})
    except (ImportError, AttributeError) as exc:
        pytest.skip(f"runtime deps unavailable: {exc}")


def test_campaign_roas_uses_canonical_not_geo(monkeypatch):
    build = _load_revattr()
    canonical = {"available": True, "rows": [{"campaign_id": "1", "campaign_name": "alpha",
                 "cost_micros": 5_000_000, "spend": 5.0}],
                 "total_cost_micros": 5_000_000, "total_spend": 5.0, "campaign_count": 1,
                 "customer_id": "C1", "currency_code": "USD",
                 "coverage_start": "2026-04-01", "coverage_end": "2026-06-22"}
    coverage = [{"chunk_start": "2026-04-01", "chunk_end": "2026-06-22", "status": "verified"}]
    # Geo says 99 — must NOT be used for campaign spend.
    geo_rows = [{"campaign_name": "alpha", "country": "US", "spend": 99.0}]
    revenue_rows = [{"campaign_name": "alpha", "country": "US", "deal_id": "d1",
                     "deal_amount_usd": 10000.0, "match_status": "matched"}]
    _patch_revattr(monkeypatch, canonical=canonical, geo_rows=geo_rows,
                   coverage=coverage, revenue_rows=revenue_rows)
    out = build("current_quarter", now=_at("2026-06-22"))
    camp = next(c for c in out["campaigns"] if c["campaign_name"] == "alpha")
    assert camp["spend"] == 5.0, "campaign spend must come from canonical, not geo (99)"
    assert out["summary"]["spend"] == 5.0
    assert out["source_health"]["spend_source"] == "canonical_google_ads_api"


def test_country_roas_unavailable_on_variance(monkeypatch):
    build = _load_revattr()
    canonical = {"available": True, "rows": [{"campaign_id": "1", "campaign_name": "alpha",
                 "cost_micros": 5_000_000, "spend": 5.0}],
                 "total_cost_micros": 5_000_000, "total_spend": 5.0, "campaign_count": 1,
                 "customer_id": "C1", "currency_code": "USD",
                 "coverage_start": "2026-04-01", "coverage_end": "2026-06-22"}
    coverage = [{"chunk_start": "2026-04-01", "chunk_end": "2026-06-22", "status": "verified"}]
    geo_rows = [{"campaign_name": "alpha", "country": "US", "spend": 99.0}]  # far from 5.0
    revenue_rows = [{"campaign_name": "alpha", "country": "US", "deal_id": "d1",
                     "deal_amount_usd": 10000.0, "match_status": "matched"}]
    _patch_revattr(monkeypatch, canonical=canonical, geo_rows=geo_rows,
                   coverage=coverage, revenue_rows=revenue_rows)
    out = build("current_quarter", now=_at("2026-06-22"))
    assert out["source_health"]["country_spend_reconciled"] is False
    # Customer id / currency are preserved (test 9).
    assert out["source_health"]["canonical_customer_id"] == "C1"
    assert out["source_health"]["canonical_currency"] == "USD"


def test_revenue_unchanged_by_spend_source(monkeypatch):
    build = _load_revattr()
    canonical = {"available": True, "rows": [{"campaign_id": "1", "campaign_name": "alpha",
                 "cost_micros": 5_000_000, "spend": 5.0}],
                 "total_cost_micros": 5_000_000, "total_spend": 5.0, "campaign_count": 1,
                 "customer_id": "C1", "currency_code": "USD",
                 "coverage_start": "2026-04-01", "coverage_end": "2026-06-22"}
    coverage = [{"chunk_start": "2026-04-01", "chunk_end": "2026-06-22", "status": "verified"}]
    geo_rows = [{"campaign_name": "alpha", "country": "US", "spend": 99.0}]
    revenue_rows = [{"campaign_name": "alpha", "country": "US", "deal_id": "d1",
                     "deal_amount_usd": 10000.0, "match_status": "matched"}]
    _patch_revattr(monkeypatch, canonical=canonical, geo_rows=geo_rows,
                   coverage=coverage, revenue_rows=revenue_rows)
    out = build("current_quarter", now=_at("2026-06-22"))
    # Revenue truth is independent of the spend source.
    assert out["summary"]["won_revenue"] == 10000.0
    assert out["summary"]["customers"] == 1


# ════════════ correction 1: geo is never a ROAS denominator ════════════


def test_geo_fallback_never_produces_campaign_or_country_roas(monkeypatch):
    # Canonical table UNREACHABLE → geo is diagnostic only. ROAS unavailable for
    # both Campaign and Country; geo spend (99) is never a denominator.
    build = _load_revattr()
    canonical = {"available": False, "rows": []}
    coverage = []
    geo_rows = [{"campaign_name": "alpha", "country": "US", "spend": 99.0}]
    revenue_rows = [{"campaign_name": "alpha", "country": "US", "deal_id": "d1",
                     "deal_amount_usd": 10000.0, "match_status": "matched"}]
    _patch_revattr(monkeypatch, canonical=canonical, geo_rows=geo_rows,
                   coverage=coverage, revenue_rows=revenue_rows)
    out = build("current_quarter", now=_at("2026-06-22"))
    sh = out["source_health"]
    assert sh["spend_source"] == "geo_fallback"
    assert sh["spend_coverage_status"] == "geo_fallback"
    assert sh["campaign_roas_available"] is False
    assert sh["country_roas_available"] is False
    assert out["summary"]["roas"] is None
    for c in out["campaigns"]:
        assert c["roas"] is None, "geo fallback must never back a Campaign ROAS"


def test_incomplete_canonical_coverage_blocks_roas(monkeypatch):
    # Canonical REACHABLE with spend rows, but coverage is missing a chunk →
    # incomplete → Campaign ROAS unavailable (never from a partial denominator).
    build = _load_revattr()
    canonical = {"available": True, "rows": [{"campaign_id": "1", "campaign_name": "alpha",
                 "cost_micros": 5_000_000, "spend": 5.0}],
                 "total_cost_micros": 5_000_000, "total_spend": 5.0, "campaign_count": 1,
                 "customer_id": "C1", "currency_code": "USD",
                 "coverage_start": "2026-05-01", "coverage_end": "2026-06-22"}
    # Coverage only covers part of Q2 (window starts 2026-04-01) → incomplete.
    coverage = [{"chunk_start": "2026-05-01", "chunk_end": "2026-06-22", "status": "verified"}]
    geo_rows = [{"campaign_name": "alpha", "country": "US", "spend": 5.0}]
    revenue_rows = [{"campaign_name": "alpha", "country": "US", "deal_id": "d1",
                     "deal_amount_usd": 10000.0, "match_status": "matched"}]
    _patch_revattr(monkeypatch, canonical=canonical, geo_rows=geo_rows,
                   coverage=coverage, revenue_rows=revenue_rows)
    out = build("current_quarter", now=_at("2026-06-22"))
    sh = out["source_health"]
    assert sh["spend_coverage_status"] == "incomplete"
    assert sh["campaign_roas_available"] is False
    assert out["summary"]["roas"] is None
    camp = next(c for c in out["campaigns"] if c["campaign_name"] == "alpha")
    assert camp["roas"] is None


def test_complete_coverage_zero_rows_is_verified_zero_spend(monkeypatch):
    # Complete coverage + zero canonical rows = verified zero spend (NOT incomplete).
    build = _load_revattr()
    canonical = {"available": True, "rows": [], "total_cost_micros": 0, "total_spend": 0.0,
                 "campaign_count": 0, "customer_id": "C1", "currency_code": "USD",
                 "coverage_start": None, "coverage_end": None}
    coverage = [{"chunk_start": "2026-04-01", "chunk_end": "2026-06-22", "status": "verified"}]
    geo_rows = []
    revenue_rows = [{"campaign_name": "alpha", "country": "US", "deal_id": "d1",
                     "deal_amount_usd": 10000.0, "match_status": "matched"}]
    _patch_revattr(monkeypatch, canonical=canonical, geo_rows=geo_rows,
                   coverage=coverage, revenue_rows=revenue_rows)
    out = build("current_quarter", now=_at("2026-06-22"))
    sh = out["source_health"]
    assert sh["spend_coverage_status"] == "complete"
    assert sh["campaign_roas_available"] is True


# ════════════ correction 4: explicit campaign mapping status ════════════


def test_unmatched_campaign_shows_spend_mapping_unavailable(monkeypatch):
    # Complete canonical window: spend campaign is "alpha", but revenue is on a
    # DIFFERENT campaign "beta" with no canonical spend → beta's spend + ROAS are
    # Unavailable (never a fake $0), flagged spend_mapping="unavailable".
    build = _load_revattr()
    canonical = {"available": True, "rows": [{"campaign_id": "1", "campaign_name": "alpha",
                 "cost_micros": 5_000_000, "spend": 5.0}],
                 "total_cost_micros": 5_000_000, "total_spend": 5.0, "campaign_count": 1,
                 "customer_id": "C1", "currency_code": "USD",
                 "coverage_start": "2026-04-01", "coverage_end": "2026-06-22"}
    coverage = [{"chunk_start": "2026-04-01", "chunk_end": "2026-06-22", "status": "verified"}]
    geo_rows = []
    revenue_rows = [{"campaign_name": "beta", "country": "US", "deal_id": "d1",
                     "deal_amount_usd": 10000.0, "match_status": "matched"}]
    _patch_revattr(monkeypatch, canonical=canonical, geo_rows=geo_rows,
                   coverage=coverage, revenue_rows=revenue_rows)
    out = build("current_quarter", now=_at("2026-06-22"))
    beta = next(c for c in out["campaigns"] if c["campaign_name"] == "beta")
    assert beta["spend"] is None, "unmatched campaign spend must be Unavailable, never $0"
    assert beta["roas"] is None
    assert beta["spend_mapping"] == "unavailable"


def test_campaign_table_renders_spend_mapping_unavailable():
    # The campaign table renders Spend + ROAS as Unavailable for unmatched rows.
    assert 'r.spend_mapping === "unavailable"' in JS
    assert "Spend mapping unavailable" in JS


# ════════════ correction 2: fail closed on canonical persistence ════════════


def test_backfill_fails_chunk_when_upsert_count_mismatches(monkeypatch):
    # API returns 2 rows but the canonical upsert persists only 1 → chunk fails,
    # is NOT marked verified/completed, and a `failed` coverage row is written.
    svc = _load_spend()
    rows = [{"customer_id": "C1", "currency_code": "USD", "campaign_id": str(i),
             "campaign_name": "a", "spend_date": "2026-01-01", "cost_micros": 1_000_000}
            for i in (1, 2)]
    monkeypatch.setattr(svc, "fetch_daily_spend",
                        lambda a, b: {"customer_id": "C1", "rows": rows, "source_query_version": "v1"})
    monkeypatch.setattr(svc, "configured_customer_id", lambda: "C1")
    monkeypatch.setattr("db.writers.upsert_campaign_daily_spend", lambda *a, **k: 1)  # short write
    cov_calls = []
    monkeypatch.setattr("db.writers.upsert_spend_coverage",
                        lambda cid, s, e, status, **k: cov_calls.append((cid, status)) or True)
    out = svc.run_google_ads_spend_backfill(
        date_from="2026-01-01", date_to="2026-01-31", dry_run=False, chunk_months=1)
    assert out["summary"]["chunks_verified"] == 0
    assert out["summary"]["chunks_failed"] == 1
    assert ("C1", "failed") in cov_calls
    assert not any(s == "verified" for _, s in cov_calls)


def test_backfill_fails_chunk_when_coverage_write_fails(monkeypatch):
    # Spend upsert succeeds but the coverage-ledger write fails → chunk fails.
    svc = _load_spend()
    rows = [{"customer_id": "C1", "currency_code": "USD", "campaign_id": "1",
             "campaign_name": "a", "spend_date": "2026-01-01", "cost_micros": 1_000_000}]
    monkeypatch.setattr(svc, "fetch_daily_spend",
                        lambda a, b: {"customer_id": "C1", "rows": rows, "source_query_version": "v1"})
    monkeypatch.setattr(svc, "configured_customer_id", lambda: "C1")
    monkeypatch.setattr("db.writers.upsert_campaign_daily_spend", lambda *a, **k: 1)
    seen = []
    # Verified write fails (False); the later failed write succeeds.
    monkeypatch.setattr(
        "db.writers.upsert_spend_coverage",
        lambda cid, s, e, status, **k: (seen.append(status), status != "verified")[1])
    out = svc.run_google_ads_spend_backfill(
        date_from="2026-01-01", date_to="2026-01-31", dry_run=False, chunk_months=1)
    assert out["summary"]["chunks_failed"] == 1
    assert out["summary"]["chunks_verified"] == 0
    assert "verified" in seen and "failed" in seen


def test_daily_sync_fails_closed_on_persistence(monkeypatch):
    # The daily canonical-spend step must NOT report success when a non-empty
    # fetch cannot be fully persisted; it records a `failed` coverage chunk.
    try:
        import scheduler.incremental_sync as inc
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"incremental sync import unavailable: {exc}")
    from datetime import date as _date
    monkeypatch.setattr(
        "services.google_ads_spend_service.fetch_daily_spend",
        lambda a, b: {"customer_id": "C1", "source_query_version": "v1", "rows": [
            {"customer_id": "C1", "currency_code": "USD", "campaign_id": "1",
             "campaign_name": "a", "spend_date": a, "cost_micros": 1_000_000}]})
    monkeypatch.setattr("services.google_ads_spend_service.configured_customer_id", lambda: "C1")
    monkeypatch.setattr(inc.db_writers, "start_sync_batch", lambda **k: 1)
    monkeypatch.setattr(inc.db_writers, "finish_sync_batch", lambda **k: True)
    monkeypatch.setattr(inc.db_writers, "upsert_campaign_daily_spend", lambda *a, **k: 0)  # short write
    cov = []
    monkeypatch.setattr(inc.db_writers, "upsert_spend_coverage",
                        lambda cid, s, e, status, **k: cov.append((cid, status)) or True)
    errors = []
    out = inc._sync_canonical_spend(run_id=42, date_to=_date(2026, 6, 22), errors=errors)
    assert out["status"] == "failed"
    assert ("C1", "failed") in cov
    assert not any(s == "verified" for _, s in cov)


def test_failed_coverage_uses_configured_customer_id_and_guards_verified():
    # Failed coverage is written with the configured customer id (not None), and
    # a failed write never overwrites a prior verified chunk (writer guard).
    assert "def configured_customer_id" in SPEND_SRC
    assert "GOOGLE_ADS_CUSTOMER_ID" in SPEND_SRC
    # Backfill + daily sync both write failed coverage via configured id.
    assert SPEND_SRC.count("configured_customer_id()") >= 2
    INCR = open(os.path.join(ROOT, "scheduler", "incremental_sync.py"), encoding="utf-8").read()
    assert "configured_customer_id()" in INCR
    # Writer guard: a failed status cannot demote a verified chunk.
    assert "status <> 'verified'" in WRITERS


# ════════════ 10. no Google Ads write methods ════════════


def test_no_google_ads_write_methods():
    for src in (SPEND_SRC, WRITERS):
        low = src.lower()
        for forbidden in ("mutate", "campaignoperation", "create_campaign",
                          "update_campaign", "campaignbudgetservice", "set_budget"):
            assert forbidden not in low, f"forbidden GA write token: {forbidden}"
    # The new canonical spend query is read-only (SELECT, no mutate).
    fn = CONN_SRC[CONN_SRC.find("def fetch_campaign_daily_spend"):]
    assert "mutate" not in fn.lower()
    assert "search_stream" in fn or "_run_search_stream" in fn


def test_canonical_query_has_required_fields():
    fn = CONN_SRC[CONN_SRC.find("def fetch_campaign_daily_spend"):CONN_SRC.find("def fetch_campaign_daily_spend") + 2000]
    for field in ("campaign.id", "campaign.name", "segments.date",
                  "metrics.cost_micros", "customer.currency_code"):
        assert field in fn, f"canonical query missing {field}"
    # Customer ID + currency preserved on each row.
    assert '"customer_id"' in fn and '"currency_code"' in fn


# ════════════ endpoints + durable backfill ════════════


def test_spend_endpoints_registered():
    assert '"/api/google-ads-spend-audit"' in SERVER
    assert '"/api/google-ads-spend-backfill/run"' in SERVER
    assert '"/api/google-ads-spend-backfill/status"' in SERVER
    run_idx = SERVER.find("def api_spend_backfill_run")
    assert "status_code=202" in SERVER[run_idx - 90:run_idx]
    region = SERVER[run_idx:run_idx + 2600]
    assert "check_admin_or_token(request)" in region
    assert "threading.Thread" in region
    assert 'job_type="google_ads_spend_backfill"' in region


def test_failed_spend_chunk_not_completed_and_resumes(monkeypatch):
    svc = _load_spend()
    calls = {"n": 0}

    def fetch(a, b):
        # February fails on the first pass.
        if a.startswith("2026-02"):
            if calls["n"] == 0:
                raise RuntimeError("transient")
        return {"customer_id": "C1", "rows": [
            {"customer_id": "C1", "currency_code": "USD", "campaign_id": "1",
             "campaign_name": "a", "spend_date": a, "cost_micros": 1_000_000}],
            "source_query_version": "v1"}

    monkeypatch.setattr(svc, "fetch_daily_spend", fetch)
    # PR-ADS-120: account-daily reconciliation fetch runs on the success path.
    monkeypatch.setattr(svc, "fetch_account_daily_spend", lambda a, b: {"rows": []})
    monkeypatch.setattr("db.writers.upsert_campaign_daily_spend", lambda *a, **k: 1)
    monkeypatch.setattr("db.writers.upsert_spend_coverage", lambda *a, **k: True)
    monkeypatch.setattr("db.writers.upsert_account_daily_spend", lambda *a, **k: 0)
    store = {"completed": []}
    cp = lambda jid, snap: store.__setitem__("completed", list(snap.get("completed_chunks", [])))
    lc = lambda: list(store["completed"])

    out1 = svc.run_google_ads_spend_backfill(
        date_from="2026-01-01", date_to="2026-02-28", dry_run=False, chunk_months=1,
        job_id="g1", checkpoint=cp, load_completed=lc)
    assert out1["status"] == "partial"
    assert any(c.startswith("2026-01") for c in store["completed"])
    assert not any(c.startswith("2026-02") for c in store["completed"]), "failed Feb stays incomplete"

    calls["n"] = 1  # Feb now succeeds
    out2 = svc.run_google_ads_spend_backfill(
        date_from="2026-01-01", date_to="2026-02-28", dry_run=False, chunk_months=1,
        resume=True, job_id="g1", checkpoint=cp, load_completed=lc)
    processed = {c["chunk"] for c in out2["chunks"]}
    assert all(c.startswith("2026-02") for c in processed), "resume retries only Feb"
    assert out2["status"] == "success"


# ════════════ 12. revenue-page chrome stays hidden ════════════


def test_revenue_pages_hide_global_chrome():
    # All five Revenue & Attribution pages are revenue pages.
    m = JS[JS.find("REVENUE_PAGES"):JS.find("REVENUE_PAGES") + 200]
    for p in ("roas-campaigns", "roas-countries", "deals", "revenue-health", "revenue-by-source"):
        assert f'"{p}"' in m
    nav = JS[JS.find("function navigate(page, options)"):JS.find("function navigate(page, options)") + 3400]
    assert "isRevenuePage(page)" in nav
    assert "time-range-bar" in nav        # global ad-window bar gated
    assert "monitoring-banner" in nav     # monitoring warning gated
    assert "!revenuePage && !!PAGE_HELP_CONTENT[page]" in JS  # help button gated


# ════════════ spend truth panel + gates on the page ════════════


def test_spend_truth_panel_and_gates_present():
    assert 'id="spend-truth-panel"' in HTML
    assert "function renderSpendTruth" in JS
    assert "/api/google-ads-spend-audit?window=" in JS
    assert "data-spend-backfill-action" in JS
    # Campaign page ROAS gate on incomplete spend coverage.
    assert "function spendCoverageIncomplete" in JS
    assert "Spend coverage incomplete — ROAS unavailable" in JS
    # Country page reconciliation gate.
    assert "country_spend_reconciled === false" in JS
    # PR-ADS-128 reworded the country blocked card with cause-specific copy; the
    # reconciliation gate still blocks the trusted ROAS table.
    assert "Country ROAS is blocked" in JS
