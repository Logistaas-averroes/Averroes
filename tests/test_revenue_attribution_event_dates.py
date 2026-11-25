"""
PR-ADS-109 — Revenue attribution event-date tests.

Proves leads/SQLs use the HubSpot business event date (contact_created_at), not
the scheduler sync date (run_date), and that paid-search / pseudo-campaign
filtering is applied. Without a live DB these assert the repository SQL shape and
the service's safety behavior with a mocked repository.
"""

import inspect
import os
import sys
from contextlib import contextmanager
from datetime import date, datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.canonical_ledger_fixtures import (  # noqa: E402
    canonical_ledger_patch, from_legacy_deal_rows,
)

import db.revenue_repository as repo
import db.writers as writers
import services.revenue_attribution_service as svc
from services.revenue_attribution_service import build_revenue_attribution

NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


# ── Repository SQL structural proofs ─────────────────────────────────────────


def test_lead_query_filters_by_contact_created_at_not_run_date():
    src = inspect.getsource(repo.fetch_lead_quality)
    assert "contact_created_at >=" in src
    assert "contact_created_at <" in src
    # run_date must not be used as the business-window filter anymore.
    assert "run_date >=" not in src
    assert "run_date <=" not in src


def test_lead_query_filters_paid_search_only():
    src = inspect.getsource(repo.fetch_lead_quality)
    assert "source_type = 'paid_search'" in src


def test_lead_query_excludes_pseudo_and_email_campaigns():
    src = inspect.getsource(repo.fetch_lead_quality)
    assert "NOT IN %s" in src           # pseudo-campaign exclusion
    assert "email_campaign" in src.lower()
    assert "campaign_name IS NOT NULL" in src


def test_pseudo_campaign_constant_covers_required_names():
    for name in ("(direct)", "(organic)", "(referral)", "(not set)", "(cross-network)"):
        assert name in repo._PSEUDO_CAMPAIGNS


# ── Writer persists the business event date ──────────────────────────────────


def test_write_leads_persists_contact_created_at_and_source():
    captured = {}

    class FakeCur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def executemany(self, sql, rows):
            captured["sql"] = sql
            captured["rows"] = rows

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return FakeCur()

    @contextmanager
    def fake_get_conn():
        yield FakeConn()

    with patch.object(writers, "get_conn", fake_get_conn):
        n = writers.write_leads(1, [{
            "id": "c1",
            "properties": {
                "hs_analytics_source": "PAID_SEARCH",
                "hs_analytics_source_data_1": "gulf",
                "createdate": "2026-05-01T00:00:00Z",
                "mql_status": "CLOSED - Sales Qualified",
                "ip_country": "Saudi Arabia",
            },
        }])
    assert n == 1
    assert "contact_created_at" in captured["sql"]
    assert "hs_analytics_source" in captured["sql"]
    row = captured["rows"][0]
    assert row[-1] == "PAID_SEARCH"          # hs_analytics_source
    assert isinstance(row[-2], datetime)      # contact_created_at parsed


def test_write_leads_null_event_date_when_createdate_missing():
    captured = {}

    class FakeCur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def executemany(self, sql, rows):
            captured["rows"] = rows

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return FakeCur()

    @contextmanager
    def fake_get_conn():
        yield FakeConn()

    with patch.object(writers, "get_conn", fake_get_conn):
        writers.write_leads(1, [{"id": "c2", "properties": {
            "hs_analytics_source": "PAID_SEARCH", "hs_analytics_source_data_1": "gulf"}}])
    assert captured["rows"][0][-2] is None  # contact_created_at null


# ── Service safety behavior with mocked repository ───────────────────────────


def _spend(rows=None):
    return {"available": True, "table": "geo",
            "rows": rows if rows is not None else [{"campaign_name": "gulf", "country": "Saudi Arabia", "spend": 1000.0}],
            "coverage_start": None, "coverage_end": None}


def _leads(safe=True, rows=None):
    return {"available": True, "table": "leads",
            "rows": rows if rows is not None else [{"campaign_name": "gulf", "country": "Saudi Arabia", "status_category": "qualified", "has_gclid": True}],
            "event_date_safe": safe, "lead_event_date_field_available": True,
            "missing_contact_created_at_count": 0 if safe else 42,
            "excluded_non_paid_count": 0, "excluded_pseudo_campaign_count": 0}


def _rev(rows=None):
    return {"available": True, "table": "gclid_attribution",
            "rows": rows if rows is not None else [],
            "coverage_start": None, "coverage_end": None}


def _cohort(leads):
    """PR-ADS-177A: the mart's leads / SQLs are the acquisition cohort. The
    fixture's contacts are served from the canonical funnel, created inside the
    window; the fixture's "unsafe" is the cohort's own withheld verdict (the
    real freshness rule over a sync state whose bootstrap is still running)."""
    from tests.cohort_fixture import (cohort_patch, contacts_from_lead_rows,
                                      healthy_sync_row, withheld_sync_row)
    leads = leads or _leads()
    return cohort_patch(
        contacts_from_lead_rows(leads["rows"],
                                created=datetime(2026, 5, 15, 12, tzinfo=timezone.utc)),
        as_of=NOW, sync_row=(healthy_sync_row(NOW) if leads["event_date_safe"]
                             else withheld_sync_row(NOW)))


def _build(window="current_quarter", leads=None, spend=None, rev=None):
    with patch("db.revenue_repository.fetch_campaign_country_spend", return_value=spend or _spend()), \
         patch("db.revenue_repository.fetch_lead_quality", return_value=leads or _leads()), \
         _cohort(leads), \
         canonical_ledger_patch(from_legacy_deal_rows((rev or _rev()).get("rows") or [])), \
         patch("db.revenue_repository.fetch_sync_state", return_value={"available": True, "datasets": {}}):
        return build_revenue_attribution(window, now=NOW)


def test_lead_metrics_withheld_when_event_date_unsafe():
    # PR-ADS-177A: the cohort is windowed on the contact's own creation date
    # (always the event grain); "unsafe" is the cohort verdict withholding.
    r = _build(leads=_leads(safe=False))
    assert r["lead_date_grain_status"] == "event_date"
    assert (r["cohort"]["sql_status"], r["cohort"]["sql_reason"]) == (
        "withheld", "source_bootstrap_incomplete")
    assert r["lead_metrics_status"] == "withheld"
    assert r["summary"]["leads"] is None
    assert r["summary"]["sqls"] is None
    assert any("withheld" in w.lower() for w in r["warnings"])


def test_lead_metrics_present_when_event_date_safe():
    r = _build(leads=_leads(safe=True))
    assert r["lead_date_grain_status"] == "event_date"
    assert r["lead_metrics_status"] == "db"
    assert r["summary"]["sqls"] == 1


def test_backfilled_old_contacts_do_not_pollute_window():
    """When the repo reports missing event dates (old synced rows), the window is
    unsafe and SQL metrics are withheld rather than counting sync-date ghosts."""
    r = _build(leads=_leads(safe=False))
    for row in r["campaigns"]:
        assert row["sqls"] is None
        assert row["leads"] is None


def test_windows_differ_when_event_dates_differ():
    # PR-ADS-177A: the acquisition cohort, windowed by each contact's own
    # creation date: one contact created this quarter, four created in 2025.
    from tests.cohort_fixture import cohort_patch, contacts_from_lead_rows
    row = {"campaign_name": "gulf", "country": "Saudi Arabia", "status_category": "qualified"}
    contacts = (contacts_from_lead_rows([row], created=datetime(2026, 5, 15, 12, tzinfo=timezone.utc))
                + contacts_from_lead_rows([row] * 4,
                                          created=datetime(2025, 5, 15, 12, tzinfo=timezone.utc)))

    with patch("db.revenue_repository.fetch_campaign_country_spend", return_value=_spend()), \
         cohort_patch(contacts, as_of=NOW), \
         canonical_ledger_patch(from_legacy_deal_rows(_rev().get("rows") or [])), \
         patch("db.revenue_repository.fetch_sync_state", return_value={"available": True, "datasets": {}}):
        cq = build_revenue_attribution("current_quarter", now=NOW)["summary"]["leads"]
        at = build_revenue_attribution("all_time", now=NOW)["summary"]["leads"]
    assert cq != at, "windows must not be identical when event-date data differs"
    assert (cq, at) == (1, 5)


def test_service_passes_event_window_dates_to_repo():
    # PR-ADS-177A: the window's days reach the acquisition-cohort read, as the
    # account-local instants of [start, end] inclusive.
    captured = {}

    def fake_contacts(start_at, end_before):
        captured["start"] = start_at
        captured["end"] = end_before
        return {"available": True, "rows": [], "missing_created_at": 0}

    with patch("db.revenue_repository.fetch_campaign_country_spend", return_value=_spend()), \
         patch("db.crm_funnel_repository.fetch_acquisition_cohort_contacts",
               side_effect=fake_contacts), \
         canonical_ledger_patch(from_legacy_deal_rows(_rev().get("rows") or [])), \
         patch("db.revenue_repository.fetch_sync_state", return_value={"available": True, "datasets": {}}):
        build_revenue_attribution("current_quarter", now=NOW)
    from services.marketing_outcome_cohort_service import window_instants
    assert (captured["start"], captured["end"]) == window_instants(
        date(2026, 4, 1), date(2026, 6, 20))
