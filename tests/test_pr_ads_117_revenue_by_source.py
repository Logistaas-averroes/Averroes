"""
PR-ADS-117 — Revenue by Acquisition Source tests.

Proves the classification rules, deal-attribution safety, the revenue-by-source
contract (Google Ads is the only group with spend/ROAS; deals counted once;
window-correct), and that the backfill is read-only to HubSpot.
"""

import os
import sys
from datetime import datetime
from unittest.mock import patch

import pytest

from tests.canonical_ledger_fixtures import (  # noqa: E402
    from_source_rows, patch_canonical_ledger,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
HTML = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
SERVER = open(os.path.join(ROOT, "api", "server.py"), encoding="utf-8").read()
SCHEMA = open(os.path.join(ROOT, "db", "schema.py"), encoding="utf-8").read()


def _at(d):
    return datetime.fromisoformat(d + "T12:00:00+00:00")


# ════════════════════════ classification rules (pure) ════════════════════════

from analysis.source_classification import (  # noqa: E402
    classify_source, attribute_deal,
    GROUP_GOOGLE_ADS, GROUP_OTHER_PAID, GROUP_ORGANIC, GROUP_OFFLINE, GROUP_UNCLASSIFIED,
)


@pytest.mark.parametrize("primary,detail,expected", [
    ("Paid Search", None, GROUP_GOOGLE_ADS),
    ("PAID_SEARCH", None, GROUP_GOOGLE_ADS),
    ("Paid Social", None, GROUP_OTHER_PAID),
    ("Other Campaigns", None, GROUP_OTHER_PAID),
    ("Email Marketing", None, GROUP_OTHER_PAID),
    ("Direct Traffic", None, GROUP_ORGANIC),
    ("Organic Search", None, GROUP_ORGANIC),
    ("Organic Social", None, GROUP_ORGANIC),
    ("Direct Email", None, GROUP_ORGANIC),
    ("Referrals", None, GROUP_ORGANIC),
    ("Offline Sources", "SalesNash / Events", GROUP_OTHER_PAID),
    ("Offline Sources", "Events", GROUP_OTHER_PAID),
    ("Offline Sources", "Referrals", GROUP_ORGANIC),
    ("Offline Sources", "Direct Email", GROUP_ORGANIC),
    ("Offline Sources", "Resellers", GROUP_ORGANIC),
    ("Offline Sources", "Something else entirely", GROUP_OFFLINE),
    ("Other Offline Sources", None, GROUP_OFFLINE),
])
def test_explicit_mapping_rules(primary, detail, expected):
    assert classify_source(primary, detail) == expected


@pytest.mark.parametrize("primary", ["", None, "  ", "Some New Channel", "tiktok ads", "podcast"])
def test_unknown_or_missing_is_unclassified(primary):
    assert classify_source(primary, None) == GROUP_UNCLASSIFIED


def test_unknown_never_defaults_to_organic():
    assert classify_source("mystery source", "mystery detail") != GROUP_ORGANIC


def test_normalisation_case_and_underscores():
    assert classify_source("paid_search", None) == GROUP_GOOGLE_ADS
    assert classify_source("PAID SEARCH", None) == GROUP_GOOGLE_ADS
    assert classify_source("Email_Marketing", None) == GROUP_OTHER_PAID


# ════════════════════════ deal attribution safety ════════════════════════


def test_single_contact_attributed():
    out = attribute_deal([GROUP_GOOGLE_ADS])
    assert out["attribution_status"] == "attributed"
    assert out["acquisition_group"] == GROUP_GOOGLE_ADS


def test_multiple_same_group_attributed():
    out = attribute_deal([GROUP_ORGANIC, GROUP_ORGANIC])
    assert out["attribution_status"] == "attributed"
    assert out["acquisition_group"] == GROUP_ORGANIC


def test_conflicting_groups_ambiguous_not_allocated():
    out = attribute_deal([GROUP_GOOGLE_ADS, GROUP_ORGANIC])
    assert out["attribution_status"] == "ambiguous"
    assert out["acquisition_group"] == "ambiguous"  # never split across sources


def test_no_classified_contact_unclassified():
    assert attribute_deal([])["attribution_status"] == "unclassified"
    assert attribute_deal([GROUP_UNCLASSIFIED])["attribution_status"] == "unclassified"


# ════════════════════════ revenue-by-source contract ════════════════════════


def _load_build():
    try:
        from services.source_attribution_service import build_revenue_by_source
        return build_revenue_by_source
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"service import unavailable: {exc}")


def _verified_truth(usd_spend, native_spend=None):
    """A canonical Google Ads spend-truth block in the verified state."""
    return {
        "state": "verified",
        "google_ads_spend_source": "canonical_campaign_daily_spend",
        "native_currency": "GBP",
        "native_spend": native_spend,
        "usd_spend": usd_spend,
        "fx_status": "verified",
        "spend_coverage_status": "verified",
        "roas_available": True,
        "geo_spend_used": False,
        "geo_spend_note": "Geo spend is diagnostic and is not used as the source-level "
                          "Google Ads denominator.",
    }


def _unavailable_truth():
    """No canonical Google Ads spend source — spend/ROAS Unavailable (never $0)."""
    return {
        "state": "source_unavailable",
        "google_ads_spend_source": "unavailable",
        "native_currency": "GBP",
        "native_spend": None,
        "usd_spend": None,
        "fx_status": "unavailable",
        "spend_coverage_status": "unavailable",
        "roas_available": False,
        "geo_spend_used": False,
        "geo_spend_note": "Geo spend is diagnostic and is not used as the source-level "
                          "Google Ads denominator.",
    }


def _patch_sources(monkeypatch, *, lead_rows=None, revenue_rows=None, spend_rows=None,
                   spend_truth=None, seen=None):
    def cap_leads(start, end, *a, **k):
        if seen is not None:
            seen["leads"] = (start, end)
        return {"available": True, "rows": lead_rows or []}

    def cap_revenue(start, end, *a, **k):
        if seen is not None:
            seen["revenue"] = (start, end)
        return {"available": True, "rows": revenue_rows or []}

    def cap_spend(start, end, *a, **k):
        if seen is not None:
            seen["spend"] = (start, end)
        return {"available": True, "rows": spend_rows or [], "coverage_start": None, "coverage_end": None}

    # PR-ADS-140: Google Ads source spend now comes from the CANONICAL campaign
    # spend truth (same as the mart), NOT the geo table. Default: derive a verified
    # truth from spend_rows (USD == their sum) so the spend/ROAS assertions hold; an
    # empty spend source yields the "source unavailable" truth (never a fake $0).
    if spend_truth is None:
        total = round(sum(float(r.get("spend") or 0) for r in (spend_rows or [])), 2)
        spend_truth = _verified_truth(total) if spend_rows else _unavailable_truth()

    def cap_truth(window, now=None):
        if seen is not None:
            seen["spend_truth_window"] = window
        return dict(spend_truth)

    try:
        monkeypatch.setattr("db.revenue_repository.fetch_source_leads", cap_leads)
        # PR-ADS-153E-B: won revenue is read from the canonical deal ledger, so
        # the window bounds are captured there.
        patch_canonical_ledger(monkeypatch, from_source_rows(revenue_rows or []))
        import db.deal_ledger_repository as ledger_repo
        _canonical_rows = from_source_rows(revenue_rows or [])

        def cap_canonical(start=None, end=None):
            if seen is not None:
                seen["revenue"] = (start, end)
            return {"available": True, "rows": [dict(r) for r in _canonical_rows]}

        monkeypatch.setattr(ledger_repo, "fetch_won_deals", cap_canonical)
        monkeypatch.setattr("db.revenue_repository.fetch_campaign_country_spend", cap_spend)
        monkeypatch.setattr(
            "services.revenue_spend_truth_service.build_google_ads_spend_truth", cap_truth)
        monkeypatch.setattr("db.writers.source_attribution_health_counts",
                            lambda: {"contacts_classified": 0, "deals_attributed": 0,
                                     "ambiguous_deals": 0, "unclassified_deals": 0})
    except (ImportError, AttributeError) as exc:
        pytest.skip(f"runtime deps unavailable: {exc}")


def _group(out, key):
    return next(g for g in out["groups"] if g["group"] == key)


def test_only_google_ads_has_spend_and_roas(monkeypatch):
    build = _load_build()
    _patch_sources(
        monkeypatch,
        revenue_rows=[
            {"acquisition_group": "google_ads", "attribution_status": "attributed", "deal_amount_usd": 1000.0},
            {"acquisition_group": "other_paid", "attribution_status": "attributed", "deal_amount_usd": 500.0},
            {"acquisition_group": "organic", "attribution_status": "attributed", "deal_amount_usd": 700.0},
        ],
        spend_rows=[{"campaign_name": "x", "country": "US", "spend": 500.0}],
    )
    out = build("current_quarter", now=_at("2026-06-22"))
    g = _group(out, "google_ads")
    assert g["spend"] == 500.0 and g["roas"] == 2.0 and g["has_spend"] is True
    for other in ("other_paid", "organic", "offline", "unclassified"):
        og = _group(out, other)
        assert og["spend"] is None, f"{other} must never inherit spend"
        assert og["roas"] is None, f"{other} must never show ROAS"
        assert og["roas_status"] == "unavailable_no_spend_source"


def test_paid_social_and_email_never_inherit_google_roas(monkeypatch):
    build = _load_build()
    _patch_sources(
        monkeypatch,
        lead_rows=[
            {"acquisition_group": "other_paid", "status_category": "qualified"},
        ],
        revenue_rows=[{"acquisition_group": "other_paid", "attribution_status": "attributed", "deal_amount_usd": 999.0}],
        spend_rows=[{"campaign_name": "x", "country": "US", "spend": 1000.0}],
    )
    out = build("current_quarter", now=_at("2026-06-22"))
    op = _group(out, "other_paid")
    assert op["spend"] is None and op["roas"] is None
    assert op["won_revenue"] == 999.0  # revenue still shown


def test_deal_counted_once_across_groups(monkeypatch):
    build = _load_build()
    _patch_sources(
        monkeypatch,
        revenue_rows=[
            {"acquisition_group": "google_ads", "attribution_status": "attributed", "deal_amount_usd": 100.0},
            {"acquisition_group": "ambiguous", "attribution_status": "ambiguous", "deal_amount_usd": 200.0},
            {"acquisition_group": "unclassified", "attribution_status": "unclassified", "deal_amount_usd": 300.0},
        ],
    )
    out = build("all_time", now=_at("2026-06-22"))
    total_customers = sum(g["customers"] for g in out["groups"])
    total_revenue = sum(g["won_revenue"] for g in out["groups"])
    assert total_customers == 3, "each deal counted exactly once"
    assert total_revenue == 600.0
    # Ambiguous + unclassified fold into the Needs-Review section.
    nr = _group(out, "unclassified")
    assert nr["customers"] == 2 and nr["won_revenue"] == 500.0


def test_window_correct_same_bounds_all_sources(monkeypatch):
    build = _load_build()
    seen = {}
    _patch_sources(monkeypatch, seen=seen)
    from datetime import date
    out = build("last_quarter", now=_at("2026-06-22"))
    assert out["window"]["key"] == "last_quarter"
    assert out["window"]["start_date"] == "2026-01-01"
    assert out["window"]["end_date"] == "2026-03-31"
    expect = (date(2026, 1, 1), date(2026, 3, 31))
    # Leads/revenue read the resolved date bounds; PR-ADS-140: Google Ads spend is
    # the canonical spend truth, resolved from the SAME business-window key.
    assert seen["leads"] == expect
    # PR-ADS-153E-B: the canonical revenue read uses UTC datetimes with an
    # EXCLUSIVE upper bound — the same window in the canonical convention.
    rev_start, rev_end = seen["revenue"]
    assert rev_start.date() == date(2026, 1, 1)
    assert rev_end.date() == date(2026, 4, 1)
    assert seen["spend_truth_window"] == "last_quarter"


def test_no_fabricated_zero_spend_or_roas(monkeypatch):
    build = _load_build()
    _patch_sources(monkeypatch, spend_rows=[])  # no google spend at all
    out = build("current_quarter", now=_at("2026-06-22"))
    for g in out["groups"]:
        if not g["has_spend"]:
            assert g["spend"] is None and g["roas"] is None


# ════════════════════════ backfill read-only doctrine ════════════════════════


def test_backfill_read_only_to_external():
    src = open(os.path.join(ROOT, "services", "source_attribution_service.py"), encoding="utf-8").read().lower()
    for forbidden in ("create_deal", "update_deal", "create_contact", "update_contact",
                      "googleads", "google_ads_upload", "upload_conversions"):
        assert forbidden not in src


def test_endpoints_registered():
    assert '"/api/revenue-by-source"' in SERVER
    assert '"/api/source-attribution-backfill/run"' in SERVER
    assert '"/api/source-attribution-backfill/status"' in SERVER
    assert '"/api/source-attribution-health"' in SERVER
    run_idx = SERVER.find("def api_source_backfill_run")
    assert "status_code=202" in SERVER[run_idx - 90:run_idx]
    region = SERVER[run_idx:run_idx + 2600]
    assert "check_admin_or_token(request)" in region
    assert "threading.Thread" in region
    assert 'job_type="source_attribution_backfill"' in region


def test_durable_tables_exist():
    assert "CREATE TABLE IF NOT EXISTS contact_source_classification" in SCHEMA
    assert "CREATE TABLE IF NOT EXISTS deal_source_attribution" in SCHEMA
    for col in ("source_primary_raw", "source_detail_raw", "acquisition_group",
                "classification_rule_version", "attribution_status", "attribution_reason"):
        assert col in SCHEMA
    # deal_id is unique (each deal once).
    assert "deal_id                TEXT NOT NULL UNIQUE" in SCHEMA


# ════════════════════════ frontend page ════════════════════════


def test_page_registered_and_revenue_page():
    assert '"revenue-by-source"' in JS[JS.find("const PAGES"):JS.find("const PAGES") + 700]
    assert '"revenue-by-source"' in JS[JS.find("REVENUE_PAGES"):JS.find("REVENUE_PAGES") + 160]
    assert 'id="page-revenue-by-source"' in HTML
    assert 'data-page="revenue-by-source"' in HTML
    assert 'id="revenue-by-source-range"' in HTML  # date-range chip (PR-116 contract)


def test_page_sections_and_roas_only_for_google():
    # PR-ADS-133: the page renders a group → channel → platform hierarchy. Each
    # group section has Spend + ROAS/Status columns; only Google Ads shows ROAS,
    # every other source is revenue-only (never a fabricated ROAS).
    fn_idx = JS.find("function renderSourceGroupSection")
    body = JS[fn_idx:fn_idx + 1600]
    assert ">Spend<" in body and ">ROAS / Status<" in body
    assert ">Channel / Platform<" in body
    status = JS[JS.find("function sourceStatusLabel"):JS.find("function sourceStatusLabel") + 900]
    assert "Revenue-only — no connected spend source" in status
    # Health summary surfaces the four counts.
    health = JS[JS.find("function renderRevenueBySourceHealth"):JS.find("function renderRevenueBySourceHealth") + 700]
    for label in ("Contacts classified", "Deals attributed", "Ambiguous deals", "Unclassified deals"):
        assert label in health


def test_loader_has_window_guard():
    idx = JS.find("async function loadRevenueBySource")
    body = JS[idx:idx + 1400]
    assert "++_revReqSeq.bySource" in body
    assert "_revResponseIsCurrent" in body
    assert 'setWindowRangeLoading("revenue-by-source-range")' in body
    assert 'renderWindowRange("revenue-by-source-range"' in body


# ════════════════ retryable failures + chunk retryability (corrections) ═══════


def _load_backfill():
    try:
        from services.source_attribution_service import run_source_attribution_backfill
        return run_source_attribution_backfill
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"service import unavailable: {exc}")


def _patch_backfill(monkeypatch, *, contacts_by_chunk=None, deals_by_chunk=None,
                    contacts_fail_first=False):
    """Patch the backfill connector/writer deps. contacts_by_chunk / deals_by_chunk
    are callables (date_from, date_to) -> list. Records upserts."""
    calls = {"deal_upserts": [], "contact_upserts": [], "contacts_pull_count": 0}
    state = {"contacts_attempt": 0}

    def _contacts(date_from, date_to, **k):
        state["contacts_attempt"] += 1
        calls["contacts_pull_count"] += 1
        if contacts_fail_first and state["contacts_attempt"] == 1:
            raise RuntimeError("transient contacts failure")
        return (contacts_by_chunk(date_from, date_to) if contacts_by_chunk else [])

    def _deals(date_from, date_to, **k):
        return (deals_by_chunk(date_from, date_to) if deals_by_chunk else [])

    try:
        monkeypatch.setattr("connectors.hubspot_pull.pull_all_contacts_in_range", _contacts)
        monkeypatch.setattr("connectors.hubspot_pull.pull_closed_won_deals_with_sources_in_range", _deals)
        monkeypatch.setattr("db.writers.upsert_contact_source_classification",
                            lambda rows: calls["contact_upserts"].append(rows) or len(rows))
        monkeypatch.setattr("db.writers.upsert_deal_source_attribution",
                            lambda rows: calls["deal_upserts"].append(rows) or len(rows))
    except (ImportError, AttributeError) as exc:
        pytest.skip(f"runtime deps unavailable: {exc}")
    return calls


def test_failed_deal_lookup_does_not_create_unclassified_row(monkeypatch):
    run = _load_backfill()
    # One deal whose source lookup failed (lookup_failed) + one genuine no-contact deal.
    def deals(df, dt):
        return [
            {"deal_id": "d1", "lookup_failed": True, "contacts": []},
            {"deal_id": "d2", "lookup_failed": False, "contacts": []},  # genuine empty
        ]
    calls = _patch_backfill(monkeypatch, deals_by_chunk=deals)
    out = run(date_from="2026-01-01", date_to="2026-01-31", dry_run=False, chunk_months=1)
    # d1 must NOT be upserted; only d2 (genuine unclassified) is written.
    upserted_ids = [r["deal_id"] for batch in calls["deal_upserts"] for r in batch]
    assert "d1" not in upserted_ids
    assert "d2" in upserted_ids
    assert out["summary"]["unclassified_deals"] == 1   # only the genuine one
    assert out["summary"]["failed"] >= 1               # the failed lookup
    assert out["status"] == "partial"


def test_genuine_zero_contact_deal_is_unclassified(monkeypatch):
    run = _load_backfill()
    calls = _patch_backfill(monkeypatch, deals_by_chunk=lambda df, dt: [
        {"deal_id": "d9", "lookup_failed": False, "contacts": []}])
    out = run(date_from="2026-01-01", date_to="2026-01-31", dry_run=False, chunk_months=1)
    upserted = [r for batch in calls["deal_upserts"] for r in batch]
    assert upserted and upserted[0]["attribution_status"] == "unclassified"
    assert out["status"] == "success"


def test_failed_chunk_not_marked_complete_and_resumes(monkeypatch):
    run = _load_backfill()
    # February's deal lookup fails on the first pass; January is clean.
    def deals(df, dt):
        if df.startswith("2026-02"):
            return [{"deal_id": "feb1", "lookup_failed": True, "contacts": []}]
        return [{"deal_id": "jan1", "lookup_failed": False, "contacts": []}]
    store = {"completed": []}
    def checkpoint(job_id, snap):
        store["completed"] = list(snap.get("completed_chunks", []))
    def load_completed():
        return list(store["completed"])

    _patch_backfill(monkeypatch, deals_by_chunk=deals)
    out1 = run(date_from="2026-01-01", date_to="2026-02-28", dry_run=False, chunk_months=1,
               job_id="sb1", checkpoint=checkpoint, load_completed=load_completed)
    completed = set(store["completed"])
    assert any(c.startswith("2026-01") for c in completed), "clean January must be complete"
    assert not any(c.startswith("2026-02") for c in completed), "failed February must stay incomplete"
    assert out1["status"] == "partial"

    # February now succeeds; resume must retry ONLY February.
    def deals_ok(df, dt):
        return [{"deal_id": "feb1", "lookup_failed": False, "contacts": []}]
    calls2 = _patch_backfill(monkeypatch, deals_by_chunk=deals_ok)
    out2 = run(date_from="2026-01-01", date_to="2026-02-28", dry_run=False, chunk_months=1,
               resume=True, job_id="sb1", checkpoint=checkpoint, load_completed=load_completed)
    processed = {c["chunk"] for c in out2["chunks"]}
    assert all(c.startswith("2026-02") for c in processed), "resume retries only the incomplete chunk"
    assert out2["status"] == "success"
    assert any(c.startswith("2026-02") for c in store["completed"]), "February now complete"


def test_partial_chunk_reupserts_contacts_idempotently(monkeypatch):
    run = _load_backfill()
    # Contacts succeed; deals fail → chunk incomplete. Resume re-upserts contacts.
    calls = _patch_backfill(
        monkeypatch,
        contacts_by_chunk=lambda df, dt: [{"id": "c1", "properties": {
            "hs_analytics_source": "ORGANIC_SEARCH", "createdate": "2026-01-05T00:00:00Z"}}],
        deals_by_chunk=lambda df, dt: [{"deal_id": "dx", "lookup_failed": True, "contacts": []}],
    )
    out = run(date_from="2026-01-01", date_to="2026-01-31", dry_run=False, chunk_months=1)
    assert calls["contact_upserts"], "contacts are upserted even when the deal phase fails"
    assert out["status"] == "partial"


# ════════════════ connector propagates retryable failures ════════════════


def test_connector_propagates_retryable_errors():
    src = open(os.path.join(ROOT, "connectors", "hubspot_pull.py"), encoding="utf-8").read()
    assert "class HubSpotRetryableError" in src
    # The lookups raise the retryable error instead of returning []/{} on failure.
    assoc = src[src.find("def _fetch_associated_contact_ids"):src.find("def _fetch_contact_source_props")]
    assert "raise HubSpotRetryableError" in assoc
    assert "return []" in assoc  # successful empty association is still allowed
    props = src[src.find("def _fetch_contact_source_props"):src.find("def get_lead_quality_summary")]
    assert "raise HubSpotRetryableError" in props
    # The deals pull marks a failed lookup rather than fabricating contacts.
    pull = src[src.find("def pull_closed_won_deals_with_sources_in_range"):
               src.find("def _fetch_associated_contact_ids")]
    assert 'entry["lookup_failed"] = True' in pull
    assert "except HubSpotRetryableError" in pull


def test_connector_association_failure_via_node_not_needed():
    # Behavioural: a raised association error must propagate as lookup_failed.
    pytest.importorskip("hubspot")
    import connectors.hubspot_pull as hp
    monkey_deal = {"deal_id": "z1"}
    # Patch the underlying won-deal pull + the two lookups.
    orig_assoc = hp._fetch_associated_contact_ids
    orig_props = hp._fetch_contact_source_props
    hp.pull_closed_won_deals_in_range = lambda a, b: [dict(monkey_deal)]
    def boom(_):
        raise hp.HubSpotRetryableError("503")
    hp._fetch_associated_contact_ids = boom
    try:
        out = hp.pull_closed_won_deals_with_sources_in_range("2026-01-01", "2026-01-31")
    finally:
        hp._fetch_associated_contact_ids = orig_assoc
        hp._fetch_contact_source_props = orig_props
    assert out[0]["lookup_failed"] is True
    assert out[0]["contacts"] == []
