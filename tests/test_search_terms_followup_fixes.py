from __future__ import annotations

import datetime as dt
import sys
import types


def test_daily_empty_search_terms_marks_success_not_success_empty(monkeypatch):
    import scheduler.daily as daily

    finish_calls = []

    monkeypatch.setattr(daily, "start_run", lambda *_: {})
    monkeypatch.setattr(daily, "finish_run", lambda *a, **k: None)
    monkeypatch.setattr(daily, "detect_anomalies", lambda *_: [])
    monkeypatch.setattr(daily, "detect_junk_terms", lambda *_: [])
    monkeypatch.setattr(daily, "check_crm_delta", lambda *_: {"alert": False, "message": ""})
    monkeypatch.setattr(daily, "check_budget_pacing", lambda *_: {})
    monkeypatch.setattr(daily, "save_daily_report", lambda *_: None)
    monkeypatch.setattr(daily, "deliver_report", lambda *_: None)
    monkeypatch.setattr(daily, "persistence_succeeded", lambda rows, written: (len(rows or []) == 0 and written == 0) or written > 0)

    monkeypatch.setattr("connectors.windsor_pull.pull_campaign_performance", lambda **_: [])
    monkeypatch.setattr("connectors.windsor_pull.pull_search_terms", lambda **_: [])
    monkeypatch.setattr("connectors.hubspot_pull.pull_paid_search_contacts", lambda **_: [])
    monkeypatch.setattr("connectors.hubspot_pull.get_lead_quality_summary", lambda *_: {})

    monkeypatch.setattr(daily.db_writers, "write_run", lambda *_: 1)
    monkeypatch.setattr(daily.db_writers, "start_sync_batch", lambda *a, **k: 1)
    monkeypatch.setattr(daily.db_writers, "write_leads", lambda *a, **k: 0)
    monkeypatch.setattr(daily.db_writers, "write_search_terms", lambda *a, **k: 0)
    monkeypatch.setattr(daily.db_writers, "update_run", lambda *a, **k: None)
    monkeypatch.setattr(
        daily.db_writers,
        "finish_sync_batch",
        lambda *a, **k: finish_calls.append(k) or True,
    )

    daily.run_daily_pulse()

    st_calls = [c for c in finish_calls if "search-term rows for daily pull" in str(c.get("error_message", ""))]
    assert st_calls
    assert st_calls[0]["status"] == "success"


# PR-ADS-165A: `test_weekly_empty_search_terms_marks_success_not_success_empty`
# stood here. It drove `scheduler.weekly.run_weekly_report`, which is deleted
# with the weekly job; the daily case above is the surviving half of the pair.


def test_api_search_terms_data_quality_includes_total_rows_and_rows_returned(monkeypatch):
    import api.server as server

    class FakeCursor:
        def __init__(self):
            self._last_query = ""
            self.description = []

        def execute(self, query, params=None):
            self._last_query = query

        def fetchone(self):
            if "SELECT COUNT(*), MAX(source_date)" in self._last_query:
                return (5, dt.date(2026, 5, 25))
            return None

        def fetchall(self):
            self.description = [
                ("id",), ("source_date",), ("campaign_name",), ("campaign_id",),
                ("ad_group",), ("keyword",), ("match_type",), ("search_term",),
                ("spend_usd",), ("clicks",), ("impressions",), ("conversions",),
                ("is_flagged_waste",), ("junk_category",), ("matched_pattern",), ("updated_at",),
            ]
            return [
                (10, dt.date(2026, 5, 25), "c1", "1", "ag", "kw", "broad", "term a", 1.0, 1, 10, 0.0, None, None, None, dt.datetime(2026, 5, 25, 10, 0, 0)),
                (9, dt.date(2026, 5, 24), "c1", "1", "ag", "kw", "broad", "term b", 2.0, 2, 20, 0.0, None, None, None, dt.datetime(2026, 5, 24, 10, 0, 0)),
                (8, dt.date(2026, 5, 23), "c1", "1", "ag", "kw", "broad", "term c", 3.0, 3, 30, 0.0, None, None, None, dt.datetime(2026, 5, 23, 10, 0, 0)),
            ]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setitem(sys.modules, "db.connection", types.SimpleNamespace(get_conn=lambda: FakeConn()))

    data = server.api_search_terms(
        user={"email": "x"},
        days=14,
        limit=2,
        waste_state=None,
        waste_only=False,
        campaign=None,
        match_type=None,
        q=None,
        min_spend=None,
        cursor=None,
    )

    dq = data["data_quality"]
    assert dq["rows_returned"] == 2
    assert dq["total_rows_in_window"] == 5
    assert dq["rows_in_window"] == 5


def test_api_search_terms_summary_data_quality_has_total_and_rows_returned(monkeypatch):
    import api.server as server

    class FakeCursor:
        def __init__(self):
            self.step = 0
            self._last_query = ""

        def execute(self, query, params=None):
            self.step += 1
            self._last_query = query

        def fetchone(self):
            if "COUNT(*) AS total_terms" in self._last_query:
                return (4, 4, 10.0, 5, 100, 1.0)
            return None

        def fetchall(self):
            if "GROUP BY is_flagged_waste" in self._last_query:
                return [(None, 4, 10.0)]
            return []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setitem(sys.modules, "db.connection", types.SimpleNamespace(get_conn=lambda: FakeConn()))

    data = server.api_search_terms_summary(
        user={"email": "x"},
        days=14,
        waste_state=None,
        waste_only=False,
        campaign=None,
        match_type=None,
        q=None,
        min_spend=None,
    )
    dq = data["data_quality"]
    assert dq["rows_in_window"] == 4
    assert dq["total_rows_in_window"] == 4
    assert dq["rows_returned"] == 4
