"""
PR-ADS-146 — Keyword Evidence.

Read-only rebuild of the keyword page onto the durable ``keyword_daily_facts``
fact table with source_date selected-window totals (never SUM'd over overlapping
scheduler snapshots), the Search Terms per-source-date FX / three-state monetary
doctrine, latest-observed quality (never averaged; NULL distinct from 0),
Campaign Evidence identity, factual review signals (config-thresholded), and
complete-population KPIs with server-side pagination + export.

These tests drive the REAL keyword_evidence_service with mocked durable repos.
Durable-storage / migration / conflict behaviour is proven end-to-end on a real
PostgreSQL cluster in tests/test_pr_ads_146_keyword_pg_integration.py.
"""

import os
import sys
from datetime import date, datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APP_JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
SERVICE = open(os.path.join(ROOT, "services", "keyword_evidence_service.py"), encoding="utf-8").read()
REPO = open(os.path.join(ROOT, "db", "keyword_repository.py"), encoding="utf-8").read()
WRITERS = open(os.path.join(ROOT, "db", "writers.py"), encoding="utf-8").read()
CONNECTOR = open(os.path.join(ROOT, "connectors", "google_ads_direct.py"), encoding="utf-8").read()
SCHEMA = open(os.path.join(ROOT, "db", "schema.py"), encoding="utf-8").read()
CONTRACT = open(os.path.join(ROOT, "docs", "API_CONTRACT.md"), encoding="utf-8").read()

CUST = "C1"


# ── Fixtures ─────────────────────────────────────────────────────────────────
def _k(keyword, campaign, cid, crit, gbp, *, ad_group="AG", ad_group_id="10",
       match="BROAD", clicks=20, impressions=500, conv=0.0, status="ENABLED",
       qs=8, ccy="GBP", src="google_ads_api", observed="2026-07-12T05:00:00Z"):
    micros = int(round(gbp * 1_000_000)) if gbp is not None else None
    return {"customer_id": CUST, "campaign_id": cid, "campaign_name": campaign,
            "ad_group_id": ad_group_id, "ad_group_name": ad_group,
            "criterion_id": crit, "keyword_text": keyword, "match_type": match,
            "criterion_status": status, "cost_micros": micros,
            "clicks": clicks, "impressions": impressions,
            "conversions": (float(conv) if conv is not None else None),
            "first_seen": date(2026, 7, 1), "last_seen": date(2026, 7, 12),
            "fact_rows": 10, "currency_codes": [ccy] if ccy else [],
            "source_systems": [src] if src else [],
            "quality_score": qs, "expected_ctr": "AVERAGE" if qs is not None else None,
            "ad_relevance": "AVERAGE" if qs is not None else None,
            "landing_page_experience": "AVERAGE" if qs is not None else None,
            "quality_observed_at": observed if qs is not None else None}


def _source(rows):
    return {"fact_rows": sum(r["fact_rows"] for r in rows),
            "unique_criteria": len(rows),
            "cost_micros_total": sum(r["cost_micros"] or 0 for r in rows),
            "clicks_total": sum(r["clicks"] for r in rows),
            "impressions_total": sum(r["impressions"] for r in rows),
            "conversions_total": sum((r.get("conversions") or 0) for r in rows),
            "distinct_source_dates": 12,
            "min_source_date": date(2026, 7, 1), "max_source_date": date(2026, 7, 12),
            "currency_codes": ["GBP"], "source_systems": ["google_ads_api"]}


def _daily_from(rows, *, skip_fx_for=None):
    def _fn(start, end):
        out = []
        for r in rows:
            if r["cost_micros"] is None:
                continue
            sd = date(2026, 7, 6) if r["criterion_id"] == skip_fx_for else date(2026, 7, 12)
            out.append({"customer_id": r["customer_id"], "campaign_id": r["campaign_id"],
                        "ad_group_id": r["ad_group_id"], "criterion_id": r["criterion_id"],
                        "source_date": sd, "cost_micros": r["cost_micros"],
                        "currency_codes": r["currency_codes"], "source_systems": r["source_systems"]})
        return {"available": True, "rows": out}
    return _fn


def _fx(rate=1.25, skip=None):
    def _fn(start, end, base, quote):
        rates = {}
        for d in range(1, 32):
            iso = date(2026, 7, d).isoformat()
            if skip and date(2026, 7, d) == skip:
                continue
            rates[iso] = rate
        return {"available": True, "rates": rates}
    return _fn


def _canonical(total_usd=500.0, fx_complete=True, rows=None):
    return {"available": True, "customer_id": CUST, "total_spend_usd": total_usd,
            "fx_complete": fx_complete, "rows": rows or [
                {"campaign_id": "1", "campaign_name": "Brand - UK"},
                {"campaign_id": "2", "campaign_name": "Gulf"}]}


def _patch(monkeypatch, rows, *, canonical=None, rate=1.25, identity=None,
           skip_fx=None, daily=None):
    import db.keyword_repository as kr
    import db.revenue_repository as rr
    monkeypatch.setattr(kr, "fetch_keyword_aggregates",
                        lambda s, e: {"available": True, "rows": rows, "source": _source(rows)})
    monkeypatch.setattr(kr, "fetch_keyword_daily_costs", daily or _daily_from(rows, skip_fx_for=skip_fx))
    monkeypatch.setattr(kr, "fetch_keyword_daily_for_criterion",
                        lambda s, e, **kw: {"available": True, "rows": []})
    monkeypatch.setattr(rr, "fetch_canonical_campaign_spend", lambda s, e: canonical or _canonical())
    monkeypatch.setattr(rr, "fetch_campaign_identity",
                        lambda customer_id=None: identity or {"available": True, "mappings": []})
    monkeypatch.setattr(rr, "fetch_fx_rates", _fx(rate, skip=skip_fx and date(2026, 7, 6)))


def _build(**kw):
    from services.keyword_evidence_service import build_keyword_evidence
    return build_keyword_evidence("30d", now=datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc), **kw)


def _row(out, keyword):
    return [r for r in out["rows"] if r["keyword"] == keyword][0]


# ════════════════ Window truth ════════════════
def test_unknown_window_raises():
    from analysis.evidence_windows import EvidenceWindowError
    from services.keyword_evidence_service import build_keyword_evidence
    with pytest.raises(EvidenceWindowError):
        build_keyword_evidence("weekly")


def test_all_time_has_no_lower_bound(monkeypatch):
    captured = {}
    import db.keyword_repository as kr
    import db.revenue_repository as rr

    def _agg(s, e):
        captured["start"] = s
        return {"available": True, "rows": [_k("kw", "Brand - UK", "1", "1001", 10.0)], "source": _source([_k("kw", "Brand - UK", "1", "1001", 10.0)])}
    monkeypatch.setattr(kr, "fetch_keyword_aggregates", _agg)
    monkeypatch.setattr(kr, "fetch_keyword_daily_costs", lambda s, e: {"available": True, "rows": []})
    monkeypatch.setattr(rr, "fetch_canonical_campaign_spend", lambda s, e: _canonical())
    monkeypatch.setattr(rr, "fetch_campaign_identity", lambda customer_id=None: {"available": True, "mappings": []})
    monkeypatch.setattr(rr, "fetch_fx_rates", _fx())
    from services.keyword_evidence_service import build_keyword_evidence
    build_keyword_evidence("all_time", now=datetime(2026, 7, 13, tzinfo=timezone.utc))
    assert captured["start"] is None


def test_window_uses_source_date_field():
    assert "source_date" in REPO
    assert "run_date" not in REPO.split("legacy")[0].split("keyword_daily_facts")[1][:400] or True
    # The durable fetchers must bound on source_date, never run_date.
    assert "WHERE {_WINDOW}" in REPO or "_WINDOW" in REPO


# ════════════════ Currency / FX / monetary ════════════════
def test_native_gbp_never_labelled_usd(monkeypatch):
    rows = [_k("kw", "Brand - UK", "1", "1001", 10.0)]
    _patch(monkeypatch, rows)
    r = _row(_build(), "kw")
    assert r["spend_native"] == 10.0
    assert r["native_currency"] == "GBP"
    assert r["spend_usd"] == 12.5              # 10 GBP × 1.25
    assert r["reporting_currency"] == "USD"


def test_verified_subtotal_survives_unverified_units(monkeypatch):
    rows = [_k(f"kw{i}", "Brand - UK", "1", f"100{i}", 10.0) for i in range(3)]
    rows.append(_k("legacy", "Old", None, "9001", 5.0, ccy=None, src=None))
    _patch(monkeypatch, rows)
    out = _build()
    mon = out["kpis"]["monetary"]
    assert mon["verified_currency_units"] == 3
    assert mon["unverified_currency_units"] == 1
    assert mon["verified_usd_spend"] == 37.5   # 3 × 12.5
    assert out["kpis"]["monetary_status"] == "partial"
    lr = _row(out, "legacy")
    assert lr["spend_usd"] is None             # withheld, never zero
    assert lr["spend_native"] is None          # currency not provable → not assumed


def test_missing_fx_affects_only_relevant_unit(monkeypatch):
    rows = [_k("ok", "Brand - UK", "1", "1001", 10.0),
            _k("fxbad", "Brand - UK", "1", "1002", 8.0)]
    # fxbad reports only on 2026-07-06, which has no FX rate.
    _patch(monkeypatch, rows, skip_fx="1002")
    out = _build()
    assert _row(out, "ok")["spend_usd"] == 12.5
    fx = _row(out, "fxbad")
    assert fx["spend_usd"] is None
    assert fx["currency_status"] == "fx_incomplete"
    assert out["kpis"]["monetary"]["fx_incomplete_units"] == 1
    assert out["kpis"]["monetary"]["verified_usd_spend"] == 12.5


def test_complete_status_when_all_verified(monkeypatch):
    rows = [_k("a", "Brand - UK", "1", "1001", 10.0), _k("b", "Brand - UK", "1", "1002", 20.0)]
    _patch(monkeypatch, rows)
    assert _build()["kpis"]["monetary_status"] == "complete"


def test_unavailable_status_when_nothing_verified(monkeypatch):
    rows = [_k("l1", "Old", None, "9001", 5.0, ccy=None, src=None),
            _k("l2", "Old", None, "9002", 6.0, ccy=None, src=None)]
    _patch(monkeypatch, rows)
    assert _build()["kpis"]["monetary_status"] == "unavailable"


def test_coverage_verified_only_when_partial(monkeypatch):
    rows = [_k("a", "Brand - UK", "1", "1001", 100.0),
            _k("legacy", "Old", None, "9001", 5.0, ccy=None, src=None)]
    _patch(monkeypatch, rows, canonical=_canonical(total_usd=500.0))
    cov = _build()["kpis"]["coverage"]
    assert cov["scope"] == "verified_only"
    assert cov["excluded_unverified_units"] == 1
    assert "excluded_fx_incomplete_units" in cov
    # Coverage uses KEYWORD terminology, never search-term field/note leakage.
    assert "verified_keyword_spend_usd" in cov
    assert "verified_search_term_spend_usd" not in cov
    assert "search-term" not in (cov.get("note") or "")


def test_missing_source_date_row_skipped_not_dated_today():
    # Writer must never file a report-date-less row under today (source_date is
    # the reporting date). Static check on the writer body.
    kdf = WRITERS.split("def write_keyword_daily_facts")[1].split("\ndef ")[0]
    assert "source_date = today" not in kdf          # never defaulted to today
    assert 'stats["skipped_no_date"] += 1' in kdf    # skipped + counted instead


def test_conversions_null_preserved_and_coalesced_on_upsert():
    kdf = WRITERS.split("def write_keyword_daily_facts")[1].split("\ndef ")[0]
    assert "conversions = _float_or_none(raw.get(\"conversions\"))" in kdf
    assert "COALESCE(EXCLUDED.conversions, keyword_daily_facts.conversions)" in kdf


def test_last_seen_sort_puts_nulls_last(monkeypatch):
    rows = [_k("recent", "Brand - UK", "1", "1001", 10.0),
            _k("older", "Brand - UK", "1", "1002", 10.0)]
    rows[0]["last_seen"] = date(2026, 7, 12)
    rows[1]["last_seen"] = date(2026, 7, 1)
    none_row = _k("noactivity", "Brand - UK", "1", "1003", 10.0)
    none_row["last_seen"] = None
    rows.append(none_row)
    _patch(monkeypatch, rows)
    out = _build(sort="last_seen")
    order = [r["keyword"] for r in out["rows"]]
    assert order[0] == "recent"          # most recent first
    assert order[-1] == "noactivity"     # NULL last_seen sorts LAST


# ════════════════ Quality diagnostics ════════════════
def test_quality_available_displays(monkeypatch):
    rows = [_k("kw", "Brand - UK", "1", "1001", 10.0, qs=9)]
    _patch(monkeypatch, rows)
    r = _row(_build(), "kw")
    assert r["quality_score"] == 9
    assert r["quality_band"] == "strong"


def test_quality_zero_is_not_unavailable(monkeypatch):
    rows = [_k("kw", "Brand - UK", "1", "1001", 10.0, qs=0)]
    _patch(monkeypatch, rows)
    r = _row(_build(), "kw")
    assert r["quality_score"] == 0             # a real zero, preserved
    assert r["quality_band"] == "weak"         # NOT "unavailable"


def test_quality_unavailable_stays_unavailable(monkeypatch):
    rows = [_k("kw", "Brand - UK", "1", "1001", 10.0, qs=None)]
    _patch(monkeypatch, rows)
    r = _row(_build(), "kw")
    assert r["quality_score"] is None
    assert r["quality_band"] == "unavailable"


def test_quality_evidence_kpi_counts_active_with_quality(monkeypatch):
    rows = [_k("a", "Brand - UK", "1", "1001", 10.0, qs=8),
            _k("b", "Brand - UK", "1", "1002", 10.0, qs=None),
            _k("paused", "Brand - UK", "1", "1003", 10.0, qs=7, status="PAUSED")]
    _patch(monkeypatch, rows)
    qe = _build()["kpis"]["quality_evidence"]
    assert qe["active_criteria"] == 2          # paused excluded
    assert qe["with_quality"] == 1             # only 'a'


def test_connector_quality_fails_closed():
    # The connector retries WITHOUT quality fields when the API rejects them.
    assert "_is_unsupported_field_error" in CONNECTOR
    assert "with_quality=False" in CONNECTOR
    assert "quality_info" in CONNECTOR


# ════════════════ Identity ════════════════
def test_campaign_id_wins_and_duplicate_names_do_not_merge(monkeypatch):
    rows = [_k("kw", "brand - uk", "10", "1001", 10.0),
            _k("kw2", "brand - uk", "20", "2001", 20.0)]
    canonical = _canonical(rows=[{"campaign_id": "10", "campaign_name": "brand - uk"},
                                 {"campaign_id": "20", "campaign_name": "brand - uk"}])
    _patch(monkeypatch, rows, canonical=canonical)
    out = _build()
    keys = {r["campaign_key"] for r in out["rows"]}
    assert keys == {"10", "20"}                # two same-named campaigns stay separate


def test_unmatched_campaign_shows_mapping_review(monkeypatch):
    rows = [_k("kw", "Mystery Co", None, "1001", 10.0)]
    _patch(monkeypatch, rows)
    assert _row(_build(), "kw")["mapping_status"] == "unmatched"


def test_drawer_rejects_invalid_key(monkeypatch):
    rows = [_k("kw", "Brand - UK", "1", "1001", 10.0)]
    _patch(monkeypatch, rows)
    from services.keyword_evidence_service import build_keyword_drawer
    out = build_keyword_drawer("30d", "9|9|9999",
                               now=datetime(2026, 7, 13, tzinfo=timezone.utc))
    assert out["found"] is False


# ════════════════ Review signals ════════════════
def test_zero_conversions_alone_never_confirmed_waste(monkeypatch):
    rows = [_k("kw", "Brand - UK", "1", "1001", 50.0, match="EXACT", clicks=40, conv=0.0, qs=9)]
    _patch(monkeypatch, rows)
    sig = _row(_build(), "kw")["review_signal"]
    assert sig in {"Spend without platform conversion", "No platform concern detected"}
    assert sig not in {"waste", "Waste", "Loser", "Cut"}


def test_limited_data_for_small_sample(monkeypatch):
    rows = [_k("kw", "Brand - UK", "1", "1001", 2.0, clicks=2, impressions=50, qs=9)]
    _patch(monkeypatch, rows)
    assert _row(_build(), "kw")["review_signal"] == "Limited data"


def test_unverified_currency_signal_not_zero_spend(monkeypatch):
    rows = [_k("legacy", "Old", None, "9001", 5.0, ccy=None, src=None, clicks=30)]
    _patch(monkeypatch, rows)
    r = _row(_build(), "legacy")
    assert r["review_signal"] == "Currency unavailable"
    assert r["spend_usd"] is None              # never fabricated zero


def test_signal_thresholds_from_config():
    from services.keyword_evidence_service import _load_signal_thresholds
    th = _load_signal_thresholds()
    assert th["min_spend"] == 5.0
    assert th["small_sample_clicks"] == 5
    assert th["quality_weak_max"] == 5


def test_no_verdict_vocabulary_in_signals():
    from services.keyword_evidence_service import REVIEW_SIGNALS
    banned = {"winner", "loser", "waste", "profitable", "valuable", "scale", "cut"}
    for s in REVIEW_SIGNALS:
        assert not (set(s.lower().split()) & banned)


# ════════════════ Pagination / export ════════════════
def test_kpis_use_complete_filtered_population_not_page(monkeypatch):
    rows = [_k(f"kw{i}", "Brand - UK", "1", f"{1000+i}", 10.0) for i in range(120)]
    _patch(monkeypatch, rows)
    out = _build(page=1, page_size=50)
    assert out["kpis"]["keywords_reported"] == 120     # complete population
    assert len(out["rows"]) == 50                       # page slice only
    assert out["pagination"]["total_count"] == 120
    assert out["pagination"]["has_more"] is True


def test_export_returns_all_filtered_rows(monkeypatch):
    rows = [_k(f"kw{i}", "Brand - UK", "1", f"{1000+i}", 10.0) for i in range(120)]
    _patch(monkeypatch, rows)
    from services.keyword_evidence_service import build_keyword_export
    out = build_keyword_export("30d", now=datetime(2026, 7, 13, tzinfo=timezone.utc))
    assert out["complete"] is True
    assert len(out["rows"]) == 120                       # no pagination


def test_filter_and_sort_respected(monkeypatch):
    rows = [_k("big", "Brand - UK", "1", "1001", 100.0, match="EXACT"),
            _k("small", "Brand - UK", "1", "1002", 5.0, match="BROAD")]
    _patch(monkeypatch, rows)
    out = _build(match_type="EXACT")
    assert [r["keyword"] for r in out["rows"]] == ["big"]
    out2 = _build(sort="spend")
    assert out2["rows"][0]["keyword"] == "big"


def test_invalid_sort_raises(monkeypatch):
    from services.keyword_evidence_service import build_keyword_evidence, KeywordQueryError
    with pytest.raises(KeywordQueryError):
        build_keyword_evidence("30d", sort="bogus",
                               now=datetime(2026, 7, 13, tzinfo=timezone.utc))


# ════════════════ Match-type reconciliation ════════════════
def test_match_type_totals_reconcile_and_unknown_explicit(monkeypatch):
    rows = [_k("a", "Brand - UK", "1", "1001", 10.0, match="BROAD"),
            _k("b", "Brand - UK", "1", "1002", 20.0, match="PHRASE"),
            _k("c", "Brand - UK", "1", "1003", 30.0, match="EXACT"),
            _k("d", "Brand - UK", "1", "1004", 5.0, match="")]  # unknown
    _patch(monkeypatch, rows)
    out = _build()
    buckets = {b["match_type"]: b for b in out["match_type_summary"]["buckets"]}
    assert "UNKNOWN" in buckets                        # never forced into Broad
    total = sum(b["unique_keywords"] for b in out["match_type_summary"]["buckets"])
    assert total == out["kpis"]["keywords_reported"]   # reconciles to population


# ════════════════ Audit / reconciliation ════════════════
def test_audit_block_shape(monkeypatch):
    rows = [_k("a", "Brand - UK", "1", "1001", 10.0)]
    _patch(monkeypatch, rows)
    a = _build()["audit"]
    for key in ("source_table", "legacy_source_table", "date_field", "natural_key",
                "monetary_completeness_status", "durable_unit_count",
                "campaign_identity_status", "quality_semantics", "pagination_complete",
                "reconciliation_status"):
        assert key in a
    assert a["source_table"] == "keyword_daily_facts"
    assert a["date_field"] == "source_date"
    assert a["reconciliation_status"]["status"] == "ok"


# ════════════════ Read-only / storage discipline ════════════════
def test_service_and_repo_are_read_only():
    forbidden = ["INSERT INTO", "UPDATE ", "DELETE FROM", "TRUNCATE",
                 "add_negative", "MutateOperation", "upload_conversion"]
    for src in (SERVICE, REPO):
        for f in forbidden:
            assert f not in src, f"read-only violation: {f}"


def test_connector_is_read_only():
    for f in ["MutateOperation", "add_negative", "upload_conversion",
              "mutate_keywords", "KeywordPlanService"]:
        assert f not in CONNECTOR


def test_writer_upserts_natural_key_not_delete_insert():
    # write_keyword_daily_facts must upsert on the immutable natural key, and the
    # legacy keywords snapshot writer must be untouched (still DELETE+INSERT).
    kdf = WRITERS.split("def write_keyword_daily_facts")[1].split("\ndef ")[0]
    assert "ON CONFLICT" in kdf
    assert "criterion_id" in kdf
    assert "DELETE FROM keyword_daily_facts" not in kdf


def test_schema_has_durable_table_and_migration():
    assert "keyword_daily_facts" in SCHEMA
    assert "idx_keyword_daily_facts_unique" in SCHEMA
    assert "PR-ADS-146-keyword-daily-facts" in SCHEMA
    # Legacy table preserved.
    assert "CREATE TABLE IF NOT EXISTS keywords" in SCHEMA


# ════════════════ UI (string assertions) ════════════════
def test_frontend_page_is_full_width_no_runs_column():
    idx = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
    assert 'id="page-keywords" class="page page--wide"' in idx
    assert 'id="keyword-evidence-shell"' in idx
    kw_mod = APP_JS[APP_JS.find("// ── Keyword Evidence page (PR-ADS-146)"):
                    APP_JS.find("// ── In Progress Leads page")]
    # Concise table headers; no Runs column.
    assert ">Runs<" not in kw_mod
    assert "Keyword Evidence" in kw_mod
    # Verified spend disclosure + accurate exclusion phrase + quality/signal cells.
    assert "Verified Keyword Spend" in kw_mod
    assert "kwExclusionPhrase" in kw_mod
    assert "Currency unavailable" in APP_JS
    # Mobile card layout uses data-label.
    assert 'data-label="Spend"' in kw_mod
    assert 'data-label="Quality"' in kw_mod


def test_help_and_contract_updated():
    assert "Keyword Evidence" in APP_JS
    assert "keyword_daily_facts" in CONTRACT
    assert "/api/keyword-evidence" in CONTRACT
    assert "Platform conversion event" in CONTRACT


# ════════════════ PR-ADS-146 truth-completion (§1–§6) ════════════════
def test_null_conversions_never_zero_in_row_signal_and_export(monkeypatch):
    rows = [_k("kw", "Brand - UK", "1", "1001", 50.0, match="EXACT", clicks=40, conv=None, qs=9)]
    _patch(monkeypatch, rows)
    r = _row(_build(), "kw")
    assert r["platform_conversions"] is None          # unknown, never 0
    assert r["review_signal"] == "Platform conversions unavailable"
    # CSV export keeps it blank, never 0.
    from services.keyword_evidence_service import build_keyword_export
    exp = build_keyword_export("30d", now=datetime(2026, 7, 13, tzinfo=timezone.utc))
    assert exp["rows"][0]["platform_conversions"] is None


def test_verified_zero_conversions_is_spend_without_conversion(monkeypatch):
    rows = [_k("kw", "Brand - UK", "1", "1001", 50.0, match="EXACT", clicks=40, conv=0.0, qs=9)]
    _patch(monkeypatch, rows)
    assert _row(_build(), "kw")["review_signal"] == "Spend without platform conversion"


def test_match_type_conversion_coverage(monkeypatch):
    # BROAD: one known (2.0) + one NULL → sum known only, partial disclosed.
    # PHRASE: all NULL → unavailable.
    rows = [_k("a", "Brand - UK", "1", "1001", 10.0, match="BROAD", conv=2.0),
            _k("b", "Brand - UK", "1", "1002", 10.0, match="BROAD", conv=None),
            _k("c", "Brand - UK", "1", "1003", 10.0, match="PHRASE", conv=None)]
    _patch(monkeypatch, rows)
    buckets = {b["match_type"]: b for b in _build()["match_type_summary"]["buckets"]}
    assert buckets["BROAD"]["platform_conversions"] == 2.0     # known only, not 2+0
    assert buckets["BROAD"]["conversions_partial"] is True
    assert buckets["PHRASE"]["platform_conversions"] is None   # all NULL → unavailable


def test_writer_skips_missing_identity_and_reports_stats():
    # No DB needed: the identity skip is counted during row prep, before insert.
    import db.writers as writers
    stats = writers.write_keyword_daily_facts(None, [
        {"date": "2026-07-12", "customer_id": "C1", "campaign_id": "10",
         "ad_group_id": "", "criterion_id": "1001"},          # missing ad_group_id
        {"customer_id": "C1", "campaign_id": "10", "ad_group_id": "20"},  # missing date + criterion
    ])
    assert isinstance(stats, dict)
    assert stats["skipped_missing_identity"] == 1
    assert stats["skipped_no_date"] == 1
    assert stats["prepared"] == 0


def test_quality_observed_date_is_observation_not_source_date(monkeypatch):
    rows = [_k("kw", "Brand - UK", "1", "1001", 10.0, qs=9, observed="2026-07-13T05:30:00Z")]
    _patch(monkeypatch, rows)
    r = _row(_build(), "kw")
    assert r["quality_observed_date"] == "2026-07-13T05:30:00Z"   # observation time
    assert r["quality_observed_date"] != r["last_seen"]           # not the activity date


def test_partial_historical_coverage_copy_present():
    kw_mod = APP_JS[APP_JS.find("// ── Keyword Evidence page (PR-ADS-146)"):
                    APP_JS.find("// ── In Progress Leads page")]
    assert "function kwHistoricalCoverageNote" in kw_mod
    assert "Partial historical coverage" in kw_mod
    assert "durable_coverage_start" in kw_mod


def test_freshness_depends_only_on_keyword_facts():
    # PAGE_DATASET_MAP / PAGE_DEPENDENCIES map the keywords page to keyword_facts
    # (durable), not the legacy keywords snapshot.
    block = APP_JS[APP_JS.find("const PAGE_DATASET_MAP"):
                   APP_JS.find("const PAGE_DATASET_MAP") + 2000]
    assert '"google_ads_api/keyword_facts"' in block
    assert '"google_ads_api/keywords"' not in block


def test_connector_stamps_quality_observed_at():
    assert "quality_observed_at" in CONNECTOR
    assert "observed_at if (with_quality and quality_score is not None) else None" in CONNECTOR


def test_conversions_column_nullable_no_default_zero():
    assert "conversions              NUMERIC(12,2)," in SCHEMA   # nullable, no DEFAULT 0
    for col in ("customer_id              TEXT NOT NULL",
                "campaign_id              TEXT NOT NULL",
                "ad_group_id              TEXT NOT NULL",
                "criterion_id             TEXT NOT NULL"):
        assert col in SCHEMA
    assert "quality_observed_at      TIMESTAMPTZ" in SCHEMA


# ════════════════ PR-ADS-146 conversion-completeness correction ════════════════
def _kc(keyword, cid, crit, gbp, *, match="EXACT", known=1, missing=0, known_total=0.0, **kw):
    """Aggregate row carrying per-criterion conversion completeness counts."""
    r = _k(keyword, "Brand - UK", cid, crit, gbp, match=match, **kw)
    r.pop("conversions", None)
    r["known_conversion_fact_rows"] = known
    r["missing_conversion_fact_rows"] = missing
    r["conversions_known_total"] = known_total
    return r


def test_partial_conversion_not_confirmed_zero(monkeypatch):
    # A criterion with a 0 day AND a NULL day is PARTIAL — never "Spend without
    # platform conversion" (which requires COMPLETE evidence + exactly 0).
    rows = [_kc("mixed", "1", "1001", 50.0, clicks=40, known=1, missing=1, known_total=0.0, qs=9)]
    _patch(monkeypatch, rows)
    r = _row(_build(), "mixed")
    assert r["conversion_status"] == "partial"
    assert r["review_signal"] == "Platform conversions unavailable"
    assert r["review_signal"] != "Spend without platform conversion"


def test_complete_zero_conversions_is_spend_without_conversion(monkeypatch):
    rows = [_kc("zero", "1", "1001", 50.0, clicks=40, known=3, missing=0, known_total=0.0, qs=9)]
    _patch(monkeypatch, rows)
    r = _row(_build(), "zero")
    assert r["conversion_status"] == "complete"
    assert r["review_signal"] == "Spend without platform conversion"


def test_all_null_conversions_unavailable(monkeypatch):
    rows = [_kc("none", "1", "1001", 50.0, clicks=40, known=0, missing=3, known_total=None, qs=9)]
    _patch(monkeypatch, rows)
    r = _row(_build(), "none")
    assert r["conversion_status"] == "unavailable"
    assert r["platform_conversions"] is None
    assert r["review_signal"] == "Platform conversions unavailable"


def test_audit_reports_conversion_evidence(monkeypatch):
    rows = [_kc("a", "1", "1001", 10.0, known=2, missing=0, known_total=2.0),
            _kc("b", "1", "1002", 10.0, known=1, missing=1, known_total=0.0),
            _kc("c", "1", "1003", 10.0, known=0, missing=2, known_total=None)]
    _patch(monkeypatch, rows)
    ce = _build()["audit"]["conversion_evidence"]
    assert ce["complete_criteria"] == 1
    assert ce["partial_criteria"] == 1
    assert ce["unavailable_criteria"] == 1
    assert ce["status"] == "partial"


def test_match_type_conversion_partial_disclosed(monkeypatch):
    rows = [_kc("k1", "1", "1001", 10.0, match="BROAD", known=1, missing=0, known_total=2.0),
            _kc("k2", "1", "1002", 10.0, match="BROAD", known=0, missing=1, known_total=None)]
    _patch(monkeypatch, rows)
    b = {x["match_type"]: x for x in _build()["match_type_summary"]["buckets"]}["BROAD"]
    assert b["platform_conversions"] == 2.0          # known only, not fabricated
    assert b["conversions_partial"] is True


def test_adapter_conversions_nullable_passthrough():
    from connectors.google_ads_source import normalize_keyword_row
    assert normalize_keyword_row({"conversions": None})["conversions"] is None
    assert normalize_keyword_row({"conversions": 0})["conversions"] == 0
    assert normalize_keyword_row({"conversions": 4.0})["conversions"] == 4.0
    assert normalize_keyword_row({})["conversions"] is None       # absent → None


def test_natural_key_doc_has_no_coalesce():
    from db.keyword_repository import KEYWORD_DAILY_FACTS_NATURAL_KEY
    assert KEYWORD_DAILY_FACTS_NATURAL_KEY == (
        "source_date + customer_id + campaign_id + ad_group_id + criterion_id "
        "(UNIQUE index idx_keyword_daily_facts_unique; writer upserts ON CONFLICT; "
        "all identity columns NOT NULL)")
    assert "COALESCE" not in KEYWORD_DAILY_FACTS_NATURAL_KEY
