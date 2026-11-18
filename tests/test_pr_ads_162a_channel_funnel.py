"""PR-ADS-162A — the Channels page: one acquisition cohort, every channel.

Contacts created in the window, by HubSpot original source, and how many have
reached each funnel stage. It shares Campaign Evidence's cohort rule, SQL proof
and publication verdict; it publishes no deals, revenue, customers or spend,
and when the cohort is not published it carries no counts at all.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services import channel_funnel_service as cf
from services import marketing_outcome_cohort_service as cohort_service

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)


def _row(cid, source="PAID_SEARCH", detail=None, stage="lead", **dates):
    row = {"contact_id": str(cid), "funnel_row_id": int(cid),
           "created_at": "2026-10-01T00:00:00+00:00", "lifecycle_stage": stage,
           "hs_analytics_source": source, "hs_analytics_source_data_1": detail}
    for event in ("lead", "mql", "sql", "opportunity", "customer"):
        row[f"{event}_entered_direct"] = dates.get(f"{event}_direct")
        row[f"{event}_entered_recovered"] = dates.get(f"{event}_recovered")
    return row


ROWS = [
    _row(1, "PAID_SOCIAL", "LinkedIn", "customer"),
    _row(2, "PAID_SOCIAL", "facebook", "lead", sql_direct="2026-10-05"),
    _row(3, "PAID_SOCIAL", "facebook", "marketingqualifiedlead"),
    _row(4, "PAID_SEARCH", "Brand - US", "opportunity"),
    _row(5, "AI_REFERRALS", "chatgpt.com", "lead", mql_recovered="2026-10-09"),
    _row(6, None, None, "other", opportunity_direct="2026-10-11"),
    _row(7, "ORGANIC_SEARCH", None, "370543605"),          # discarded contact
]


def _fresh(hours=3.0):
    return {"fresh": True, "reason": "source_fresh", "age_hours": hours,
            "last_successful_incremental_at":
                (NOW - timedelta(hours=hours)).isoformat()}


def _payload(monkeypatch, rows=ROWS, *, freshness=None, available=True):
    from db import crm_funnel_repository as repo
    monkeypatch.setattr(repo, "fetch_channel_funnel_contacts",
                        lambda start, end: ({"available": True, "rows": rows,
                                             "missing_created_at": 2}
                                            if available else
                                            {"available": False, "rows": [],
                                             "missing_created_at": None}))
    monkeypatch.setattr(cohort_service, "read_freshness",
                        lambda now=None: freshness or _fresh())
    return cf.build_channel_funnel("180d", now=NOW)


def test_01_a_stage_is_reached_on_a_timestamp_or_a_stage_at_or_beyond_it():
    assert cf.reached(_row(1, stage="customer"), "mql") is True
    assert cf.reached(_row(1, stage="customer"), "opportunity") is True
    assert cf.reached(_row(1, stage="marketingqualifiedlead"), "sql") is False
    assert cf.reached(_row(1, stage="lead", mql_recovered="2026-10-09"), "mql") is True
    assert cf.reached(_row(1, stage="other", opportunity_direct="2026-10-11"),
                      "opportunity") is True
    # No inference downward: opportunity evidence is not SQL evidence.
    assert cf.reached(_row(1, stage="other", opportunity_direct="2026-10-11"),
                      "sql") is False
    # A custom stage with no rank proves nothing.
    assert cf.reached(_row(1, stage="370543605"), "mql") is False


def test_02_the_sql_rule_is_the_cohort_services_own_proof():
    for row in ROWS + [_row(9, stage="salesqualifiedlead"),
                       _row(10, stage="lead", sql_recovered="2026-10-02")]:
        assert cf.reached(row, "sql") is (cohort_service.sql_proof(row) is not None)


def test_03_contacts_are_grouped_by_hubspot_original_source_unmodified():
    funnel = cf.build_funnel(ROWS)
    by = {c["source"]: c for c in funnel["channels"]}
    assert set(by) == {"PAID_SOCIAL", "PAID_SEARCH", "AI_REFERRALS",
                       "ORGANIC_SEARCH", cf.NO_SOURCE}
    assert by["PAID_SOCIAL"]["label"] == "Paid Social"
    assert by["AI_REFERRALS"]["label"] == "AI Referrals"
    assert by[cf.NO_SOURCE]["label"] == "No source recorded"
    # A source HubSpot adds later is shown under its own name.
    assert cf.build_funnel([_row(1, "NEW_SOURCE")])["channels"][0]["label"] == \
        "NEW_SOURCE"


def test_04_stage_counts_per_channel_and_in_total():
    funnel = cf.build_funnel(ROWS)
    assert funnel["totals"] == {"contacts": 7, "reached_mql": 4,
                                "reached_sql": 3, "reached_opportunity": 3,
                                "reached_customer_stage": 1}
    social = next(c for c in funnel["channels"] if c["source"] == "PAID_SOCIAL")
    assert (social["contacts"], social["reached_mql"], social["reached_sql"],
            social["reached_opportunity"], social["reached_customer_stage"]) == \
        (3, 2, 2, 1, 1)
    assert [(d["value"], d["contacts"], d["reached_sql"])
            for d in social["drilldowns"]] == [("facebook", 2, 1),
                                               ("linkedin", 1, 1)]
    assert cf.reconcile(funnel) == []


def test_05_long_drilldown_lists_are_folded_and_still_sum():
    rows = [_row(i, "PAID_SEARCH", f"campaign {i % 12}", "salesqualifiedlead")
            for i in range(1, 61)]
    ch = cf.build_funnel(rows)["channels"][0]
    assert len(ch["drilldowns"]) == cf.MAX_DRILLDOWNS + 1
    assert ch["drilldowns"][-1]["value"] == "(other: 4 values)"
    assert sum(d["contacts"] for d in ch["drilldowns"]) == 60
    assert cf.reconcile(cf.build_funnel(rows)) == []


def test_06_a_duplicate_contact_row_is_counted_once_and_blocks_publication(
        monkeypatch):
    funnel = cf.build_funnel(ROWS + [ROWS[0]])
    assert funnel["totals"]["contacts"] == 7
    assert cf.reconcile(funnel) == ["1 duplicate contact rows"]
    payload = _payload(monkeypatch, ROWS + [ROWS[0]])
    assert payload["publication"] == {"status": "withheld",
                                      "reason": "cohort_reconciliation_failed"}
    assert payload["channels"] is None and payload["totals"] is None


def test_07_published_payload_carries_the_cohort_metadata(monkeypatch):
    p = _payload(monkeypatch)
    assert p["publication"] == {"status": "published", "reason": None}
    assert p["metric_family"] == "acquisition_cohort_outcomes"
    assert p["window_basis"] == "contact_created_at"
    assert p["window"]["key"] == "180d" and p["window"]["end_date"] == "2026-11-18"
    assert p["totals"]["contacts"] == 7
    assert [s["key"] for s in p["stages"]] == list(cf.STAGE_KEYS)
    assert p["freshness"]["as_of"] and p["contacts_without_created_at"] == 2


@pytest.mark.parametrize("freshness,reason", [
    ({"fresh": False, "reason": "source_bootstrap_incomplete"},
     "source_bootstrap_incomplete"),
    ({"fresh": False, "reason": "source_last_incremental_failed"},
     "source_last_incremental_failed"),
    ({"fresh": None, "reason": "source_freshness_unreadable"},
     "data_watermark_unknown"),
])
def test_08_withheld_means_absent_no_count_anywhere(monkeypatch, freshness,
                                                    reason):
    p = _payload(monkeypatch, freshness=freshness)
    assert p["publication"] == {"status": "withheld", "reason": reason}
    assert p["totals"] is None and p["channels"] is None
    assert p["contacts_without_created_at"] is None

    def numbers(value, path=""):
        if isinstance(value, dict):
            for k, v in value.items():
                yield from numbers(v, f"{path}.{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                yield from numbers(v, f"{path}[{i}]")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            yield path, value
    assert [n for n in numbers(p) if "freshness" not in n[0]] == []


def test_09_an_unreadable_funnel_is_unavailable_not_empty(monkeypatch):
    p = _payload(monkeypatch, available=False)
    assert p["publication"] == {"status": "unavailable",
                                "reason": "canonical_funnel_unreadable"}
    assert p["channels"] is None


def test_10_a_stale_but_known_watermark_still_publishes_with_its_as_of(
        monkeypatch):
    stale = dict(_fresh(60.0), fresh=False, reason="source_stale")
    p = _payload(monkeypatch, freshness=stale)
    assert p["publication"]["status"] == "published"
    assert p["freshness"]["reason"] == "source_stale"


def test_11_an_unknown_window_is_rejected():
    from analysis.evidence_windows import EvidenceWindowError
    with pytest.raises(EvidenceWindowError):
        cf.build_channel_funnel("13d", now=NOW)


def test_12_the_module_publishes_no_deals_revenue_customers_or_spend():
    src = (ROOT / "services/channel_funnel_service.py").read_text()
    code = src.split('"""', 2)[2]
    for forbidden in ("load_won_deals", "reader_row", "revenue_usd",
                      "deal_ledger", "spend", "cpql", "roas", "cac",
                      "get_client", "hubspot_pull"):
        assert forbidden not in code.lower().replace("no spend", ""), forbidden
    assert '"customers"' not in src


def _render(payload_js: str) -> str:
    """Evaluate the real renderChannelFunnel from static/app.js in node."""
    src = (ROOT / "static/app.js").read_text()
    start = src.index("const CHANNEL_FUNNEL_REASON_LABELS = {")
    end = src.index("async function loadChannels()")
    esc_start = src.index("function escapeHtml(str) {")
    esc_end = src.index("\n}\n", esc_start) + 3
    script = (src[esc_start:esc_end] + src[start:end]
              + f"\nprocess.stdout.write(renderChannelFunnel({payload_js}));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_13_the_page_renders_channels_stages_and_shares():
    html = _render("""{publication:{status:"published"},
      window:{start_date:"2026-05-23",end_date:"2026-11-18"},
      freshness:{as_of:"2026-11-18T06:00:00+00:00"},
      stages:[{key:"reached_mql",label:"MQL"},{key:"reached_sql",label:"SQL"}],
      totals:{contacts:10,reached_mql:6,reached_sql:2},
      channels:[{source:"PAID_SOCIAL",label:"Paid Social",contacts:4,
        reached_mql:3,reached_sql:1,drilldowns:[
          {value:"linkedin",contacts:3,reached_mql:2,reached_sql:1},
          {value:"facebook",contacts:1,reached_mql:1,reached_sql:0}]}],
      notes:{}}""")
    assert "All channels" in html and "Paid Social" in html
    assert "linkedin" in html and "facebook" in html
    assert "(25.0%)" in html and "(60.0%)" in html
    assert "measured as of 2026-11-18 06:00 UTC" in html


@pytest.mark.parametrize("status,word", [("withheld", "Withheld"),
                                         ("unavailable", "Unavailable")])
def test_14_a_withheld_page_shows_the_word_and_reason_never_a_number(status,
                                                                     word):
    html = _render(f"""{{publication:{{status:"{status}",
      reason:"source_bootstrap_incomplete"}}, window:{{}}, freshness:{{}},
      stages:[{{key:"reached_sql",label:"SQL"}}],
      totals:{{contacts:999,reached_sql:77}}, channels:null, notes:{{}}}}""")
    assert f"<strong>{word}</strong>" in html
    assert "still being copied from HubSpot" in html
    assert "999" not in html and "77" not in html and "<table" not in html


def test_15_the_page_is_wired_into_the_sidebar_router_and_api():
    html = (ROOT / "static/index.html").read_text()
    js = (ROOT / "static/app.js").read_text()
    server = (ROOT / "api/server.py").read_text()
    assert 'data-page="channels"' in html and 'id="page-channels"' in html
    assert 'case "channels":' in js and "/api/channel-funnel?window=" in js
    assert '@app.get("/api/channel-funnel")' in server


def test_16_the_real_query_and_counts_on_a_live_database(monkeypatch):
    from tests import test_pr_ads_153b_pg_integration as pgmod
    if not pgmod._have_postgres():
        pytest.skip("PostgreSQL not available")
    cluster = pgmod._PgCluster().start()
    import db.connection as connection
    try:
        monkeypatch.setenv("DATABASE_URL", cluster.url)
        monkeypatch.setattr(connection, "_pool", None, raising=False)
        connection.init_pool()
        from db.schema import init_db
        init_db()
        c = pgmod._contact
        pgmod._write([
            c("1", lifecyclestage="customer", created="2026-10-01T00:00:00Z",
              source="PAID_SOCIAL", campaign="LinkedIn"),
            c("2", lifecyclestage="lead", created="2026-10-02T00:00:00Z",
              source="PAID_SOCIAL", campaign="facebook",
              entered_sql="2026-10-05T00:00:00Z"),
            c("3", lifecyclestage="marketingqualifiedlead",
              created="2026-10-03T00:00:00Z", source="AI_REFERRALS",
              campaign="chatgpt.com"),
            c("4", lifecyclestage="opportunity", created="2025-01-03T00:00:00Z",
              source="PAID_SEARCH"),                      # outside the window
        ])
        monkeypatch.setattr(cohort_service, "read_freshness",
                            lambda now=None: _fresh())
        p = cf.build_channel_funnel("180d", now=NOW)
        assert p["publication"]["status"] == "published"
        assert p["totals"] == {"contacts": 3, "reached_mql": 2,
                               "reached_sql": 2, "reached_opportunity": 1,
                               "reached_customer_stage": 1}
        assert [c_["source"] for c_ in p["channels"]] == ["PAID_SOCIAL",
                                                          "AI_REFERRALS"]
        everything = cf.build_channel_funnel("all_time", now=NOW)
        assert everything["totals"]["contacts"] == 4
    finally:
        if getattr(connection, "_pool", None) is not None:
            connection._pool.closeall()
            connection._pool = None
        cluster.stop()
