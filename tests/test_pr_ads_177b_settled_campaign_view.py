"""PR-ADS-177B — the mart's settled campaign view, and who reads it.

Owner decision 2026-11-24 (2), as the coordinator applied it (option B): ONLY
the Campaigns page and Dashboard → Campaigns read the campaign view over
settled days (``settled=True``). Overview, Revenue, Deals, Channels and the
Closed-Won Deals page keep the campaign / deal views through today — the
2026-11-20 decision stands for them.

These cases need no database: the mock fixtures of PR-ADS-137 / 154C-F3 /
177A-F2 drive the real builders and the real parity audit.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest

from services import revenue_decision_mart as mart

_FIRST_DAYS = [("current_quarter", datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)),
               ("ytd", datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc))]


def test_01_the_settled_campaign_view_is_asked_for_never_the_default():
    sig = inspect.signature(mart.build_revenue_decision_mart)
    assert sig.parameters["settled"].default is False
    assert sig.parameters["settled"].kind is inspect.Parameter.KEYWORD_ONLY
    assert mart.SETTLED_DAY_VIEWS == frozenset({"country"})
    assert mart.SETTLED_ON_REQUEST_VIEWS == frozenset({"campaign"})
    with pytest.raises(ValueError):
        mart.build_revenue_decision_mart(view="deal", window="ytd", settled=True)
    # The consumers that keep today never ask for it; the two that read the
    # settled view always do.
    import services.dashboard_campaigns_service as dc
    for name in ("dashboard_overview_service", "dashboard_revenue_service",
                 "dashboard_deals_service", "dashboard_channels_service"):
        src = inspect.getsource(__import__(f"services.{name}", fromlist=["x"]))
        assert "settled=True" not in src, name
    src = inspect.getsource(dc)
    assert src.count('build_revenue_decision_mart(view="campaign", window=window, now=now,\n'
                     '                                       settled=True)') == 1
    assert "now=prev_now, settled=True" in src


def test_02_the_route_passes_settled_only_when_asked():
    from api import server
    src = inspect.getsource(server.get_revenue_performance)
    assert "settled: bool = Query(default=False)" in src
    assert "settled=settled" in src
    src = inspect.getsource(server.get_revenue_performance_campaign_detail)
    assert "settled_days=settled" in src


def test_03_the_settle_rule_holds_campaign_spend_until_yesterday_is_re_read():
    """The shared rule (analyze_settled_coverage) over the campaign ledger,
    pure: fetched this morning = settled through yesterday; fetched yesterday
    = yesterday not settled."""
    from datetime import date
    from zoneinfo import ZoneInfo
    from services.google_ads_spend_service import analyze_settled_coverage
    london = ZoneInfo("Europe/London")
    now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
    chunk = {"chunk_start": "2026-10-01", "chunk_end": "2026-11-24", "status": "verified"}
    fresh = analyze_settled_coverage(date(2026, 10, 1), date(2026, 11, 23),
                                     [{**chunk, "fetched_at": "2026-11-24T09:00:00+00:00"}],
                                     london, now=now)
    stale = analyze_settled_coverage(date(2026, 10, 1), date(2026, 11, 23),
                                     [{**chunk, "fetched_at": "2026-11-23T09:00:00+00:00"}],
                                     london, now=now)
    assert fresh["complete"] is True
    assert stale["complete"] is False
    assert stale["unsettled_reason"] == "spend_not_reread_since_day_ended"
    # The mart's campaign spend block applies it only when asked.
    from services import revenue_attribution_service as ras
    sig = inspect.signature(ras.canonical_campaign_spend_block)
    assert sig.parameters["settled"].default is False


@pytest.mark.parametrize("window,now", _FIRST_DAYS)
def test_04_on_a_windows_first_day_the_settled_campaign_identities_are_exempt(
        monkeypatch, window, now):
    """No day of the window has ended: the settled campaign view and Dashboard
    → Campaigns withhold every figure with `no_settled_day_in_window` — by
    contract, in their metric_truth — and the parity audit's first-day
    exemption (its OWN settled range is empty) covers them."""
    from tests.test_pr_ads_177a_f2_truth_audit import _healthy_parity_at
    parity = _healthy_parity_at(monkeypatch, now)
    r = parity.audit_window(window, now=now)
    assert r["ok"] is True, r["violation_codes"]
    by = {m["metric"]: m for m in r["metrics"]}
    for key in ("google_ads_spend_usd__settled_days",
                "campaign_attributed_won_revenue_usd__settled_days",
                "campaign_attributed_closed_won_deals__settled_days",
                "campaign_attributed_customers__settled_days",
                "google_ads_cohort_sqls__settled_days"):
        assert by[key]["status"] == "not_evaluable_no_settled_day", (key, by[key]["status"])
    cert = {c["consumer"]: c for c in r["consumer_certification"]}
    for name in ("dashboard/campaigns", "revenue_decision_mart/campaign_settled"):
        assert cert[name]["certified"] is False
        assert cert[name]["not_evaluable_reason"] == "no_settled_day_in_window"
    # The through-today campaign view (Overview's) still certifies.
    assert cert["revenue_decision_mart"]["certified"] is True


def test_05_the_settled_view_withholds_everything_over_no_settled_day(monkeypatch):
    window, now = _FIRST_DAYS[0]
    from tests.test_pr_ads_177a_f2_truth_audit import _healthy_parity_at
    _healthy_parity_at(monkeypatch, now)
    out = mart.build_revenue_decision_mart(view="campaign", window=window, now=now,
                                           settled=True)
    assert out["settled_days"] is True
    assert out["window"]["window_range"]["empty"] is True
    assert out["rows"] == [] and out["campaign_residual_row"] is None
    for key, m in out["campaign_totals"]["metrics"].items():
        assert (m["status"], m["value"], m["reason"]) == (
            "unavailable", None, "no_settled_day_in_window"), key
    for key, c in out["metric_truth"].items():
        if c["truth_status"] != "ready":
            assert c["unavailable_reason"] == "no_settled_day_in_window", key
    from services.dashboard_campaigns_service import build_dashboard_campaigns
    dash = build_dashboard_campaigns(window, now=now)
    assert dash["campaigns"] == [] and dash["campaign_residual_row"] is None
    k = dash["kpis"]
    assert (k["verified_spend_usd"], k["sqls"], k["closed_won_deals"],
            k["won_revenue_usd"], k["period_revenue_per_ad_spend"]) == (None,) * 5
    # The through-today view of the same instant is unchanged (Overview's).
    today = mart.build_revenue_decision_mart(view="campaign", window=window, now=now)
    assert today["settled_days"] is False
    assert today["window"]["end_date"] == "2026-10-01"
