"""PR-ADS-181A — the Channels page redesign, the Dashboard → Channels tab
retired, and the System page tidy-up (``docs/66_CHANNELS_REDESIGN.md``).

The redesign is presentation only: the same /api/channel-funnel response, the
same numbers and the same publication verdicts. These cases hold what the new
layout must not change:

* each KPI group follows its own verdict, and withheld means absent;
* customers are distinct companies — the "All channels" figure is the window's
  own count, never a sum of the rows;
* "Most from" names a channel only when the published rows make it certain;
* the drawer's funnel is the shared block over the shared model (one set of
  stage rules, PR-ADS-180A/B);
* the period ratios carry their disclosure, and nothing is named ROAS / CAC;
* Meta spend is set against nothing;
* the retired tab and route stay retired;
* a run type is shown in plain words, and an unknown one as itself.

There is no JavaScript test harness in this repository: the UI cases evaluate
the real ``static/app.js`` in ``node`` (the 161B vm harness), or extract the
functions they need, as ``test_106`` in test_pr_ads_160 does.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")
SERVER = (ROOT / "api" / "server.py").read_text(encoding="utf-8")


def _run(script):
    from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app
    return _run_app(script)


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _fn(name: str, js: str = JS) -> str:
    m = re.search(r"\n(?:async )?function %s\(" % re.escape(name), js)
    assert m, f"function {name} not found"
    return js[m.start() + 1:js.index("\n}\n", m.start()) + 2]


def _m(value, status="published", reason=None, **extra):
    return {"value": value if status == "published" else None, "status": status,
            "reason": None if status == "published" else reason, **extra}


def _orow(source, label, deals, customers, revenue, status="published", reason=None):
    return {"source": source, "label": label,
            "metrics": {"closed_won_deals": _m(deals, status, reason),
                        "customers": _m(customers, status, reason),
                        "revenue_usd": _m(revenue, status, reason)}}


def _funnel_row(source, label, contacts, sql, drill=None):
    return {"source": source, "label": label, "contacts": contacts,
            "reached_mql": contacts // 2, "reached_sql": sql,
            "reached_opportunity": sql // 2, "reached_customer_stage": 0,
            "drilldowns": drill or [{"value": "(none)", "contacts": contacts}]}


def _payload(**over):
    """A production-shaped response: two channels, the no-source funnel row
    and the deals with no single source. One company closed deals through both
    Paid Search and Direct Traffic, so the rows' customers (3 + 2 + 1 = 6) are
    MORE than the window's distinct companies (5)."""
    channels = [
        _funnel_row("PAID_SEARCH", "Paid Search", 400, 20,
                    [{"value": "brand - uk", "contacts": 300}, {"value": "search - uae", "contacts": 100}]),
        _funnel_row("DIRECT_TRAFFIC", "Direct Traffic", 300, 9),
        _funnel_row("(no source recorded)", "No source recorded", 10, 0),
    ]
    data = {
        "metric_family": "acquisition_cohort_outcomes", "window_basis": "contact_created_at",
        "publication": {"status": "published", "reason": None},
        "freshness": {"as_of": "2026-11-26T06:06:00+00:00"},
        "window": {"key": "180d", "start_date": "2026-05-31", "end_date": "2026-11-26",
                   "is_all_time": False},
        "stages": [{"key": "reached_mql", "label": "MQL"}, {"key": "reached_sql", "label": "SQL"},
                   {"key": "reached_opportunity", "label": "Opportunity"},
                   {"key": "reached_customer_stage", "label": "Customer stage"}],
        "totals": {"contacts": 710, "reached_mql": 350, "reached_sql": 29,
                   "reached_opportunity": 14, "reached_customer_stage": 0},
        "channels": channels, "contacts_without_created_at": 0,
        "notes": {"channels": "Channel is HubSpot's original source."},
        "outcomes": {
            "metric_family": "closed_won_outcomes", "window_basis": "deal_close_date",
            "window": {"window_type": "evidence", "window_key": "180d",
                       "start_date": "2026-05-31", "end_date": "2026-11-25",
                       "disclosure": "Through 25 Nov 2026 — today is not included until tomorrow morning's sync."},
            "publication": {"closed_won_deals": _m(7), "revenue_usd": _m(90000.0),
                            "customers": _m(5)},
            "totals": {"metrics": {"closed_won_deals": _m(7), "revenue_usd": _m(90000.0),
                                   "customers": _m(5, dedup_key="hubspot_company_id",
                                                   additive=False)}},
            "channels": [_orow("PAID_SEARCH", "Paid Search", 3, 3, 60000.0),
                         _orow("DIRECT_TRAFFIC", "Direct Traffic", 2, 2, 20000.0),
                         _orow("__deal_source_unresolved__", "Deals with no single source",
                               1, 1, 10000.0)],
            "google_ads_line": {
                "label": "Google Ads (canonical evidence)",
                "spend": {"value": 4000.0, "status": "published", "reason": None},
                "metrics": {
                    "closed_won_deals": _m(2), "customers": _m(2), "revenue_usd": _m(30000.0),
                    "period_revenue_per_ad_spend": _m(
                        7.5, ratio_kind="non_cohort_operational_ratio",
                        cohort_basis="close_date_outcomes_over_same_window_spend"),
                    "period_spend_per_customer_won": _m(
                        2000.0, ratio_kind="non_cohort_operational_ratio",
                        cohort_basis="close_date_outcomes_over_same_window_spend"),
                    "roas": _m(None, "unavailable", "roas_requires_revenue_and_spend_in_one_acquisition_cohort"),
                    "cac": _m(None, "unavailable", "cac_requires_spend_and_customers_in_one_acquisition_cohort")},
                "evidence_split": {"with_gclid": {"closed_won_deals": _m(1)},
                                   "source_classification_only": {"closed_won_deals": _m(1)}}},
            "meta_ads_line": {"label": "Meta Ads (spend)", "spend": _m(10360.8, currency="USD"),
                              "native_spend": _m(10360.8, currency="USD"),
                              "comparison": _m(None, "unavailable",
                                               "channel_population_is_not_meta_spend_population"),
                              "accounts": [], "owner_declared_retired": [],
                              "day_basis_disclosure": "Each ad account is read on its own calendar days."},
            "notes": {"spend": "Google Ads spend is compared only with the Google Ads line."},
        },
    }
    data.update(over)
    return data


def _render(payload, view=None):
    return _run("renderChannelFunnel(%s, %s)" % (json.dumps(payload), json.dumps(view or {})))


def _drawer(payload, key):
    return _run("channelDrawerHtml(%s, %s)" % (json.dumps(payload), json.dumps(key)))


def _names(html):
    table = html[html.index("<tbody>"):html.index("</tbody>")]
    return re.findall(r'<span class="channels-name">([^<]+)</span>', table)


def _tr(html, label):
    table = html[html.index("<tbody>"):html.index("</tbody>")]
    for tr in re.findall(r"<tr[^>]*>.*?</tr>", table, re.S):
        if f'<span class="channels-name">{label}<' in tr or f"<strong>{label}<" in tr:
            return _text(tr)
    raise AssertionError(label)


def _group(html, label):
    i = html.rindex("<section", 0, html.index(f'aria-label="{label}"'))
    return html[i:html.index("</section>", i)]


# ── 1. the ranked table ──────────────────────────────────────────────────────

def test_01_the_table_is_ranked_by_customers_won_by_default():
    out = _payload()
    out["outcomes"]["channels"][0]["metrics"]["customers"] = _m(1)       # Paid Search 1
    html = _render(out)
    assert _names(html) == ["Direct Traffic", "Paid Search", "No source recorded",
                            "Deals with no single source"]
    assert "Channels ranked by customers won" in html
    assert 'data-channel-sort="customers" title' in html and "is-active is-desc" in html
    head = html[html.index("<thead>"):html.index("</thead>")]
    assert re.findall(r'data-channel-sort="([a-z_]+)"', head) == [
        "channel", "customers", "revenue", "share", "contacts", "sqls", "sql_rate"]
    # Seven columns, no paid-media column on a channel row.
    for gone in ("Spend", "Period Revenue", "Spend per Customer"):
        assert gone not in head, gone


def test_02_every_column_sorts_and_the_rows_that_are_not_channels_stay_last():
    for key, first in (("revenue", "Paid Search"), ("share", "Paid Search"),
                       ("contacts", "Paid Search"), ("sqls", "Paid Search"),
                       ("sql_rate", "Paid Search"), ("channel", "Direct Traffic")):
        names = _names(_render(_payload(), {"sort": key}))
        assert names[0] == first, key
        assert names[-2:] == ["No source recorded", "Deals with no single source"], key
    asc = _names(_render(_payload(), {"sort": "contacts", "dir": "asc"}))
    assert asc[:2] == ["Direct Traffic", "Paid Search"]


def test_03_the_all_channels_row_is_the_windows_own_customers_never_a_sum():
    html = _render(_payload())
    # 3 + 2 + 1 = 6 company rows; the window closed deals with 5 companies.
    assert _tr(html, "All channels").startswith("All channels 5 $90.0k")
    assert _tr(html, "Paid Search").startswith("Paid Search 3 $60.0k")
    assert "the rows don't add up to 5" in _text(html)
    assert "Customers won 5 distinct companies" in _text(_group(html, "Deals closed"))
    # The window's figure is read, not derived: a withheld window total stays
    # withheld even though every row is published.
    held = _payload()
    held["outcomes"]["totals"]["metrics"]["customers"] = _m(
        None, "withheld", "customer_identity_unresolved")
    row = _tr(_render(held), "All channels")
    assert row.startswith("All channels Withheld a won deal has no company, or several")
    assert " 6 " not in f" {row} "


def test_04_the_unresolved_and_no_source_rows_read_n_a_where_they_hold_nothing():
    html = _render(_payload())
    assert _tr(html, "Deals with no single source") == "Deals with no single source 1 $10.0k 11.1% n/a n/a"
    assert _tr(html, "No source recorded") == "No source recorded n/a n/a 10 0 0.0%"
    assert "Source not resolved" not in html
    # A zero-customer row is dimmed; so are the two rows that are not channels.
    assert re.search(r'class="channels-row channels-row--dim channels-row--unresolved"', html)


def test_05_the_phone_layout_keeps_channel_customers_revenue_and_sqls():
    wide = set(re.findall(r'channels-col--([a-z_]+) channels-col--wide', _render(_payload())))
    assert wide == {"share", "contacts", "sql_rate"}
    phone = CSS[CSS.index("@media (max-width: 600px) {\n  .channels-table"):]
    assert ".channels-table .channels-col--wide { display: none; }" in phone[:400]
    assert "overflow-x" not in _fn("channelTableHtml")


# ── 2. each group follows its own verdict ────────────────────────────────────

def test_06_a_withheld_funnel_group_shows_no_number_while_outcomes_publish():
    p = _payload(publication={"status": "withheld", "reason": "source_last_incremental_failed"})
    html = _render(p)
    funnel = _text(_group(html, "Contacts acquired"))
    assert funnel.startswith("Contacts acquired 31 May – 26 Nov Withheld — the last contact sync failed.")
    assert "New contacts Withheld" in funnel and "Reached SQL so far Withheld" in funnel
    for leak in ("710", "29", "400", "Most from", "%"):
        assert leak not in funnel, leak
    # Absent from the table too: no funnel column, the word and the reason.
    head = html[html.index("<thead>"):html.index("</thead>")]
    assert "New contacts" not in head and "SQL" not in head
    assert "New contacts, SQLs and SQL rate are withheld — the last contact sync failed." in _text(html)
    assert "710" not in html and ">29<" not in html
    # The outcome group publishes on its own verdict.
    deals = _text(_group(html, "Deals closed"))
    assert "Customers won 5 distinct companies" in deals
    # Paid Search 3 is not certainly ahead of Direct 2 + 1 unresolved: no
    # customer leader; its revenue lead ($60.0k > $20.0k + $10.0k) is certain.
    assert "Most from Paid Search · $60.0k" in deals and "· 3" not in deals


def test_07_unavailable_outcomes_show_the_word_while_the_funnel_publishes():
    p = _payload()
    reason = "canonical_ledger_stale"
    p["outcomes"] = {**p["outcomes"], "channels": None, "totals": None, "google_ads_line": None,
                     "publication": {k: _m(None, "unavailable", reason)
                                     for k in ("closed_won_deals", "revenue_usd", "customers")}}
    html = _render(p)
    deals = _text(_group(html, "Deals closed"))
    assert "Customers won Unavailable" in deals and "Closed-won revenue Unavailable" in deals
    assert "the deal sync is overdue" in deals
    for leak in ("$90", "$60", "Most from", "—"):
        assert leak not in deals, leak
    head = html[html.index("<thead>"):html.index("</thead>")]
    assert "Customers" not in head and "Revenue" not in head
    assert "New contacts 710" in _text(_group(html, "Contacts acquired"))
    assert _tr(html, "All channels") == "All channels 710 29 4.1%"
    # Never a dash for an outcome: the word, with its reason.
    assert "Customers and revenue are unavailable — the deal sync is overdue" in _text(html)


def test_08_both_groups_keep_their_metric_family_and_window_basis():
    html = _render(_payload())
    assert ('aria-label="Deals closed" data-metric-family="closed_won_outcomes" '
            'data-window-basis="deal_close_date"') in html
    assert ('aria-label="Contacts acquired" data-metric-family="acquisition_cohort_outcomes" '
            'data-window-basis="contact_created_at"') in html


# ── 3. "Most from" ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("candidates,uncertain,expected", [
    ([{"label": "A", "value": 5}, {"label": "B", "value": 3}], 0, {"label": "A", "value": 5}),
    ([{"label": "A", "value": 5}, {"label": "B", "value": 5}], 0, None),          # a tie
    ([{"label": "A", "value": 5}, {"label": "B", "value": 3}], 2, None),          # 3 + 2 could tie
    ([{"label": "A", "value": 5}, {"label": "B", "value": None}], 0, None),       # unpublished row
    ([{"label": "A", "value": 5}, {"label": "B", "value": 3}], None, None),       # unknown remainder
    ([{"label": "A", "value": 0}, {"label": "B", "value": 0}], 0, None),          # nothing to lead
    ([], 0, None),
])
def test_09_most_from_is_said_only_when_the_published_rows_make_it_certain(
        candidates, uncertain, expected):
    assert _run("channelLeader(%s, %s)" % (json.dumps(candidates), json.dumps(uncertain))) \
        == expected


def test_10_most_from_is_omitted_on_a_tie_or_an_unpublished_row_on_the_page():
    tie = _payload()
    tie["outcomes"]["channels"][1]["metrics"]["customers"] = _m(3)
    deals = _text(_group(_render(tie), "Deals closed"))
    assert "Customers won 5 distinct companies Closed-won revenue" in deals   # no leader
    assert "Most from Paid Search · $60.0k" in deals                         # revenue still clear
    held = _payload()
    held["outcomes"]["channels"][1]["metrics"]["customers"] = _m(1)
    held["outcomes"]["channels"][1]["metrics"]["revenue_usd"] = _m(
        None, "withheld", "closed_won_deals_missing_proven_usd_amount")
    deals = _text(_group(_render(held), "Deals closed"))
    assert "Most from Paid Search · 3" in deals and "· $60.0k" not in deals
    # The unresolved row and the no-source row never lead, and they count
    # against the leader's margin.
    close = _payload()
    close["outcomes"]["channels"][2]["metrics"]["customers"] = _m(2)       # 3 > 2 + 2 fails
    assert "Most from Paid Search · 3" not in _text(_group(_render(close), "Deals closed"))


# ── 4. the drawer reuses the shared funnel rules ─────────────────────────────

def test_11_the_drawer_is_the_shared_block_over_the_shared_model():
    p = _payload()
    drawer, expected = _run(
        "const p = %s; [channelDrawerHtml(p, 'PAID_SEARCH'),"
        " funnelBlockHtml(channelFunnelModel({...p, totals: p.channels[0]}),"
        "  {ariaLabel: 'Paid Search funnel', layout: 'vertical'})]" % json.dumps(p))
    assert expected in drawer                       # byte for byte, the block's own markup
    assert drawer.count('class="funnel-block"') == 1
    assert re.findall(r'data-funnel-slot="([a-z]+)"', drawer) == [
        "contacts", "mql", "sql", "opportunity", "customer"]
    assert "Reached SQL 20 5.0% of contacts" in _text(drawer)
    # No forked stage rule: the drawer writes no stage label or share itself.
    body = _fn("channelDrawerHtml")
    assert "funnelBlockHtml(channelFunnelModel(" in body
    for forked in ("Reached MQL", "Reached SQL", "% of contacts", "toFixed", "channelFunnelRate"):
        assert forked not in body, forked


def test_12_the_drawer_keeps_the_bound_and_withheld_rules():
    bound = _text(_drawer(_payload(contacts_without_created_at=3), "PAID_SEARCH"))
    assert "Contacts acquired at least 400" in bound and "share unknown" in bound
    held = _drawer(_payload(publication={"status": "withheld",
                                         "reason": "source_bootstrap_incomplete"}), "PAID_SEARCH")
    assert "<strong>Withheld</strong>" in held
    for leak in (">400<", "brand - uk", "300"):
        assert leak not in held, leak
    # Its outcome figures follow THEIR verdict, not the funnel's.
    assert "Closed-won deals 3 Customers won 3 Revenue $60.0k" in _text(held)


def test_13_the_drawer_lists_sources_and_links_and_closes_three_ways():
    d = _text(_drawer(_payload(), "PAID_SEARCH"))
    assert "Top sources (HubSpot detail) brand - uk 300 search - uae 100" in d
    assert "Open in Campaigns →" in d and "Open in Pipeline →" in d
    assert "No detail recorded" not in d
    # A contact with no detail recorded is named so, never dropped (and a
    # channel whose ONLY detail is none has no list to show).
    p = _payload()
    p["channels"][0]["drilldowns"].append({"value": "(none)", "contacts": 7})
    assert "No detail recorded 7" in _text(_drawer(p, "PAID_SEARCH"))
    assert "Top sources" not in _drawer(_payload(), "DIRECT_TRAFFIC")
    wiring = JS[JS.index("// Wire up Channels controls"):JS.index("// Force-close help drawer on startup")]
    assert 'e.key === "Escape" && _channelsView.drawer' in wiring
    assert "[data-channels-drawer-close]" in wiring
    root = _fn("renderChannelDrawer")
    assert '<div class="channels-drawer-overlay" data-channels-drawer-close></div>' in root
    assert 'class="channels-drawer__close" data-channels-drawer-close' in _fn("channelDrawerHtml")
    assert "width: 100vw" in CSS[CSS.index("@media (max-width: 600px) {\n  .channels-table"):]


# ── 5. the paid channels block ───────────────────────────────────────────────

def test_14_the_google_ads_card_carries_the_disclosure_beside_every_period_value():
    html = _render(_payload())
    i = html.index("channels-paid__card--google")
    card = html[i:html.index("channels-paid__card--meta")]
    text = _text(card)
    assert "Period Revenue / Ad Spend 7.50x Non-cohort operational ratio" in text
    assert "Spend per Customer Won This Period $2.0k Non-cohort operational ratio" in text
    assert card.count('title="Non-cohort operational ratio · Closed-won outcomes may originate '
                      'from earlier advertising periods"') == 4
    assert "2 customers from 2 deals with a Google click ID or HubSpot Paid Search. " \
           "Deals closed in these days may come from earlier ads." in text
    assert not re.search(r"\b(ROAS|CAC)\b", text)
    # A ratio that is not published is the word and the reason, never a value.
    p = _payload()
    p["outcomes"]["google_ads_line"]["metrics"]["period_revenue_per_ad_spend"] = _m(
        None, "withheld", "customer_identity_unresolved")
    t = _text(_render(p))
    assert "Period Revenue / Ad Spend Withheld" in t and "7.50x" not in t
    # Check 18 holds on this tree.
    from scripts.audit_customer_revenue_reader_cutover import ui_ratio_label_violations
    assert ui_ratio_label_violations(JS, HTML) == []


def test_15_meta_shows_spend_and_no_ratio():
    html = _render(_payload())
    card = html[html.index("channels-paid__card--meta"):]
    card = card[:card.index("</section>")]
    figures = re.findall(r'channels-paid__label">([^<]+)<', card)
    assert figures == ["Spend"]
    text = _text(card)
    assert "Spend $10.4k" in text and "10,360.80" not in text      # same currency: once
    assert "Not yet matched to HubSpot deals, so there is no revenue ratio." in text
    assert not re.search(r"\d\.\d\dx|per (?:lead|contact|sql|deal|customer)|%", text, re.I)
    # The card reads the figure through the one gate.
    assert "metaLineSpendMetric" in _fn("channelMetaSpendCellHtml")


# ── 6. the Dashboard → Channels tab and its route are gone ──────────────────

def test_16_the_dashboard_channels_tab_loader_and_renderer_are_deleted():
    assert 'data-dash-tab="channels"' not in HTML
    assert "dashboard-channels-root" not in HTML + JS
    assert '"channels"' not in re.search(r"const DASHBOARD_TABS = \[(.*?)\];", JS).group(1)
    for gone in ("loadDashboardChannels", "renderDashboardChannels", "renderChanKpiRow",
                 "renderChanMetaPanel", "CHAN_METRICS", "dashChannels", "/api/dashboard/channels",
                 "channelOutcomeStripHtml", "channelFunnelRankHtml", "data-channel-stage"):
        assert not re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(gone),
                             re.sub(r"(?m)^\s*//.*$", "", JS)), gone
    # A bookmark of the retired tab opens the Channels page.
    assert _run("[dashTabIsRetiredChannels('#/dashboard?tab=channels'),"
                " dashTabIsRetiredChannels('#/dashboard?tab=revenue')]") == [True, False]
    nav = _fn("navigate")
    assert ('if (page === "dashboard" && dashTabIsRetiredChannels(window.location.hash)) '
            'page = "channels";') in nav
    assert nav.index("dashTabIsRetiredChannels") < nav.index("PAGES.includes(page)")
    # The Overview's Channel Mix donut stays (it reads /api/dashboard/overview).
    assert "renderDashSourceMix(d)" in JS


def test_17_the_route_answers_404_and_the_audit_fails_if_it_returns(monkeypatch):
    from fastapi.testclient import TestClient
    import api.server as server
    from scripts import audit_customer_revenue_reader_cutover as audit
    monkeypatch.setenv("ADMIN_API_TOKEN", "secret-token")
    client = TestClient(server.app, raise_server_exceptions=False)
    paths = {getattr(r, "path", None) for r in server.app.routes}
    assert "/api/dashboard/channels" not in paths
    assert client.get("/api/dashboard/channels?window=ytd",
                      headers={"Authorization": "Bearer secret-token"}).status_code == 404
    assert "/api/dashboard/channels" in audit.RETIRED_READER_ROUTES
    assert "build_dashboard_channels" in audit.RETIRED_READER_SYMBOLS
    assert not (ROOT / "services" / "dashboard_channels_service.py").exists()
    assert not any(r["consumer"] == "services/dashboard_channels_service.py"
                   for r in audit.INVENTORY)
    a = audit.Audit()
    audit.retired_reader_checks(a, audit.APP_JS and audit._text(audit.APP_JS))
    assert a.checks.get("retired_readers_absent") is True, a.violations
    b = audit.Audit()
    audit.retired_reader_checks(b, JS + '\nfetchJSON(`/api/dashboard/channels?window=${w}`);\n')
    assert b.checks.get("retired_readers_absent") is False
    assert "requests /api/dashboard/channels" in b.violations[0]


def test_18_no_registry_names_the_retired_tab():
    from services import cross_page_parity_service as parity
    assert not any(c == "dashboard/channels" for spec in parity.METRIC_IDENTITIES.values()
                   for c, _ in spec["consumers"])
    assert "source_group_sqls" not in parity.METRIC_IDENTITIES
    registry = (ROOT / "analysis" / "sql_doctrine_registry.py").read_text(encoding="utf-8")
    code = re.sub(r"(?m)^\s*#.*$", "", registry)
    for gone in ("dashboard_channels_service", "/api/dashboard/channels", "renderChanKpiRow",
                 "fetch_source_leads_daily"):
        assert gone not in code, gone
    repo = (ROOT / "db" / "revenue_repository.py").read_text(encoding="utf-8")
    assert "def fetch_source_leads_daily(" not in repo


# ── 7. the System page: run types in plain words ─────────────────────────────

def test_19_a_run_type_reads_in_plain_words_and_an_unknown_one_as_itself():
    out = _run("['daily_incremental_sync', 'daily', 'monthly', 'weekly', 'backfill',"
               " 'a_new_scheduler', '', null].map(runTypeLabel)")
    assert out == ["Daily data sync", "Daily check (morning)", "Monthly refresh",
                   "Weekly refresh (retired)", "Backfill", "a_new_scheduler",
                   "unknown run", "unknown run"]
    # Every cadence monitoring knows has a label of its own.
    from api.monitoring import RETIRED_RUN_TYPES, RUN_TYPE_CADENCE
    labels = _run("RUN_TYPE_LABELS")
    assert set(RUN_TYPE_CADENCE) | set(RETIRED_RUN_TYPES) <= set(labels)
    # The global pill and the per-page strip both go through it.
    load = _fn("loadDataFreshness") if "function loadDataFreshness(" in JS else JS
    assert "runTypeLabel(latestRun.run_type)" in load
    assert "runTypeLabel(run.run_type)" in _fn("renderRunMeta")
    assert 'latestRun.run_type || "unknown"' not in JS


def test_20_the_freshness_maps_cover_every_canonical_status_and_a_partial_run():
    from services.freshness_service import CanonicalFreshnessStatus
    detail = JS[JS.index("const _shortLabels = {"):]
    detail = detail[:detail.index("};")]
    keys = set(re.findall(r"(?m)(?:^|\s)([a-z_]+):\s", detail))
    # datasetDisplayStatus passes a row's own run status through: success,
    # stale, partial, failed, running — each needs a word.
    assert set(CanonicalFreshnessStatus.ALL) | {"success", "stale", "partial", "failed",
                                                "running"} <= keys
    tally = JS[JS.index("const warningCount = statuses.filter"):]
    assert '|| s === "partial"' in tally[:tally.index(").length")]
    # PR-ADS-181A: a healthy System source has a style of its own.
    assert ".system-source--ok {" in CSS
    # ... and the System table's status colours outrank `.data-table td`.
    assert ".data-table td.system-source--ok {" in CSS
    assert ".data-table td.system-source--warn {" in CSS


def test_21_stale_labels_are_gone():
    assert "Phase 1" not in HTML
    cert = _fn("systemCertCard")
    assert "the Pipeline page's lead counts" in cert and ", Leads or" not in cert
    from services.canonical_contract import resolve_settled_window  # noqa: F401
    src = (ROOT / "services" / "canonical_contract.py").read_text(encoding="utf-8")
    assert "such as Deals" not in src and "such as Pipeline" in src


# ── 8. the period in the hash ────────────────────────────────────────────────

def test_22_the_period_is_kept_in_the_hash():
    out = _run("[channelsWindowFromHash('#/channels?window=60d'),"
               " channelsWindowFromHash('#/channels?window=7d'),"
               " channelsWindowFromHash('#/channels'),"
               " channelsHashFor('all_time'), channelsHashFor('180d'), channelsHashFor('bogus')]")
    assert out == ["60d", None, None, "#/channels?window=all_time", "#/channels", "#/channels"]
    load = _fn("loadChannels")
    assert load.index("channelsWindowFromHash(window.location.hash)") < load.index("fetchJSON(")
    wiring = JS[JS.index("// Wire up Channels controls"):JS.index("// Force-close help drawer on startup")]
    assert 'history.replaceState(null, "", channelsHashFor(_channelsWindow));' in wiring
    nav = _fn("navigateToHash")
    assert ('hash = channelsHashFor(channelsWindowFromHash(window.location.hash) || _channelsWindow);'
            in nav)


def test_23_the_header_dates_and_money_use_the_overview_formats():
    assert _run("[channelsUpdatedText({freshness: {as_of: '2026-11-26T06:06:00+00:00'}}),"
                " channelsUpdatedText({}), channelsRange({start_date: '2026-05-31', end_date: '2026-11-25'}),"
                " channelsRange({start_date: '2025-12-01', end_date: '2026-11-25'}),"
                " channelsRange({is_all_time: true, end_date: '2026-11-25'}),"
                " channelMoney(896400), channelMoney(5337.24), channelMoney(0)]") == [
        "Contacts updated 26 Nov, 06:06 UTC", "Contacts update time not stated", "31 May – 25 Nov",
        "1 Dec 2025 – 25 Nov 2026", "All time, to 25 Nov 2026", "$896.4k", "$5.3k", "$0"]
    page = HTML[HTML.index('id="page-channels"'):]
    page = page[:page.index("</section>")]
    assert "Where your customers come from." in page
    assert 'id="channels-updated"' in page and "Read-only" in page


def test_24_the_page_reads_no_page_state_inside_its_renderers():
    for name in ("renderChannelFunnel", "channelDrawerHtml", "channelTableHtml",
                 "channelOutcomeGroupHtml", "channelFunnelGroupHtml", "channelPaidBlockHtml"):
        body = _fn(name)
        for state in ("_channelsData", "_channelsView", "_channelsWindow"):
            assert state not in body, (name, state)


# ── PR-ADS-181A-F1: the truth audit's minors ────────────────────────────────

def test_25_m3_a_leader_under_a_lower_bound_reads_at_least_like_the_block():
    html = _render(_payload(contacts_without_created_at=3))
    group = _group(html, "Contacts acquired")
    text = _text(group)
    # 400 > 300 + 10 (no source) + 3 (undated): still certain, and a bound.
    assert "Most from Paid Search · at least 400" in text
    assert "Most from Paid Search · at least 20" in text
    lead = group[group.index("channels-kpi__lead"):]
    assert 'class="dash-lower-bound" data-lower-bound="contacts"' in lead
    # The block's renderer draws it, not a local string.
    assert "funnelBlockValueHtml(" in _fn("channelCountHtml")
    assert "at least" not in _fn("channelCountHtml") and "at least" not in _fn("channelLeaderHtml")
    # Exact cohort: no "at least" anywhere on the leader.
    assert "Most from Paid Search · 400" in _text(_group(_render(_payload()), "Contacts acquired"))


def _ledger_stale(p=None):
    p = p or _payload()
    reason = "canonical_ledger_stale"
    p["outcomes"] = {**p["outcomes"], "channels": None, "totals": None, "google_ads_line": None,
                     "spend": {"google_ads": {"value": 186803.57, "status": "published",
                                              "reason": None}},
                     "publication": {k: _m(None, "unavailable", reason)
                                     for k in ("closed_won_deals", "revenue_usd", "customers")}}
    return p


def test_26_m4_no_google_line_under_a_stale_ledger_says_the_ledger_is_stale():
    html = _render(_ledger_stale())
    card = html[html.index("channels-paid__card--google"):html.index("channels-paid__card--meta")]
    text = _text(card)
    assert "Spend $186.8k" in text                    # spend still shows
    assert text.count("the deal sync is overdue") == 3   # revenue and both ratios
    assert "evidence partition" not in text
    # A PUBLISHED window with no line is still a missing partition.
    p = _payload()
    p["outcomes"]["google_ads_line"] = None
    held = _text(_render(p))
    assert "the Google Ads evidence partition is unavailable" in held


_ASYNC = None


def _run_async(script):
    """The 161B vm harness, awaiting the probe's promise (loadChannels is async)."""
    import tempfile
    from tests import test_pr_ads_161b_marketing_outcome_cohorts as t161b
    node = shutil.which("node")
    assert node, "node is required: CI runs `node --check static/app.js`"
    harness = t161b._VM_HARNESS.replace(
        "process.stdout.write(JSON.stringify(out === undefined ? null : out));",
        "Promise.resolve(out).then((v) => process.stdout.write(JSON.stringify(v === undefined ? null : v)));")
    assert harness != t161b._VM_HARNESS
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "h.js").write_text(harness, encoding="utf-8")
        (d / "app.js").write_text(JS, encoding="utf-8")
        (d / "p.js").write_text(script, encoding="utf-8")
        out = subprocess.run([node, str(d / "h.js"), str(d / "app.js"), str(d / "p.js")],
                             capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout)


def test_27_m5_a_reload_redraws_or_closes_the_open_drawer():
    old, new = _payload(), _payload()
    new["channels"][0]["contacts"] = 777
    new["window"]["start_date"] = "2026-09-27"
    gone = _payload()
    gone["channels"] = gone["channels"][1:]
    gone["outcomes"]["channels"] = gone["outcomes"]["channels"][1:]
    probe = """
      location = {hash: "#/channels?window=60d"};
      const run = async (data, fail) => {
        fetchJSON = async () => { if (fail) throw new Error("x"); return data; };
        _channelsData = %s; _channelsView.drawer = "PAID_SEARCH"; renderChannelDrawer();
        const before = __els["channels-drawer-root"].innerHTML;
        await loadChannels();
        return [before, __els["channels-drawer-root"].innerHTML, _channelsView.drawer];
      };
      (async () => [await run(%s, false), await run(%s, false), await run(null, true)])()
    """ % (json.dumps(old), json.dumps(new), json.dumps(gone))
    redrawn, closed, failed = _run_async(probe)
    assert "400" in _text(redrawn[0]) and "31 May" in redrawn[0]
    assert "777" in _text(redrawn[1]) and "27 Sep" in redrawn[1] and redrawn[2] == "PAID_SEARCH"
    assert closed[1] == "" and closed[2] is None       # the channel is not in the new response
    assert failed[1] == "" and failed[2] is None       # a failed load closes it
    # A row of the table being replaced opens nothing.
    wiring = JS[JS.index("// Wire up Channels controls"):JS.index("// Force-close help drawer on startup")]
    open_fn = wiring[wiring.index("const openChannel = (row) => {"):]
    open_fn = open_fn[:open_fn.index("};")]
    assert 'if (channelsBody.classList.contains("is-refreshing")) return;' in open_fn
    assert "_channelsView.drawer = null;" not in wiring[:wiring.index("const channelsBody")]


def test_28_m1_the_funnel_leader_is_refused_when_the_unplaced_contacts_could_flip_it():
    # (a) the no-source row: 400 > 300 + 150 is false.
    p = _payload()
    p["channels"][2]["contacts"] = 150
    text = _text(_group(_render(p), "Contacts acquired"))
    assert "Most from Paid Search · 400" not in text and "· 400" not in text
    # (b) contacts with no created date: 400 > 300 + 10 + 95 is false.
    text = _text(_group(_render(_payload(contacts_without_created_at=95)), "Contacts acquired"))
    assert "· at least 400" not in text and "· 400" not in text
    # The SQL leader keeps its own margin (20 > 9 + 0 + 95 is false too).
    assert "Most from" not in text


def test_29_m2_under_a_lower_bound_sql_rate_ranks_nothing():
    p = _payload(contacts_without_created_at=3)
    # API order: a small sample first, then Paid Search 5%, Direct 10%.
    p["channels"].insert(0, _funnel_row("AI_REFERRALS", "AI Referrals", 20, 5))
    p["channels"][2]["reached_sql"] = 30
    names = _names(_render(p, {"sort": "sql_rate"}))
    # Every rate is unknown under the bound: nothing is ranked, not even the
    # small sample — the API's order stands.
    assert names[:3] == ["AI Referrals", "Paid Search", "Direct Traffic"]
    exact = _names(_render({**p, "contacts_without_created_at": 0}, {"sort": "sql_rate"}))
    assert exact[:3] == ["Direct Traffic", "Paid Search", "AI Referrals"]


def test_30_a_stale_dataset_status_counts_as_a_warning_in_the_strip():
    tally = JS[JS.index("const warningCount = statuses.filter"):]
    tally = tally[:tally.index(").length")]
    assert '|| s === "stale"' in tally
    detail = JS[JS.index("const _shortLabels = {"):]
    assert 'stale: "Stale"' in detail[:detail.index("};")]
    # datasetDisplayStatus is the source of the raw "stale".
    assert 'return "stale";' in _fn("datasetDisplayStatus")
