"""PR-ADS-167A — the settled-day window rule (owner decision 2026-11-20).

Country Revenue & Spend, its mart country view, Dashboard Countries and the
geo reconciliation panel end a window that includes today at YESTERDAY in the
account time zone, for revenue and spend alike. Closed windows are unchanged.
Every other revenue page keeps its window; the one-day difference is declared
(``window.through_reason`` / ``window.window_range``) and the cross-page parity
audit compares the country consumers with the same days, never with a window
one day longer and never silently.

The PostgreSQL end-to-end cases (a deal closed today on the Deals page and not
on the country pages; before the morning sync) are in
``test_pr_ads_167a_settled_coverage_consumers_pg_integration.py``.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services import canonical_contract as contract  # noqa: E402
from services import canonical_revenue_service as canonical_revenue  # noqa: E402
from services import cross_page_parity_service as parity  # noqa: E402
from tests.canonical_ledger_fixtures import ledger_row, patch_canonical_ledger  # noqa: E402
from tests.test_pr_ads_138_dashboard_countries import NOW, _patch_durable  # noqa: E402


LONDON_OFFSET_SUMMER = 1   # BST


def _resolved(key, start, end, closed=False):
    return {"key": key, "label": key, "start_date": start, "end_date": end,
            "is_closed_window": closed, "timezone": "Europe/London"}


# ── the rule, pure ──────────────────────────────────────────────────────────
def test_01_an_open_window_ends_yesterday_and_says_so():
    out = contract.end_at_settled_day(_resolved("ytd", "2026-01-01", "2026-11-20"))
    assert (out["start_date"], out["end_date"]) == ("2026-01-01", "2026-11-19")
    assert out["account_today"] == "2026-11-20"
    assert out["through_reason"] == contract.THROUGH_TODAY_NOT_SETTLED == "today_not_settled"
    assert out["window_range"] == {
        "start": "2026-01-01", "through": "2026-11-19",
        "through_reason": "today_not_settled", "empty": False,
        "disclosure": ("Through 19 Nov 2026 — today is not included until tomorrow "
                       "morning's sync. Other revenue pages, such as Deals, already "
                       "count deals closed today.")}
    # Revenue bounds are on the ACCOUNT calendar: London midnight of the start
    # and of today (GMT in November, so UTC midnight).
    assert contract.settled_revenue_bounds(out) == (
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        datetime(2026, 11, 20, tzinfo=timezone.utc))


def test_02_a_closed_window_is_unchanged():
    src = _resolved("last_quarter", "2026-07-01", "2026-09-30", closed=True)
    out = contract.end_at_settled_day(src)
    assert (out["start_date"], out["end_date"]) == ("2026-07-01", "2026-09-30")
    assert out["through_reason"] is None
    assert out["window_range"]["through_reason"] is None
    assert out["window_range"]["disclosure"] is None
    # A closed window keeps the business windows' own bounds.
    assert contract.settled_revenue_bounds(out) is None


def test_03_all_time_keeps_an_open_start():
    out = contract.end_at_settled_day(_resolved("all_time", None, "2026-11-20"))
    assert (out["start_date"], out["end_date"]) == (None, "2026-11-19")
    assert out["window_range"]["empty"] is False


def test_04_the_first_day_of_a_quarter_has_no_settled_day_and_says_so():
    """1 October: the current quarter has no completed day yet. The window is
    empty — its exclusive end is its start — and the disclosure names it."""
    out = contract.end_at_settled_day(_resolved("current_quarter", "2026-10-01",
                                                "2026-10-01"))
    assert out["end_date"] == "2026-09-30"
    assert out["window_range"]["empty"] is True
    assert out["window_range"]["disclosure"].startswith(
        "No completed day in this window yet — today (1 Oct 2026) is not included")
    start, end = contract.settled_revenue_bounds(out)
    assert start == end == datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc)   # BST


def test_05_the_account_day_decides_which_day_is_today():
    """23:30 UTC on 30 June under BST is 1 July in the account: YTD ends on
    30 June, and the current quarter (Q3) has no settled day."""
    late = datetime(2026, 6, 30, 23, 30, tzinfo=timezone.utc)
    ytd = contract.resolve_settled_window("ytd", now=late, account_time_zone="Europe/London")
    assert (ytd["account_today"], ytd["end_date"]) == ("2026-07-01", "2026-06-30")
    q = contract.resolve_settled_window("current_quarter", now=late,
                                        account_time_zone="Europe/London")
    assert q["start_date"] == "2026-07-01" and q["window_range"]["empty"] is True
    lq = contract.resolve_settled_window("last_quarter", now=late,
                                         account_time_zone="Europe/London")
    assert (lq["start_date"], lq["end_date"], lq["through_reason"]) == (
        "2026-04-01", "2026-06-30", None)


# ── the reader contract reads the settled days on the ACCOUNT calendar ──────
def _deal(idx, close):
    return ledger_row(f"d{idx}", amount_raw=100.0, revenue_usd=100.0,
                      currency_status="verified_usd",
                      currency_reason="deal_currency_is_usd",
                      deal_close_date=close, country_raw="United States",
                      acquisition_group="google_ads", attribution_status="attributed",
                      gclid=f"g{idx}")


def _settled_deals(key, now):
    q = contract.resolve_settled_window(key, now=now, account_time_zone="Europe/London")
    out = canonical_revenue.load_won_deals(key, now=now,
                                           bounds=contract.settled_revenue_bounds(q))
    return q, sorted(d["deal_id"] for d in out.get("deals") or []), out


def test_06_bounds_replace_the_named_window_and_keep_its_key(monkeypatch):
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    patch_canonical_ledger(monkeypatch, [
        _deal(1, "2026-06-21T10:00:00+00:00"), _deal(2, "2026-06-22T09:00:00+00:00")])
    full = canonical_revenue.load_won_deals("ytd", now=now)
    assert sorted(d["deal_id"] for d in full["deals"]) == ["d1", "d2"]
    q, ids, cut = _settled_deals("ytd", now)
    assert ids == ["d1"]
    assert cut["window"] == "ytd"                      # still the named window
    assert cut["window_end"] == "2026-06-21T23:00:00+00:00"   # London midnight, BST
    assert cut["publication"]["closed_won_deals"]["status"] == "published"


def test_06b_a_deal_at_0020_bst_today_is_not_in_through_yesterday(monkeypatch):
    """Reviewer probe (audit167a/test_probe_a). 00:50 BST on 16 Jul: a deal
    closed at 00:20 BST is TODAY's on the account calendar, so it is not in
    "Through 15 Jul 2026" — on the UTC calendar it was 15 Jul and was counted."""
    now = datetime(2026, 7, 15, 23, 50, tzinfo=timezone.utc)
    patch_canonical_ledger(monkeypatch, [_deal(1, "2026-07-15T23:20:00+00:00"),
                                         _deal(3, "2026-06-30T23:30:00+00:00")])
    q, ids, _ = _settled_deals("current_quarter", now)
    assert q["window_range"]["disclosure"].startswith("Through 15 Jul 2026")
    assert "d1" not in ids
    # 00:30 BST on 1 Jul is the quarter's first day on the account calendar:
    # in, although it is 30 Jun on the UTC calendar.
    assert "d3" in ids
    # The same at 09:00 the next morning.
    q, ids, _ = _settled_deals("current_quarter",
                               datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc))
    assert ids == ["d3"]


def test_06c_the_first_hour_of_a_quarter_publishes_none_of_last_quarter(monkeypatch):
    """Reviewer probe p2 (pure half). 23:30 UTC on 30 Jun is 00:30 BST on
    1 Jul: the current quarter has no settled day. The key resolved on the UTC
    day was still Q2, and its deals were published under Q3's empty window."""
    late = datetime(2026, 6, 30, 23, 30, tzinfo=timezone.utc)
    patch_canonical_ledger(monkeypatch, [_deal(1, "2026-05-10T10:00:00+00:00"),
                                         _deal(2, "2026-06-30T22:10:00+00:00")])
    q, ids, out = _settled_deals("current_quarter", late)
    assert q["window_range"]["empty"] is True
    assert ids == []
    assert out["window_start"] == out["window_end"] == "2026-06-30T23:00:00+00:00"


def test_06d_bounds_can_never_widen_a_named_window(monkeypatch):
    """Final review A. ``bounds=(2020-01-01, 2030-01-01)`` on Current Quarter
    returned 2024 and 2026 deals labelled "Current Quarter". Bounds that do
    not sit inside the named window (account-calendar start allowed) are
    refused as an unknown window; the settled bounds still publish."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    patch_canonical_ledger(monkeypatch, [_deal(1, "2024-03-01T10:00:00+00:00"),
                                         _deal(2, "2026-07-10T10:00:00+00:00")])
    utc = timezone.utc
    for bad in ((datetime(2020, 1, 1, tzinfo=utc), datetime(2030, 1, 1, tzinfo=utc)),
                (datetime(2026, 6, 30, 23, tzinfo=utc), datetime(2026, 7, 17, tzinfo=utc)),
                (datetime(2026, 6, 30, 21, tzinfo=utc), datetime(2026, 7, 15, 23, tzinfo=utc)),
                (datetime(2026, 7, 10, tzinfo=utc), datetime(2026, 7, 9, tzinfo=utc)),
                (None, datetime(2026, 7, 15, 23, tzinfo=utc))):
        out = canonical_revenue.load_won_deals("current_quarter", now=now, bounds=bad)
        assert out["available"] is False, bad
        assert out["reason"] == canonical_revenue.REASON_UNKNOWN_WINDOW, bad
        assert out["deals"] == [] and out["won_deals"] is None, bad
    q, ids, out = _settled_deals("current_quarter", now)
    assert out["available"] is True and ids == ["d2"]


# ── the parity audit: declared, reproduced, compared on the same days ───────
def _open_country_deals():
    return [ledger_row(f"d{i}", amount_raw=1000.0, revenue_usd=1000.0,
                       currency_status="verified_usd",
                       currency_reason="deal_currency_is_usd",
                       campaign_name_raw="Global Competitors",
                       country_raw="United States", acquisition_group="google_ads",
                       attribution_status="attributed", gclid=f"g{i}")
            for i in (1, 2)]


def test_07_the_country_consumers_declare_a_window_the_audit_reproduces(monkeypatch):
    _patch_durable(monkeypatch)
    patch_canonical_ledger(monkeypatch, _open_country_deals())
    out = parity.audit_window("ytd", now=NOW)
    rows = {r["consumer"]: r for r in out["consumer_windows"]}
    for name in ("dashboard/countries", parity.CONSUMER_SETTLED_REFERENCE):
        assert rows[name]["through_reason"] == "today_not_settled"
        assert rows[name]["window_end"] == out["settled_window"]["end_date"] == "2026-06-21"
    assert rows["dashboard/overview"]["window_end"] == "2026-06-22"
    assert parity.V_WINDOW_MISMATCH not in out["violation_codes"]
    # Conservation is evaluated over the countries' own days.
    rule = next(c for c in out["conservation"] if c["total"] == "campaign_attributable_sqls")
    assert rule["total_source"] == f"{parity.CONSUMER_SETTLED_REFERENCE}:summary.sqls"


def test_08_a_settled_declaration_the_audit_cannot_reproduce_is_a_window_mismatch(
        monkeypatch):
    """A consumer that claims "today not settled" over the wrong days — here
    one that kept today — is not let off the window check by the claim."""
    _patch_durable(monkeypatch)
    patch_canonical_ledger(monkeypatch, _open_country_deals())
    real = parity._build_consumers

    def lying(window, now):
        built = real(window, now)
        built["dashboard/countries"]["payload"]["window"]["end_date"] = "2026-06-22"
        return built
    monkeypatch.setattr(parity, "_build_consumers", lying)
    out = parity.audit_window("ytd", now=NOW)
    assert parity.V_WINDOW_MISMATCH in out["violation_codes"]
    assert any(v.get("consumer") == "dashboard/countries"
               for v in out["violations"] if v["code"] == parity.V_WINDOW_MISMATCH)


def test_09_a_shorter_window_without_the_declaration_is_a_window_mismatch(monkeypatch):
    """Dropping the declaration does not make a yesterday-ending window agree
    with the others: undeclared, it is compared with them and differs."""
    _patch_durable(monkeypatch)
    patch_canonical_ledger(monkeypatch, _open_country_deals())
    real = parity._build_consumers

    def silent(window, now):
        built = real(window, now)
        built["dashboard/countries"]["payload"]["window"]["through_reason"] = None
        return built
    monkeypatch.setattr(parity, "_build_consumers", silent)
    out = parity.audit_window("ytd", now=NOW)
    assert parity.V_WINDOW_MISMATCH in out["violation_codes"]


def test_10_conservation_against_the_full_window_would_be_unequal_windows(monkeypatch):
    """The reference total is used only while it measured the SAME days as
    the parts; otherwise conservation is not evaluable, never "conserved"."""
    _patch_durable(monkeypatch)
    patch_canonical_ledger(monkeypatch, _open_country_deals())
    real = parity._build_consumers

    def drifted(window, now):
        built = real(window, now)
        built[parity.CONSUMER_SETTLED_REFERENCE]["payload"]["window"]["end_date"] = "2026-06-20"
        return built
    monkeypatch.setattr(parity, "_build_consumers", drifted)
    out = parity.audit_window("ytd", now=NOW)
    rule = next(c for c in out["conservation"] if c["total"] == "campaign_attributable_sqls")
    assert rule["total_value"] is None and rule["status"] == "not_evaluable"
    assert parity.V_WINDOW_MISMATCH in out["violation_codes"]


# ── the Countries page's geo reads share one snapshot (review observation) ──
def test_11_countries_reads_spend_identities_and_carriers_in_one_snapshot():
    """A sync naming a code-less criterion between the identities read and the
    carriers read could turn "Unavailable" into a false $0. The three readers
    share one REPEATABLE READ, READ ONLY connection inside
    ``repo.geo_read_snapshot()``, and the page opens it before the first."""
    import ast
    import inspect

    from db import revenue_repository as repo
    snap = inspect.getsource(repo.geo_read_snapshot)
    assert "REPEATABLE READ READ ONLY" in snap
    for fn in (repo.fetch_geo_daily_spend_by_country,
               repo.fetch_geo_criterion_identities, repo.fetch_geo_code_carriers):
        src = inspect.getsource(fn)
        assert "_geo_conn()" in src and "get_conn()" not in src, fn.__name__
    page = (ROOT / "services" / "country_evidence_service.py").read_text(encoding="utf-8")
    tree = ast.parse(page)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
              and "geo_read_snapshot" in ast.get_source_segment(page, n))
    body = ast.get_source_segment(page, fn)
    i_snap = body.index("geo_reads.enter_context(repo.geo_read_snapshot())")
    i_with = body.index("with contextlib.ExitStack() as geo_reads:")
    # PR-ADS-176A: the carriers read moved, with the zero rule, into the ONE
    # shared rule (`services.country_spend_zero_rule.assess`), which reads them
    # in the caller's snapshot. The page calls it inside the same block.
    reads = [body.index(r) for r in ("repo.fetch_geo_daily_spend_by_country(",
                                     "repo.fetch_geo_criterion_identities(",
                                     "zero_rule.assess(")]
    shared = (ROOT / "services" / "country_spend_zero_rule.py").read_text(encoding="utf-8")
    assess = shared[shared.index("def assess("):shared.index("\ndef zero_spend_verdict(")]
    assert "repo.fetch_geo_code_carriers(" in assess
    assert "repo.geo_read_snapshot()" not in assess.split("\"\"\"")[2]  # no snapshot of its own
    assert i_with < i_snap < min(reads)
    # All three reads sit inside the ExitStack block (indented under it).
    with_indent = len(body[:i_with].rsplit("\n", 1)[1])
    for r in reads:
        line_start = body.rfind("\n", 0, r) + 1
        assert r - line_start > with_indent, body[line_start:r + 40]


def test_12_a_settled_consumer_reading_the_utc_calendar_is_a_window_mismatch(monkeypatch):
    """Final review B. Right dates, wrong instants: Dashboard Countries
    declaring the settled days while its deals were read to UTC midnight is
    `consumer_windows_differ` — the audit compares the published instants with
    `settled_revenue_bounds` for the window it resolves itself."""
    _patch_durable(monkeypatch)
    patch_canonical_ledger(monkeypatch, _open_country_deals())
    clean = parity.audit_window("ytd", now=NOW)
    assert parity.V_WINDOW_MISMATCH not in clean["violation_codes"]
    real = parity._build_consumers

    def utc_calendar(window, now):
        built = real(window, now)
        built["dashboard/countries"]["payload"]["window"]["end_utc_exclusive"] = (
            "2026-06-22T00:00:00+00:00")
        return built
    monkeypatch.setattr(parity, "_build_consumers", utc_calendar)
    out = parity.audit_window("ytd", now=NOW)
    assert parity.V_WINDOW_MISMATCH in out["violation_codes"]
    assert any(v.get("consumer") == "dashboard/countries" and "instants" in v["detail"]
               for v in out["violations"] if v["code"] == parity.V_WINDOW_MISMATCH)


def test_13_country_revenue_and_spend_rows_withhold_what_the_gate_withholds(monkeypatch):
    """Final review D. Before the morning sync the mart country view's gate
    is not ready (`spend_not_reread_since_day_ended`), and its rows still
    carried GB spend 6000 / revenue 9000 / 1 deal / SQLs. Rows now carry no
    spend, ratio or country-attributed outcome, with the gate's reason; after
    the morning sync they publish."""
    import tests.test_pr_ads_125_revenue_decision_mart as t125
    from services.revenue_decision_mart import build_revenue_decision_mart
    early = [dict(t125.GEO_COVERAGE_COMPLETE[0],
                  fetched_at=datetime(2026, 6, 21, 6, 2, tzinfo=timezone.utc))]
    t125._patch_durable(monkeypatch, geo_coverage=early)
    out = build_revenue_decision_mart(view="country", window="ytd", now=t125.NOW)
    assert out["spend_truth"]["country_gap_codes"] == ["spend_not_reread_since_day_ended"]
    assert out["rows"], "the fixture has country rows"
    for r in out["rows"]:
        for f in ("spend", "spend_native", "spend_usd", "leads", "sqls",
                  "closed_won_deals", "customers", "won_revenue",
                  "period_revenue_per_ad_spend", "period_spend_per_customer_won"):
            assert r.get(f) is None, (r.get("country"), f, r.get(f))
        assert r["metrics"] is None
        assert r["withheld_reason"] == "spend_not_reread_since_day_ended"
    t125._patch_durable(monkeypatch)
    ready = build_revenue_decision_mart(view="country", window="ytd", now=t125.NOW)
    gb = next(r for r in ready["rows"] if r.get("country_code") == "GB")
    assert gb["spend"] is not None and gb["won_revenue"] is not None
    assert "withheld_reason" not in gb
