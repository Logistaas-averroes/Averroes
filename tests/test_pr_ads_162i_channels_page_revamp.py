"""PR-ADS-162I — the Channels page, redesigned on the dashboard's components.

The redesign changes presentation only. Every figure on the page is a count
`/api/channel-funnel` published, or a share of two of them; the page still
decides nothing about publication.

* a withheld funnel shows the word and the reason and no figure, ranking,
  control or table;
* a count the payload does not carry is a dash — never 0, never NaN;
* a share of very few contacts is shown, marked, and ranked after the channels
  with enough contacts to compare — a 5-of-9 rate never tops the list;
* rankings, sorting and expanding rows never change a number.

PR-ADS-181A ported these cases to the redesigned page: the totals strip is the
"Contacts acquired" KPI group (plus the "All channels" drawer), the two ranking
panels and the stage picker are the one ranked table's sortable SQL and SQL
rate columns, and a channel's sources open in its drawer instead of hidden
table rows. Every truth assertion is kept; ``docs/66`` lists each change.

There is no JavaScript test harness in this repository: these cases evaluate
the real functions from `static/app.js` with the `node` binary CI already has.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

STAGES = [{"key": "reached_mql", "label": "MQL"},
          {"key": "reached_sql", "label": "SQL"},
          {"key": "reached_opportunity", "label": "Opportunity"},
          {"key": "reached_customer_stage", "label": "Customer stage"}]


def _row_(contacts, mql, sql, opp, cust):
    return {"contacts": contacts, "reached_mql": mql, "reached_sql": sql,
            "reached_opportunity": opp, "reached_customer_stage": cust}


def _channel(source, label, values, drilldowns=None):
    row = {"source": source, "label": label, **_row_(*values)}
    row["drilldowns"] = [{"value": v, **_row_(*x)} for v, x in
                         (drilldowns or [("(none)", values)])]
    return row


def _payload(**over):
    """Shaped like the 180-day funnel production published on 2026-11-18:
    the totals and the first seven channels are the figures on that page. The
    last channel is added here so that the channels add up to the totals, as
    the API guarantees for a published funnel."""
    data = {
        "publication": {"status": "published"},
        "window": {"start_date": "2026-05-23", "end_date": "2026-11-18"},
        "freshness": {"as_of": "2026-11-18T06:00:00+00:00"},
        "stages": STAGES,
        "totals": _row_(2951, 2641, 377, 231, 73),
        "channels": [
            _channel("PAID_SOCIAL", "Paid Social", (1147, 1146, 46, 23, 0),
                     [("linkedin", (640, 640, 28, 14, 0)),
                      ("facebook", (507, 506, 18, 9, 0))]),
            _channel("PAID_SEARCH", "Paid Search", (746, 727, 91, 52, 2),
                     [("gulf", (210, 205, 30, 18, 1)),
                      ("(other: 9 values)", (536, 522, 61, 34, 1))]),
            _channel("OFFLINE", "Offline Sources", (582, 295, 135, 98, 66)),
            _channel("DIRECT_TRAFFIC", "Direct Traffic", (392, 389, 63, 34, 3)),
            _channel("ORGANIC_SEARCH", "Organic Search (SEO)", (55, 55, 30, 17, 0)),
            _channel("AI_REFERRALS", "AI Referrals", (15, 15, 5, 2, 2),
                     [("chatgpt.com", (15, 15, 5, 2, 2))]),
            _channel("EMAIL_MARKETING", "Email Marketing", (9, 9, 5, 3, 0)),
            _channel("REFERRALS", "Referrals", (5, 5, 2, 2, 0)),
        ],
        "notes": {"stages": "S.", "channels": "C.", "maturity": "M."},
        # build_channel_funnel publishes this beside every published funnel
        # (it was missing from this fixture). PR-ADS-180B reads it: above
        # zero — or absent — every strip column is a lower bound.
        "contacts_without_created_at": 0,
    }
    data.update(over)
    return data


def _node(expr: str) -> str:
    """PR-ADS-180A: the page's funnel is the shared block (funnelBlockHtml,
    rendered through canonicalMetricHtml), defined outside the Channels span,
    so the real app.js is evaluated WHOLE (the 161B vm harness) instead of the
    Channels span plus escapeHtml. Same expression, same string result."""
    from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app
    return _run_app(f"String({expr})")


def _render(payload, view=None) -> str:
    return _node(f"renderChannelFunnel({json.dumps(payload)}, "
                 f"{json.dumps(view or {})})")


def _drawer(payload, key) -> str:
    """PR-ADS-181A: a channel's drawer (its funnel, sources and platforms)."""
    return _node(f"channelDrawerHtml({json.dumps(payload)}, {json.dumps(key)})")


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _table(html: str) -> str:
    return html[html.index("<table"):html.index("</table>")]


def _table_channels(html: str) -> list:
    return re.findall(r'<span class="channels-name">([^<]+)</span>', _table(html))


def _row(html: str, label: str) -> str:
    for tr in re.findall(r"<tr[^>]*>.*?</tr>", _table(html), re.S):
        if f">{label}<" in tr:
            return _text(tr)
    raise AssertionError(label)


# ── the totals: the "Contacts acquired" group and the All channels drawer ──

def test_01_the_totals_strip_shows_each_published_count_and_its_share():
    text = _text(_render(_payload()))
    assert "New contacts 2,951 created in these days" in text
    assert "Reached SQL so far 377 12.8% of contacts" in text
    # Every stage, with its share, in the All channels drawer (the shared block).
    drawer = _text(_drawer(_payload(), "__all__"))
    assert "Contacts acquired 2,951 in this period" in drawer
    assert "Reached MQL 2,641 89.5% of contacts" in drawer
    assert "Reached SQL 377 12.8% of contacts" in drawer
    assert "Reached Opportunity 231 7.8% of contacts" in drawer
    assert "Reached Customer stage 73 2.5% of contacts" in drawer
    assert _node(f"channelsUpdatedText({json.dumps(_payload())})") == "Contacts updated 18 Nov, 06:00 UTC"


# ── the two rankings are the table's SQLs and SQL rate columns ───────────────

def test_02_the_volume_ranking_orders_channels_by_the_published_count():
    html = _render(_payload(), {"sort": "sqls"})
    names = _table_channels(html)
    assert names == ["Offline Sources", "Paid Search", "Direct Traffic",
                     "Paid Social", "Organic Search (SEO)", "AI Referrals",
                     "Email Marketing", "Referrals"]
    assert _row(html, "Offline Sources") == "Offline Sources 582 135 23.2%"
    assert _row(html, "Paid Search") == "Paid Search 746 91 12.2%"
    # The counts are the PUBLISHED ones, and the channel counts add to the total.
    counts = [int(_row(html, n).split()[-2].replace(",", "")) for n in names]
    assert sum(counts) == 377
    assert _row(html, "All channels") == "All channels 2,951 377 12.8%"


def test_03_a_rate_on_few_contacts_is_ranked_after_the_comparable_channels():
    html = _render(_payload(), {"sort": "sql_rate", "dir": "desc"})
    names = _table_channels(html)
    # Email Marketing converts 5 of 9 (55.6%) — the highest rate on the page.
    # It is after the channels with enough contacts, not at the top.
    assert names == ["Organic Search (SEO)", "Offline Sources",
                     "Direct Traffic", "Paid Search", "Paid Social",
                     "Email Marketing", "Referrals", "AI Referrals"]
    assert _row(html, "Organic Search (SEO)") == "Organic Search (SEO) 55 30 54.5%"
    assert _row(html, "Email Marketing").endswith("9 5 55.6%")      # shown, not hidden
    assert html.count(">small sample</span>") == 3


def test_04_the_small_sample_limit_is_thirty_contacts_exactly():
    assert _node("CHANNEL_FUNNEL_SMALL_SAMPLE") == "30"
    assert _node("channelFunnelIsSmall({contacts: 29})") == "true"
    assert _node("channelFunnelIsSmall({contacts: 30})") == "false"
    # An unknown contact count is not "small": nothing is claimed about it.
    assert _node("channelFunnelIsSmall({})") == "false"


def test_05_the_column_picked_changes_what_is_compared_never_a_number():
    rows = lambda h: sorted(re.findall(r"<tr[^>]*>.*?</tr>", _table(h), re.S))  # noqa: E731
    by_contacts = _render(_payload())
    by_sqls = _render(_payload(), {"sort": "sqls"})
    by_rate = _render(_payload(), {"sort": "sql_rate"})
    strip = lambda t: re.sub(r' (?:aria-sort|class)="[^"]*"', "", t)  # noqa: E731
    assert [strip(r) for r in rows(by_contacts)] == [strip(r) for r in rows(by_sqls)] \
        == [strip(r) for r in rows(by_rate)]
    assert "Channels ranked by SQLs" in by_sqls
    # An unknown sort key falls back to the default rather than showing nothing.
    assert _table_channels(_render(_payload(), {"sort": "nope"}))[0] == "Paid Social"


def test_06_no_contact_at_a_stage_is_a_published_zero_and_names_no_leader():
    payload = _payload(totals=_row_(10, 6, 0, 0, 0), channels=[
        _channel("PAID_SOCIAL", "Paid Social", (10, 6, 0, 0, 0))])
    text = _text(_render(payload))
    assert "Reached SQL so far 0 0.0% of contacts" in text
    # Zero leads nowhere: no "Most from" on a count that is 0 everywhere.
    sql_card = text[text.index("Reached SQL so far"):text.index("Channels ranked")]
    assert "Most from" not in sql_card


# ── the table ─────────────────────────────────────────────────────────────

def test_07_the_table_keeps_every_count_and_share_the_old_page_showed():
    html = _render(_payload())
    assert _row(html, "All channels") == "All channels 2,951 377 12.8%"
    assert _row(html, "Paid Search") == "Paid Search 746 91 12.2%"
    assert _row(html, "Email Marketing") == "Email Marketing small sample 9 5 55.6%"
    # Every stage count and its share of the channel's contacts: in the drawer.
    text = _text(_drawer(_payload(), "PAID_SEARCH"))
    for part in ("Contacts acquired 746", "Reached MQL 727 97.5% of contacts",
                 "Reached SQL 91 12.2% of contacts", "Reached Opportunity 52 7.0% of contacts",
                 "Reached Customer stage 2 0.3% of contacts"):
        assert part in text, part
    assert "linkedin 640" in _text(_drawer(_payload(), "PAID_SOCIAL"))


def test_08_sources_are_in_the_drawer_of_their_own_channel():
    social = _drawer(_payload(), "PAID_SOCIAL")
    search = _drawer(_payload(), "PAID_SEARCH")
    assert "linkedin" in social and "facebook" in social and "gulf" not in social
    assert "gulf" in search and "(other: 9 values)" in search and "linkedin" not in search
    assert "chatgpt.com" in _drawer(_payload(), "AI_REFERRALS")
    # A channel whose only detail is "(none)" has no source list to show.
    assert "Top sources" not in _drawer(_payload(), "OFFLINE")
    # Every channel row opens its own drawer.
    opens = re.findall(r'data-channel-open="([A-Z_]+)"', _render(_payload()))
    assert sorted(opens) == sorted(c["source"] for c in _payload()["channels"])
    assert "(none)" not in _render(_payload())


@pytest.mark.parametrize("view,first,last", [
    ({"sort": "contacts"}, "Paid Social", "Referrals"),
    ({"sort": "sqls"}, "Offline Sources", "Referrals"),
    ({"sort": "contacts", "dir": "asc"}, "Referrals", "Paid Social"),
    ({"sort": "channel"}, "AI Referrals", "Referrals"),
    ({"sort": "not_a_column"}, "Paid Social", "Referrals"),
])
def test_09_sorting_reorders_channel_rows_and_nothing_else(view, first, last):
    html = _render(_payload(), view)
    rows = re.findall(r"<tr[^>]*>.*?</tr>", _table(html).split("<tbody>")[1], re.S)
    assert ">All channels<" in rows[0]           # the total never moves
    names = _table_channels(html)
    assert names[0] == first and names[-1] == last
    assert sorted(names) == sorted(c["label"] for c in _payload()["channels"])


# ── truth rules ───────────────────────────────────────────────────────────

def test_10_a_count_the_payload_does_not_carry_is_a_dash_never_zero():
    channels = [_channel("PAID_SOCIAL", "Paid Social", (40, 30, 5, 2, 0)),
                _channel("PAID_SEARCH", "Paid Search", (40, 30, 5, 2, 0))]
    del channels[0]["reached_sql"]
    channels[0]["reached_customer_stage"] = None
    totals = _row_(80, 60, 10, 4, 0)
    del totals["reached_opportunity"]
    payload = _payload(totals=totals, channels=channels)
    html = _render(payload)
    assert "NaN" not in html and "undefined" not in html and "null" not in html
    assert _row(html, "Paid Social") == "Paid Social 40 — —"
    assert _row(html, "Paid Search") == "Paid Search 40 5 12.5%"
    # A count one channel lacks makes the leader unclear: none is named.
    text = _text(html)
    assert "Most from" not in text[text.index("Reached SQL so far"):text.index("Channels ranked")]
    # PR-ADS-180A: in the shared block the rule is the word and its reason.
    drawer = _text(_drawer(payload, "__all__"))
    assert "Reached Opportunity Unavailable the server sent no count for this stage share unknown" in drawer
    social = _text(_drawer(payload, "PAID_SOCIAL"))
    assert "Reached SQL Unavailable the server sent no count for this stage" in social
    assert "Reached Customer stage Unavailable" in social


@pytest.mark.parametrize("status,word", [("withheld", "Withheld"),
                                         ("unavailable", "Unavailable")])
def test_11_a_withheld_funnel_shows_no_figure_ranking_control_or_table(status,
                                                                       word):
    payload = _payload(publication={"status": status,
                                    "reason": "source_last_incremental_failed"})
    html = _render(payload, {"sort": "sqls"})
    assert f"<strong>{word}</strong>" in html
    assert "the last contact sync failed" in html
    for leak in ("2,951", "377", "1,147", "Paid Social", "<table", "data-channel-sort",
                 "small sample", "Most from"):
        assert leak not in html, leak
    drawer = _drawer(payload, "PAID_SOCIAL")
    for leak in ("1,147", "linkedin", "Top sources"):
        assert leak not in drawer, leak
    assert f"<strong>{word}</strong>" in drawer


def test_12_names_from_hubspot_are_escaped_everywhere_they_appear():
    evil = '<img src=x onerror=alert(1)>'
    payload = _payload(channels=[
        _channel('A"><script>', evil, (40, 30, 5, 2, 0),
                 [(evil, (40, 30, 5, 2, 0)), ("b", (0, 0, 0, 0, 0))])],
        totals=_row_(40, 30, 5, 2, 0))
    html = _render(payload) + _drawer(payload, 'A"><script>')
    assert "<img" not in html and "<script>" not in html
    assert html.count("&lt;img src=x onerror=alert(1)&gt;") >= 4


def test_13_the_page_has_no_attribution_ratio_wording():
    """PR-ADS-168A changed this contract: the page also shows customers WON
    (distinct companies whose deals closed in the same days). The guard that
    matters: no attribution-ratio word, the stage column is the lifecycle stage
    in contacts, no outcome cell is a percentage, and the total customers
    figure is the window's own metric — never a sum of the rows. PR-ADS-181A:
    the same rules, on the redesigned renderers."""
    src = (ROOT / "static/app.js").read_text()
    span = src[src.index("// ── Channels (PR-ADS-162A; redesigned in PR-ADS-181A)"):
               src.index("// ── System: the state of the platform")]
    assert not re.search(r"\b(ROAS|CAC|CPQL)\b", span)

    def fn(name):
        i = span.index(f"function {name}(")
        return span[i:span.index("\n}\n", i)]
    # Distinct headers: a customer is a company (Customers won, distinct
    # companies); the funnel's last column is the Customer STAGE, in contacts.
    assert '"Customers won", canonicalMetricHtml(customers' in fn("channelOutcomeGroupHtml")
    assert '"distinct companies"' in fn("channelOutcomeGroupHtml")
    # No percentage in an outcome cell: the cell renderer computes none.
    cells = fn("channelCellHtml")
    outcome = cells[cells.index('case "customers": case "revenue"'):cells.index('case "share"')]
    assert "%" not in outcome and "channelPct" not in outcome \
        and "channelFunnelRate" not in outcome
    # The total row and the group read the window's metric, not the rows.
    assert 'channelOutcomeMetric(o, "customers")' in fn("channelTotalCellHtml")
    assert 'channelOutcomeMetric(o, "customers")' in fn("channelOutcomeGroupHtml")
    for name in ("renderChannelFunnel", "channelTableHtml", "channelTotalCellHtml",
                 "channelOutcomeGroupHtml"):
        assert "reduce(" not in fn(name), name
    drawer = _text(_drawer(_payload(), "__all__"))
    assert "Reached Customer stage 73" in drawer


# ── the controls ──────────────────────────────────────────────────────────

def test_14_the_window_buttons_are_exactly_the_windows_the_api_accepts():
    from analysis.evidence_windows import resolve_evidence_window
    html = (ROOT / "static/index.html").read_text()
    start = html.index('id="page-channels"')
    page = html[start:html.index('</section>', start)]
    buttons = re.findall(r'data-channels-window="([a-z0-9_]+)"', page)
    assert buttons == ["30d", "60d", "180d", "all_time"]
    for key in buttons:
        assert resolve_evidence_window(key)["key"] == key
    active = re.findall(r'is-active" data-channels-window="([a-z0-9_]+)" aria-pressed="true"', page)
    from services.channel_funnel_service import DEFAULT_WINDOW
    assert active == [DEFAULT_WINDOW]
    src = (ROOT / "static/app.js").read_text()
    assert f'let _channelsWindow = "{DEFAULT_WINDOW}";' in src
    # PR-ADS-181A: the hash's periods are the same four.
    assert _node("JSON.stringify(CHANNELS_WINDOWS)") == json.dumps(buttons, separators=(",", ":"))


def test_15_a_failed_load_does_not_leave_the_previous_figures_on_screen():
    src = (ROOT / "static/app.js").read_text()
    body = src[src.index("async function loadChannels()"):
               src.index("// ── System: the state of the platform")]
    catch = body[body.index("} catch (err) {"):]
    assert "_channelsData = null;" in catch
    assert "<strong>Unavailable</strong>" in catch
    # A superseded response is dropped before it can render.
    assert body.count("if (token !== _channelsReqSeq) return;") == 2


def test_16_every_control_the_renderer_emits_is_wired():
    src = (ROOT / "static/app.js").read_text()
    wiring = src[src.index("// Wire up Channels controls"):
                 src.index("// Force-close help drawer on startup")]
    for attr in ("data-channels-window", "data-channel-sort", "data-channel-open",
                 "data-channels-expand", "data-channels-drawer-close"):
        assert f"[{attr}]" in wiring, attr
    html = _render(_payload()) + _drawer(_payload(), "PAID_SEARCH")
    for attr in ("data-channel-sort", "data-channel-open", "data-channels-expand",
                 "data-channels-drawer-close"):
        assert attr in html, attr
