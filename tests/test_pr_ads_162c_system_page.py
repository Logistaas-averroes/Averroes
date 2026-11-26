"""PR-ADS-162C — the System page: the platform's state in plain words.

One admin page answers three questions: can the numbers be trusted, is the data
up to date, and what needs fixing. It shows recorded facts and owns no rule:

* the certification line is the daily job's stored verdict — missing, old,
  future-dated, unrecognised or internally inconsistent is never a pass;
* each source line is the verdict the platform already publishes by (the
  contact assessor, the deal ledger's coverage gate and staleness limit, the
  spend coverage ledger) — only a proven-current source reads as healthy;
* "nothing to fix" is said only on a counted zero.

The fixtures are production-shaped: the certification summary is the row the
job stored on 2026-11-17, sync-state rows carry every column the gates read,
and the unavailable/crashed payloads are produced by the real audit code.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import audit_platform_certification as apc
from services import platform_certification_service as pcs
from services import system_overview_service as so
from tests.canonical_ledger_fixtures import READY_SYNC_STATE, fresh_sync_state

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 18, 4, 30, tzinfo=timezone.utc)

AUDIT_KEYS = ("reader_cutover", "closed_won_truth", "deal_company_associations",
              "crm_truth_exceptions", "marketing_outcome_cohorts",
              "campaign_evidence")

# The summary production recorded on 2026-11-17 (all six audits, the real
# crm_data_to_fix block; per-audit timing fields dropped).
FIX = {
    "by_family": {
        "customer_identity": {"by_reason": {
            "ambiguous_multiple_companies": 6, "unresolved_no_company": 5},
            "deals": 11},
        "revenue_proof": {"by_reason": {"no_amount": 14}, "deals": 14},
        "won_deal_membership": {"by_reason": {}, "deals": 0},
    },
    "dataset_blockers": [],
    "deals_with_any_exception": 25,
    "windows": [
        {"window": "evidence/7d", "customers": "published", "revenue_usd": "published"},
        {"window": "evidence/180d", "customers": "published", "revenue_usd": "published"},
        {"window": "evidence/all_time", "customers": "withheld", "revenue_usd": "withheld"},
        {"window": "business/current_quarter", "customers": "published", "revenue_usd": "published"},
        {"window": "business/ytd", "customers": "withheld", "revenue_usd": "published"},
        {"window": "business/all_time", "customers": "withheld", "revenue_usd": "withheld"},
    ],
}
SUMMARY = {
    "verdict": "CERTIFIED", "exit_code": 0,
    "audits": [{"key": k, "outcome": "holds", "exit_code": 0, "detail": None,
                "violations": [], "unavailable": []} for k in AUDIT_KEYS],
    "crm_data_to_fix": FIX,
}


def _read(hours_ago=21.0, exit_code=0, verdict="CERTIFIED", summary=None):
    finished = NOW - timedelta(hours=hours_ago)
    return {"available": True, "row": {
        "started_at": finished - timedelta(minutes=10), "finished_at": finished,
        "exit_code": exit_code, "verdict": verdict,
        "summary": SUMMARY if summary is None else summary}}


# ── the certification card ────────────────────────────────────────────────

def test_01_a_recent_certified_run_is_reported_certified():
    block = so.certification_block(_read(), NOW)
    assert block["state"] == "certified"
    assert block["verdict"] == "CERTIFIED" and block["age_hours"] == 21.0
    assert [a["key"] for a in block["audits"]] == list(AUDIT_KEYS)
    assert block["crm_data_to_fix"]["deals_with_any_exception"] == 25
    assert block["could_not_run"] is False


def test_02_no_recorded_run_is_not_checked_never_a_pass():
    block = so.certification_block({"available": True, "row": None}, NOW)
    assert block["state"] == "not_checked" and block["verdict"] is None


def test_03_an_old_certified_run_is_not_todays_verdict():
    """The daily job stopped: the last row says CERTIFIED, but it is two days
    old. The page must not go on saying yes."""
    block = so.certification_block(_read(hours_ago=49), NOW)
    assert block["state"] == "not_checked"
    assert block["verdict"] == "CERTIFIED"          # kept for context only


def test_04_the_age_limit_covers_one_daily_cycle_and_not_two():
    assert so.certification_block(_read(hours_ago=25.9), NOW)["state"] == "certified"
    assert so.certification_block(_read(hours_ago=26.1), NOW)["state"] == "not_checked"
    assert 24 < so.CERTIFICATION_MAX_AGE_HOURS < 48


@pytest.mark.parametrize("exit_code, verdict, state", [
    (1, "VIOLATION", "violation"),
    (2, "NOT CERTIFIED", "not_certified"),
    (7, "CERTIFIED", "not_certified"),       # unrecognised exit code
    (None, "CERTIFIED", "not_certified"),    # no exit code at all
])
def test_05_only_exit_code_zero_is_certified(exit_code, verdict, state):
    block = so.certification_block(_read(exit_code=exit_code, verdict=verdict), NOW)
    assert block["state"] == state


def test_06_an_unreadable_database_is_unavailable_not_a_pass():
    block = so.certification_block({"available": False, "row": None}, NOW)
    assert block["state"] == "unavailable" and block["audits"] == []


def test_07_a_run_stamped_in_the_future_is_not_trusted():
    assert so.certification_block(_read(hours_ago=-3), NOW)["state"] == "not_checked"


def test_08_a_run_with_no_finish_time_is_not_checked():
    read = _read()
    read["row"]["finished_at"] = None
    assert so.certification_block(read, NOW)["state"] == "not_checked"


@pytest.mark.parametrize("audits", [
    [],                                                    # a pass with no audits
    [{"key": "closed_won_truth", "outcome": "holds"},
     {"key": "campaign_evidence", "outcome": "unavailable"}],
    [{"key": "closed_won_truth", "outcome": "violation"}],
    [{"key": "closed_won_truth"}],                          # outcome missing
])
def test_09_exit_zero_without_every_audit_holding_is_not_a_pass(audits):
    """``combine`` certifies only a non-empty list in which everything holds.
    A stored row that says 0 without that evidence is not repeated as a yes."""
    summary = {**SUMMARY, "audits": audits}
    block = so.certification_block(_read(summary=summary), NOW)
    assert block["state"] == "not_certified"


def test_10_a_summary_stored_as_text_cannot_certify():
    read = _read()
    read["row"]["summary"] = json.dumps(SUMMARY)       # str, not a dict
    assert so.certification_block(read, NOW)["state"] == "not_certified"


def test_11_a_crashed_run_recorded_by_the_real_job_says_nothing_was_checked():
    rows = []

    def writer(started, finished, exit_code, verdict, summary, *, run_reason):
        rows.append({"started_at": started, "finished_at": finished,
                     "exit_code": exit_code, "verdict": verdict,
                     "summary": summary})
        return True

    def broken():
        raise RuntimeError("database not ready")
    ticks = iter([NOW - timedelta(minutes=20), NOW - timedelta(minutes=19)])
    pcs.run_and_record(runner=broken, writer=writer, now=lambda: next(ticks))
    block = so.certification_block({"available": True, "row": rows[0]}, NOW)
    assert block["state"] == "not_certified" and block["could_not_run"] is True
    html = _render({"certification": block, "sources": []})
    assert "The daily check could not run, so nothing was checked." in html
    assert "Yes —" not in html and "system-card--ok" not in html


def test_12_times_are_reported_in_utc_whatever_the_session_zone():
    read = _read()
    amman = timezone(timedelta(hours=3))
    read["row"]["finished_at"] = datetime(2026, 11, 17, 10, 40, tzinfo=amman)
    block = so.certification_block(read, NOW)
    assert block["checked_at"] == "2026-11-17T07:40:00+00:00"
    assert "2026-11-17 07:40 UTC" in _render(
        {"certification": block, "sources": []})


# ── HubSpot contacts: the canonical assessor, on production-shaped rows ────

def _contact_state(**over):
    row = {"scope": "contacts", "bootstrap_status": "complete",
           "bootstrap_completed_at": "2026-11-17T09:00:00+00:00",
           "last_incremental_at": (NOW - timedelta(hours=22)).isoformat(),
           "last_successful_incremental_at": (NOW - timedelta(hours=22)).isoformat(),
           "last_incremental_status": "success", "last_status": "success",
           "last_sync_mode": "incremental", "last_error": None}
    row.update(over)
    return {"available": True, "row": row}


CONTACT_CASES = [
    ("current", _contact_state(), "Up to date"),
    ("late", _contact_state(
        last_successful_incremental_at=(NOW - timedelta(hours=60)).isoformat()),
     "Late — no successful update in the last 36 hours"),
    ("not_proven", _contact_state(last_incremental_status="failed"),
     "The last update failed"),
    ("not_proven", _contact_state(bootstrap_status="in_progress"),
     "History is still being copied from HubSpot"),
    ("not_proven", _contact_state(last_successful_incremental_at=None,
                                  last_incremental_status=None,
                                  last_sync_mode="bootstrap"),
     "No successful update has run yet"),
    ("not_proven", {"available": True, "row": None}, "No update has run yet"),
    ("unavailable", {"available": False, "row": None}, "Could not be read"),
]


@pytest.mark.parametrize("status, state, phrase", CONTACT_CASES)
def test_13_contacts_follow_the_real_assessor(monkeypatch, status, state, phrase):
    from db import crm_funnel_repository as repo
    monkeypatch.setattr(repo, "fetch_contact_funnel_sync_state", lambda **kw: state)
    src = so._contacts_source(NOW)
    assert src["status"] == status
    row = _render({"certification": {}, "sources": [src]})
    line = re.search(r"<tr>.*?</tr>", row, re.S).group(0)
    assert phrase in line
    assert ("system-source--ok" in line) is (status == "current")
    if status != "current":
        assert "Up to date" not in line


def test_14_an_unreadable_contact_state_is_not_called_a_failed_update(monkeypatch):
    """`None` is about us, `False` is about the pipeline."""
    from services import marketing_outcome_cohort_service as cohort

    def boom(now=None):
        return {"fresh": None, "reason": "source_freshness_unreadable",
                "last_successful_incremental_at": None, "age_hours": None}
    monkeypatch.setattr(cohort, "read_freshness", boom)
    src = so._contacts_source(NOW)
    assert src["status"] == "unavailable"
    html = _render({"certification": {}, "sources": [src]})
    line = re.search(r"<tr>.*?</tr>", html, re.S).group(0)
    assert "Could not be read" in line and "failed" not in line


# ── HubSpot deals: the ledger's own coverage gate and staleness limit ──────

def _deal_state(row):
    return {"available": True, "row": row}


def _deal_cases():
    t = lambda h: (NOW - timedelta(hours=h)).isoformat()   # noqa: E731
    return [
        # (id, sync state, expected status, has a success time, phrase)
        ("healthy", _deal_state(fresh_sync_state(NOW, hours_ago=22)),
         "current", True, "Up to date"),
        ("succeeded_a_week_ago", _deal_state(fresh_sync_state(NOW, hours_ago=168)),
         "late", True, "Late — no successful update in the last 36 hours"),
        ("incremental_failed_then_bootstrap_rerun", _deal_state(dict(
            READY_SYNC_STATE, bootstrap_completed_at=t(200),
            bootstrap_started_at=t(206), last_incremental_at=t(48),
            last_status="success", last_sync_mode="bootstrap")),
         "not_proven", False, "Not confirmed — the last update did not succeed"),
        ("last_incremental_failed", _deal_state(dict(
            fresh_sync_state(NOW, hours_ago=3), last_status="failed",
            last_error="HubSpot 502")),
         "not_proven", False, "Not confirmed — the last update did not succeed"),
        ("last_incremental_partial", _deal_state(dict(
            fresh_sync_state(NOW, hours_ago=3), last_status="partial")),
         "not_proven", False, "Not confirmed — the last update did not succeed"),
        ("bootstrap_done_no_incremental", _deal_state(dict(
            READY_SYNC_STATE, last_incremental_at=None,
            last_sync_mode="bootstrap")),
         "not_proven", False, "Not confirmed — the last update did not succeed"),
        ("mid_bootstrap", _deal_state(dict(
            READY_SYNC_STATE, bootstrap_status="in_progress",
            bootstrap_completed_at=None, last_incremental_at=None,
            last_sync_mode="bootstrap", last_status="running")),
         "not_proven", False, "History is still being copied from HubSpot"),
        ("never_synced", {"available": True, "row": None},
         "not_proven", False, "Not confirmed — the last update did not succeed"),
        ("unreadable", {"available": False, "row": None},
         "unavailable", False, "Could not be read"),
    ]


@pytest.mark.parametrize("name, state, status, has_time, phrase",
                         _deal_cases(), ids=[c[0] for c in _deal_cases()])
def test_15_deals_follow_the_ledgers_own_gate(monkeypatch, name, state, status,
                                             has_time, phrase):
    from db import deal_ledger_repository as ledger
    monkeypatch.setattr(ledger, "fetch_sync_state", lambda: state)
    src = so._deals_source(NOW)
    assert src["status"] == status, src
    # A time is shown only for a PROVEN successful incremental.
    assert (src["last_success_at"] is not None) is has_time
    html = _render({"certification": {}, "sources": [src]})
    line = re.search(r"<tr>.*?</tr>", html, re.S).group(0)
    assert phrase in line
    assert ("system-source--ok" in line) is (status == "current")
    if status != "current":
        assert "Up to date" not in line and "succeeded" not in line
        if not has_time:
            assert "No successful update on record" in line


def test_16_the_page_and_the_revenue_pages_reach_the_same_deal_verdict(monkeypatch):
    """The System page may never call the ledger current while the reader
    contract withholds every closed-won figure for it, or the reverse."""
    from db import deal_ledger_repository as ledger
    from services import canonical_customer_revenue_service as ccr
    from services.revenue_reconciliation_service import check_sync_coverage
    for name, state, *_ in _deal_cases():
        monkeypatch.setattr(ledger, "fetch_sync_state", lambda s=state: s)
        findings = check_sync_coverage(state)
        block = ccr.freshness_block(state.get("row"), findings, NOW)
        publishes = (block["coverage_proven"] is True
                     and ccr.ledger_staleness(block)["stale"] is False)
        assert (so._deals_source(NOW)["status"] == "current") is publishes, name


# ── Google Ads spend: the coverage ledger, last seven complete days ────────

YESTERDAY = date(2026, 11, 17)           # account day (Europe/London) before NOW


def _chunk(start, end, status="verified"):
    return {"chunk_start": start, "chunk_end": end, "status": status,
            "rows_written": 54, "cost_micros_total": 1, "customer_id": "3059734490"}


def _spend(monkeypatch, chunks, *, available=True, newest=None):
    from db import revenue_repository as repo
    seen = {}

    def coverage(start, end, customer_id=None):
        seen.update(start=start, end=end)
        return {"available": available, "chunks": chunks}
    monkeypatch.setattr(repo, "fetch_spend_coverage", coverage)
    monkeypatch.setattr(repo, "fetch_spend_coverage_recency", lambda cid=None: {
        "available": True,
        "chunk_end": newest[0] if newest else None,
        "updated_at": newest[1] if newest else None})
    src = so._spend_source(NOW)
    return src, seen


def _line(src) -> str:
    html = _render({"certification": {}, "sources": [src]})
    return re.search(r"<tr>.*?</tr>", html, re.S).group(0)


def test_17_spend_is_asked_for_the_last_seven_complete_account_days(monkeypatch):
    fetched = NOW - timedelta(hours=22)
    src, seen = _spend(monkeypatch, [_chunk("2026-11-11", "2026-11-17")],
                       newest=("2026-11-17", fetched))
    assert (seen["start"], seen["end"]) == (date(2026, 11, 11), YESTERDAY)
    assert src["status"] == "current" and src["complete_through"] == "2026-11-17"
    line = _line(src)
    assert "Complete through 2026-11-17" in line and "system-source--ok" in line
    assert "Last successful update 22 hours ago" in line


def test_18_a_spend_feed_stopped_five_days_ago_reads_late(monkeypatch):
    src, _ = _spend(monkeypatch, [_chunk("2026-11-06", "2026-11-12")],
                    newest=("2026-11-12", NOW - timedelta(days=5, hours=22)))
    assert src["status"] == "late" and src["complete_through"] is None
    assert src["missing_ranges"] == [{"start": "2026-11-13", "end": "2026-11-17"}]
    line = _line(src)
    assert "Late — no spend recorded for 2026-11-13 to 2026-11-17" in line
    assert "system-source--ok" not in line and "Complete through" not in line


def test_19_refilling_an_old_month_does_not_make_a_dead_feed_look_fresh(monkeypatch):
    """The daily feed died eight days ago; an old month was refilled twenty
    minutes ago. Coverage of the recent days is what is judged."""
    src, _ = _spend(monkeypatch, [_chunk("2026-11-03", "2026-11-09")],
                    newest=("2026-11-09", NOW - timedelta(days=8)))
    assert src["status"] == "late"
    line = _line(src)
    assert "less than an hour ago" not in line and "system-source--ok" not in line


def test_20_an_unresolved_failed_fetch_and_an_unreadable_ledger(monkeypatch):
    src, _ = _spend(monkeypatch, [_chunk("2026-11-11", "2026-11-16"),
                                  _chunk("2026-11-11", "2026-11-17", "failed")],
                    newest=("2026-11-16", NOW - timedelta(hours=46)))
    assert src["status"] == "late" and src["unresolved_failed_chunks"] == 1
    src, _ = _spend(monkeypatch, [], available=False)
    assert src["status"] == "unavailable"
    line = _line(src)
    assert "Could not be read" in line and "system-source--ok" not in line
    src, _ = _spend(monkeypatch, [])                     # an empty ledger
    assert src["status"] == "late" and src["last_success_at"] is None


def test_21_one_broken_source_does_not_take_the_page_down(monkeypatch):
    monkeypatch.setattr(so, "read_latest_certification", lambda: _read())

    def boom(now):
        raise RuntimeError("deal ledger exploded")
    monkeypatch.setattr(so, "_contacts_source", lambda now: {
        "key": "hubspot_contacts", "label": "HubSpot contacts",
        "status": "current"})
    monkeypatch.setattr(so, "_deals_source", boom)
    monkeypatch.setattr(so, "_spend_source", lambda now: {
        "key": "google_ads_spend", "label": "Google Ads spend", "status": "late"})
    # PR-ADS-171A: the page gained a fourth source, Meta Ads spend. It is
    # stubbed like the others so this case stays about one broken source; its
    # own states are tested in tests/test_pr_ads_171a_*.
    monkeypatch.setattr(so, "_meta_spend_source", lambda now: {
        "key": "meta_ads_spend", "label": "Meta Ads spend",
        "status": "not_connected"})
    page = so.build_system_overview(NOW)
    assert page["certification"]["state"] == "certified"
    assert [(s["key"], s["status"]) for s in page["sources"]] == [
        ("hubspot_contacts", "current"), ("hubspot_deals", "unavailable"),
        ("google_ads_spend", "late"), ("meta_ads_spend", "not_connected")]
    assert page["external_calls_performed"] is False


def test_22_a_status_the_page_has_never_seen_is_a_warning():
    for key in ("hubspot_contacts", "hubspot_deals", "google_ads_spend", "new"):
        for status in ("fine", "ok", None, "CURRENT"):
            line = _line({"key": key, "label": "X", "status": status,
                          "complete_through": "2026-11-17"})
            assert "system-source--warn" in line and "system-source--ok" not in line


# ── the endpoint ──────────────────────────────────────────────────────────

def test_23_the_endpoint_is_admin_only_and_read_only(monkeypatch):
    from fastapi.testclient import TestClient
    import api.server as server
    monkeypatch.setenv("ADMIN_API_TOKEN", "secret-token")
    monkeypatch.setattr(so, "build_system_overview",
                        lambda now=None: {"certification": {"state": "not_checked"},
                                          "sources": []})
    client = TestClient(server.app)
    assert client.get("/api/system/overview").status_code == 401
    assert client.get("/api/system/overview", headers={
        "Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/system/overview", headers={
        "X-API-Token": "secret-token"}).status_code == 401
    ok = client.get("/api/system/overview",
                    headers={"Authorization": "Bearer secret-token"})
    assert ok.status_code == 200
    assert ok.json()["certification"]["state"] == "not_checked"
    assert client.post("/api/system/overview", headers={
        "Authorization": "Bearer secret-token"}).status_code == 405
    service = (ROOT / "services/system_overview_service.py").read_text()
    for verb in ("INSERT", "UPDATE ", "DELETE", "hubspot_pull", "google_ads_pull"):
        assert verb not in service, verb


# ── the page, rendered by the real code ───────────────────────────────────

def _render(payload: dict) -> str:
    """Evaluate the real renderSystemOverview from static/app.js in node."""
    src = (ROOT / "static/app.js").read_text()
    start = src.index("const SYSTEM_CHECK_LABELS = {")
    end = src.index("let _systemReqSeq = 0;")
    esc_start = src.index("function escapeHtml(str) {")
    esc_end = src.index("\n}\n", esc_start) + 3
    script = (src[esc_start:esc_end] + src[start:end]
              + "\nprocess.stdout.write(renderSystemOverview("
              + json.dumps(payload, default=str) + "));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _fix_card(html: str) -> str:
    return html[html.index("What needs fixing"):html.index("Advanced tools")]


def _payload(**cert):
    block = so.certification_block(_read(), NOW)
    block.update(cert)
    return {"certification": block, "sources": []}


def test_24_a_certified_day_reads_as_a_plain_yes_with_what_to_fix():
    html = _render(_payload())
    assert "Yes — the latest daily check passed" in html
    assert "system-card--ok" in html
    assert html.count("Passed — ") == 6                    # all six audits named
    fix = _fix_card(html)
    assert "<strong>14</strong> won deals have no amount" in fix
    assert "<strong>6</strong> won deals are linked to more than one company" in fix
    assert "<strong>5</strong> won deals are not linked to any company" in fix
    assert "Customer counts are hidden for: all time, year to date." in fix
    assert "Revenue is hidden for: all time." in fix
    assert "As of the last daily check, 21 hours ago." in fix
    assert 'data-system-goto="health"' in fix
    # plain words only: no audit module names or reason codes reach the page
    for jargon in ("audit_", "reader_cutover", "no_amount", "unresolved_",
                   "business/ytd", "by_reason"):
        assert jargon not in html, jargon


def test_25_the_pass_wording_claims_only_what_the_six_audits_check():
    """No audit reads a spend table, the Channels page's contact counts, the
    Pipeline page's lead counts or search terms. The page must say so, and
    must not promise them. PR-ADS-181A: the wording named the Channels page
    whole and the retired Leads page. The reader-cutover audit's evidence pass
    (PR-ADS-168A) does check the Channels page's won-deal figures, so the
    exclusion is its contact counts; Leads is the Pipeline page since
    PR-ADS-178B."""
    html = _render(_payload())
    card = html[:html.index("Is the data up to date?")]
    assert ("It does not re-check ad spend, the Channels page's contact counts, "
            "the Pipeline page's lead counts or search terms") in card
    assert "Leads or search terms" not in card
    assert "cannot see a deal or contact that was never entered in HubSpot" in card
    for overclaim in ("spend and results add up", "Google Ads records",
                      "Every figure", "every number", "today"):
        assert overclaim not in card, overclaim
    # 5 won deals have no company on this very page: the label must not say
    # every won deal is tied to one.
    assert "A customer count is shown only where every won deal" in card
    assert "Each won deal is tied to its company" not in card


@pytest.mark.parametrize("state, headline", [
    ("violation", "No — a daily check failed"),
    ("not_certified", "Not confirmed"),
    ("not_checked", "Not checked in the last day"),
    ("unavailable", "could not be read"),
    ("something_new", "could not be read"),      # an unknown state
])
def test_26_no_other_state_can_render_as_a_yes(state, headline):
    html = _render(_payload(state=state))
    assert headline in html
    assert "Yes —" not in html and "system-card--ok" not in html


def test_27_an_unreadable_result_does_not_claim_no_check_was_recorded():
    block = so.certification_block({"available": False, "row": None}, NOW)
    html = _render({"certification": block, "sources": []})
    assert "No check has been recorded yet" not in html
    recorded_none = so.certification_block({"available": True, "row": None}, NOW)
    assert "No check has been recorded yet" in _render(
        {"certification": recorded_none, "sources": []})


def test_28_a_failed_check_and_an_unfinished_check_are_named_apart():
    audits = [{"key": "closed_won_truth", "outcome": "violation"},
              {"key": "campaign_evidence", "outcome": "holds"},
              {"key": "crm_truth_exceptions", "outcome": "unavailable"},
              {"key": "reader_cutover", "outcome": "crashed"}]
    html = _render(_payload(state="violation", audits=audits))
    assert "Failed — Won-deal, customer and revenue figures match" in html
    assert "Could not finish — Every hidden customer or revenue figure" in html
    assert "Could not finish — Every page reads won deals" in html
    assert "The Campaigns page shows only figures it can prove" not in html


def test_29_the_real_unavailable_audit_never_reads_as_nothing_to_fix():
    """B1 of the review. The CRM Truth Exceptions audit could not reach the
    database: it prints this payload and exits 2. The certification stores a
    block with a NULL count and empty lists — which is 'nobody looked'."""
    stdout = json.dumps({"available": False, "verdict": "unavailable",
                         "detail": "database not ready: connection refused"})
    result = {"key": "crm_truth_exceptions", **apc.classify(2, stdout)}
    fix = apc.crm_data_to_fix([result])
    assert fix == {"deals_with_any_exception": None, "by_family": {},
                   "dataset_blockers": [], "windows": []}
    summary = {"verdict": "NOT CERTIFIED", "exit_code": 2,
               "audits": [{"key": "crm_truth_exceptions", "outcome": "unavailable"}],
               "crm_data_to_fix": fix}
    block = so.certification_block(
        _read(exit_code=2, verdict="NOT CERTIFIED", summary=summary), NOW)
    card = _fix_card(_render({"certification": block, "sources": []}))
    assert "Nothing to fix" not in card
    assert "the daily check could not count the deals that need fixing" in card
    assert "did not record which periods show their figures" in card


PROVEN_ZERO = {"deals_with_any_exception": 0,
               "by_family": {"revenue_proof": {"by_reason": {}, "deals": 0}},
               "dataset_blockers": [],
               "windows": [{"window": "business/ytd", "customers": "published",
                            "revenue_usd": "published"}]}


@pytest.mark.parametrize("change", [
    {"deals_with_any_exception": None},                       # not counted
    {"windows": []},                                          # periods unknown
    {"windows": [{"window": "business/ytd", "customers": "withheld",
                  "revenue_usd": "published"}]},
    {"dataset_blockers": [{"code": "canonical_ledger_stale"}]},
    {"by_family": {"revenue_proof": {"by_reason": {"no_amount": 1}}}},
    {"deals_with_any_exception": "0"},                        # not an integer
])
def test_30_nothing_to_fix_is_said_only_on_a_proven_zero(change):
    assert "Nothing to fix" in _fix_card(_render(_payload(crm_data_to_fix=PROVEN_ZERO)))
    card = _fix_card(_render(_payload(crm_data_to_fix={**PROVEN_ZERO, **change})))
    assert "Nothing to fix" not in card
    none = _fix_card(_render(_payload(crm_data_to_fix=None)))
    assert "Unknown" in none and "Nothing to fix" not in none


def _fix_text_owners() -> dict:
    src = (ROOT / "static/app.js").read_text()
    start = src.index("const SYSTEM_FIX_TEXT = {")
    end = src.index("};", start) + 2
    out = subprocess.run(
        ["node", "-e", src[start:end] + "\nprocess.stdout.write(JSON.stringify("
         "Object.fromEntries(Object.entries(SYSTEM_FIX_TEXT).map(([k, v]) => [k, v[0]]))));"],
        capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_31_every_reason_production_can_emit_has_plain_words_and_an_owner():
    from services import crm_truth_exceptions_service as cte
    where = _fix_text_owners()
    emitted = {r for reasons in cte.reason_vocabulary().values() for r in reasons}
    assert emitted == set(where), (emitted ^ set(where))
    assert set(where.values()) == {"hubspot", "platform"}
    # The sync's own state is never the sales team's job.
    for ours in ("association_unavailable", "latest_association_attempt_failed",
                 "deal_modified_after_association_observation",
                 "not_certified_by_latest_complete_scan",
                 "no_fx_rate_for_close_date", "home_currency_unverified",
                 "currency_reason_not_recorded"):
        assert where[ours] == "platform", ours


def test_32_our_own_sync_problems_are_not_listed_under_hubspot():
    fix = {**FIX, "by_family": {
        "customer_identity": {"by_reason": {"association_unavailable": 3,
                                            "unresolved_no_company": 1}},
        "revenue_proof": {"by_reason": {"a_reason_added_next_year": 2}}}}
    card = _fix_card(_render(_payload(crm_data_to_fix=fix)))
    hub = card[card.index("In HubSpot"):card.index("On our side")]
    ours = card[card.index("On our side"):card.index("Other blockers")]
    other = card[card.index("Other blockers"):]
    assert "<strong>1</strong> won deal is not linked to any company" in hub
    assert "not been read from HubSpot yet" not in hub
    assert re.search(r"<strong>3</strong> won deals(&#39;|&#039;|') companies "
                     r"have not been read", ours)
    assert "a reason added next year" in other
    assert "a reason added next year" not in hub


def test_33_a_stale_ledger_blocker_is_explained_and_counts_stay_unknown():
    """The workbench's real unavailable shape for an overdue deal sync."""
    from services import crm_truth_exceptions_service as cte
    report = cte._unavailable(NOW, cte.DATASET_LEDGER_STALE,
                              "last success 60h ago")
    fix = apc.crm_data_to_fix([{"key": "crm_truth_exceptions", "report": report}])
    assert fix["deals_with_any_exception"] is None and fix["windows"] == []
    card = _fix_card(_render(_payload(crm_data_to_fix=fix)))
    assert "A data problem on our side" in card
    assert "The deal sync has not succeeded within the staleness limit" in card
    assert "could not count the deals that need fixing" in card
    assert "Nothing to fix" not in card and "In HubSpot" not in card


def test_34_every_period_hidden_is_said_as_every_period():
    windows = [{"window": w, "customers": "withheld", "revenue_usd": "unavailable"}
               for w in ("business/ytd", "evidence/30d", "business/all_time")]
    card = _fix_card(_render(_payload(crm_data_to_fix={**FIX, "windows": windows})))
    assert "Customer counts are hidden for every period." in card
    assert "Revenue is hidden for every period." in card
    assert "Other periods show their figures" not in card


def test_35_an_old_check_dates_its_fix_list():
    block = so.certification_block(_read(hours_ago=72), NOW)
    assert block["state"] == "not_checked"
    card = _fix_card(_render({"certification": block, "sources": []}))
    assert "As of the last daily check, 3 days ago." in card


def test_36_a_window_with_an_unrecognised_status_counts_as_hidden():
    fix = {**PROVEN_ZERO, "windows": [
        {"window": "business/ytd", "customers": "unavailable", "revenue_usd": None},
        {"window": "evidence/30d", "customers": "published", "revenue_usd": "published"}]}
    card = _fix_card(_render(_payload(crm_data_to_fix=fix)))
    assert "Customer counts are hidden for: year to date." in card
    assert "Revenue is hidden for: year to date." in card
    assert "Nothing to fix" not in card


# ── the sidebar ───────────────────────────────────────────────────────────

# PR-ADS-162D retired Churn Input; five technical pages remain.
ADVANCED = {"scheduler", "gclid-attribution", "health", "revenue-health",
            "backfill"}


def test_37_system_is_routable_admin_only_and_uses_the_clean_chrome():
    html = (ROOT / "static/index.html").read_text()
    js = (ROOT / "static/app.js").read_text()
    pages = re.search(r"const PAGES = \[(.*?)\];", js, re.S).group(1)
    clean = re.search(r"const REVENUE_PAGES = \[(.*?)\];", js, re.S).group(1)
    assert '"system"' in pages and '"system"' in clean
    assert 'id="page-system"' in html and 'case "system":' in js
    assert 'data-page="system"' in html and 'id="nav-system-item"' in html
    assert 'systemNav.hidden = user.role !== "admin"' in js
    # navigate() refuses the page to a non-admin, in the same branch that
    # refuses an unknown page.
    assert "if (!PAGES.includes(page) || adminOnlyDenied(page)) {" in js
    denied = js[js.index("function adminOnlyDenied(page) {"):][:160]
    assert ('return page === "system" && '
            '!(_currentUser && _currentUser.role === "admin");') in denied


def test_38_the_technical_pages_sit_under_system_and_are_still_reachable():
    """PR-ADS-174A: the five technical pages were folded under a collapsed
    "Advanced tools" sidebar group (ADVANCED_ADMIN_PAGES, .nav-advanced, the
    toggle). They are now the System item's tab strip, after System itself;
    the fold is gone because the strip lists them without one."""
    html = (ROOT / "static/index.html").read_text()
    js = (ROOT / "static/app.js").read_text()
    css = (ROOT / "static/styles.css").read_text()
    system = html[html.index('data-section="system"'):]
    system = system[:system.index("</nav>")]
    tabs = re.findall(r'class="section-tab[^"]*" data-page="([a-z-]+)"', system)
    assert tabs[0] == "system" and set(tabs[1:]) == ADVANCED and len(tabs) == 6
    pages = re.search(r"const PAGES = \[(.*?)\];", js, re.S).group(1)
    for page in ADVANCED:
        assert f'"{page}"' in pages and f'id="page-{page}"' in html
    assert "ADVANCED_ADMIN_PAGES" not in js and "setAdvancedNavOpen" not in js
    assert "nav-advanced" not in html and "nav-advanced" not in css
    # an admin-hidden tab stays hidden: the tab style sets display, so [hidden]
    # needs its own rule
    assert re.search(r"\.section-tab\[hidden\] \{\s*display: none;", css)


def test_39_the_page_links_to_every_advanced_page():
    html = _render(_payload())
    linked = set(re.findall(r'data-system-goto="([a-z-]+)"', html))
    assert linked == ADVANCED


# ── the real queries on a live database ───────────────────────────────────

def test_40_the_real_reads_on_a_live_database(monkeypatch):
    from tests import test_pr_ads_153b_pg_integration as pgmod
    if not pgmod._have_postgres():
        pytest.skip("PostgreSQL not available")
    cluster = pgmod._PgCluster().start()
    import db.connection as connection
    try:
        monkeypatch.setenv("DATABASE_URL", cluster.url)
        monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", "3059734490")
        monkeypatch.setattr(connection, "_pool", None, raising=False)
        connection.init_pool()
        from db.schema import init_db
        init_db()

        # An empty database: nothing checked, nothing proven, no spend.
        empty = so.build_system_overview()
        assert empty["certification"]["state"] == "not_checked"
        by_key = {s["key"]: s for s in empty["sources"]}
        # PR-ADS-171A: the fourth source, Meta Ads, has no credentials in this
        # environment. That is `not_connected` — neither proven nor late — and
        # is asserted on its own; the three sources this case is about keep
        # the assertion they had.
        meta_line = by_key.pop("meta_ads_spend")
        assert meta_line["status"] == "not_connected"
        assert meta_line["complete_through"] is None
        assert {s["status"] for s in by_key.values()} <= {"not_proven", "late"}
        assert by_key["hubspot_deals"]["status"] == "not_proven"
        assert by_key["hubspot_deals"]["last_success_at"] is None
        assert by_key["google_ads_spend"]["status"] == "late"
        assert "system-source--ok" not in _render(empty)

        # Two recorded runs, written by the real job writer: yesterday's pass
        # and today's failure. The NEWEST decides, whatever order they return.
        now = datetime.now(tz=timezone.utc)
        ok_ticks = iter([now - timedelta(hours=24, minutes=12),
                         now - timedelta(hours=24, minutes=2)])
        assert pcs.run_and_record(runner=lambda: SUMMARY,
                                  now=lambda: next(ok_ticks))["recorded"]
        assert so.build_system_overview()["certification"]["state"] == "certified"
        bad = {**SUMMARY, "verdict": "VIOLATION", "exit_code": 1,
               "audits": [{"key": "closed_won_truth", "outcome": "violation"}]}
        bad_ticks = iter([now - timedelta(minutes=12), now - timedelta(minutes=2)])
        assert pcs.run_and_record(runner=lambda: bad,
                                  now=lambda: next(bad_ticks))["recorded"]
        cert = so.build_system_overview()["certification"]
        assert cert["state"] == "violation" and cert["verdict"] == "VIOLATION"
        assert 0 <= cert["age_hours"] < 1
        assert cert["crm_data_to_fix"]["deals_with_any_exception"] == 25

        # The spend coverage ledger: yesterday's daily chunk, plus an OLD month
        # refilled a moment ago — which must not become "the newest fetch".
        from analysis.account_time import account_today
        yesterday = account_today(now) - timedelta(days=1)
        with cluster.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO google_ads_spend_coverage (customer_id, chunk_start, "
                "chunk_end, status, rows_written, cost_micros_total, updated_at) "
                "VALUES (%s, %s, %s, 'verified', 54, 1000, %s), "
                "       (%s, '2025-05-01', '2025-05-31', 'verified', 9, 1000, %s), "
                "       ('another-account', %s, %s, 'verified', 1, 1, now())",
                ("3059734490", yesterday - timedelta(days=6), yesterday,
                 now - timedelta(hours=20),
                 "3059734490", now - timedelta(minutes=5),
                 yesterday, yesterday + timedelta(days=30)))
            conn.commit()
        spend = {s["key"]: s for s in
                 so.build_system_overview()["sources"]}["google_ads_spend"]
        assert spend["status"] == "current"
        assert spend["complete_through"] == yesterday.isoformat()
        assert 19.5 < spend["age_hours"] < 20.5          # not the refill's 5 min
    finally:
        if getattr(connection, "_pool", None) is not None:
            connection._pool.closeall()
            connection._pool = None
        cluster.stop()
