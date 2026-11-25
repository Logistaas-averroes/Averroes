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




# PR-ADS-177C: test_api_search_terms_data_quality_includes_total_rows_and_rows_returned
# and test_api_search_terms_summary_data_quality_has_total_and_rows_returned
# stood here. They pinned /api/search-terms and /api/search-terms/summary,
# orphaned endpoints retired in PR-ADS-177C (no page read them). The live
# Terms tab reads /api/search-term-evidence, whose complete-population count
# (pagination.total_count) and page count are asserted in
# tests/test_pr_ads_144_search_terms_revamp.py.
