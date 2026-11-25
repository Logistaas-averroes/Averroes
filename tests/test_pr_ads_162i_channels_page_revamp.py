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


def _row(contacts, mql, sql, opp, cust):
    return {"contacts": contacts, "reached_mql": mql, "reached_sql": sql,
            "reached_opportunity": opp, "reached_customer_stage": cust}


def _channel(source, label, values, drilldowns=None):
    row = {"source": source, "label": label, **_row(*values)}
    row["drilldowns"] = [{"value": v, **_row(*x)} for v, x in
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
        "totals": _row(2951, 2641, 377, 231, 73),
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


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _table_channels(html: str) -> list:
    table = html[html.index("<table"):html.index("</table>")]
    return re.findall(r"<strong>([^<]+)</strong>", table)


def _panel(html: str, title_start: str) -> str:
    start = html.index(title_start)
    return html[start:html.index('<div class="dash-panel', start)]


# ── the totals strip ──────────────────────────────────────────────────────

def test_01_the_totals_strip_shows_each_published_count_and_its_share():
    text = _text(_render(_payload()))
    assert "Contacts acquired 2,951 in this period" in text
    assert "Reached MQL 2,641 89.5% of contacts" in text
    assert "Reached SQL 377 12.8% of contacts" in text
    assert "Reached Opportunity 231 7.8% of contacts" in text
    assert "Reached Customer stage 73 2.5% of contacts" in text
    assert "measured as of 2026-11-18 06:00 UTC" in text


# ── the two rankings ──────────────────────────────────────────────────────

def test_02_the_volume_ranking_orders_channels_by_the_published_count():
    html = _panel(_render(_payload()), "Where your SQLs come from")
    names = re.findall(r'channel-funnel-rank__name">([^<]+)<', html)
    assert names == ["Offline Sources", "Paid Search", "Direct Traffic",
                     "Paid Social", "Organic Search (SEO)", "AI Referrals",
                     "Email Marketing", "Referrals"]
    text = _text(html)
    assert "Offline Sources 135 35.8% of all" in text      # 135 / 377
    assert "Paid Search 91 24.1% of all" in text           # 91 / 377
    # The shares are of the PUBLISHED total, and the channel counts add to it.
    counts = [int(n.replace(",", "")) for n in
              re.findall(r"<strong>([\d,]+)</strong>", html)]
    assert sum(counts) == 377


def test_03_a_rate_on_few_contacts_is_ranked_after_the_comparable_channels():
    html = _panel(_render(_payload()), "Which channels convert best to SQL")
    names = re.findall(r'channel-funnel-rank__name">([^<]+)<', html)
    # Email Marketing converts 5 of 9 (55.6%) — the highest rate on the page.
    # It is below the divider, not at the top.
    assert names == ["Organic Search (SEO)", "Offline Sources",
                     "Direct Traffic", "Paid Search", "Paid Social",
                     "Email Marketing", "Referrals", "AI Referrals"]
    divider = html.index("Fewer than 30 contacts")
    assert html.index("Paid Social") < divider < html.index("Email Marketing")
    text = _text(html)
    assert "Organic Search (SEO) 54.5% 30 of 55" in text
    assert "Email Marketing 55.6% 5 of 9" in text           # shown, not hidden
    assert html.count("channel-funnel-rank--small") == 3


def test_04_the_small_sample_limit_is_thirty_contacts_exactly():
    assert _node("CHANNEL_FUNNEL_SMALL_SAMPLE") == "30"
    assert _node("channelFunnelIsSmall({contacts: 29})") == "true"
    assert _node("channelFunnelIsSmall({contacts: 30})") == "false"
    # An unknown contact count is not "small": nothing is claimed about it.
    assert _node("channelFunnelIsSmall({})") == "false"


def test_05_the_stage_picker_changes_what_is_compared_never_a_number():
    sql = _render(_payload())
    opp = _render(_payload(), {"stage": "reached_opportunity"})
    assert "Where your opportunities come from" in opp
    assert "Which channels convert best to Opportunity" in opp
    assert "Offline Sources 98 42.4% of all" in _text(opp)   # 98 / 231
    table = lambda h: h[h.index("<table"):h.index("</table>")]  # noqa: E731
    assert table(sql) == table(opp)
    # An unknown stage key falls back to SQL rather than showing nothing.
    assert "Where your SQLs come from" in _render(_payload(), {"stage": "nope"})


def test_06_no_contact_at_a_stage_is_said_in_words_not_drawn_as_bars():
    payload = _payload(totals=_row(10, 6, 0, 0, 0), channels=[
        _channel("PAID_SOCIAL", "Paid Social", (10, 6, 0, 0, 0))])
    html = _panel(_render(payload), "Where your SQLs come from")
    assert "No contact acquired in this period has reached SQL yet." in html
    assert "channel-funnel-rank__name" not in html


# ── the table ─────────────────────────────────────────────────────────────

def test_07_the_table_keeps_every_count_and_share_the_old_page_showed():
    html = _render(_payload())
    table = _text(html[html.index("<table"):html.index("</table>")])
    assert "All channels 2,951 2,641 (89.5%) 377 (12.8%) 231 (7.8%) 73 (2.5%)" in table
    assert "Paid Search 2 sources 746 727 (97.5%) 91 (12.2%) 52 (7.0%) 2 (0.3%)" in table
    assert "linkedin 640 640 (100.0%) 28 (4.4%) 14 (2.2%) 0 (0.0%)" in table
    assert "Email Marketing small sample 9 9 (100.0%) 5 (55.6%) 3 (33.3%) 0 (0.0%)" in table


def test_08_sources_are_in_the_page_and_collapsed_under_their_own_channel():
    html = _render(_payload())
    children = re.findall(
        r'<tr class="channel-funnel-child" data-channel-child="([A-Z_]+)" hidden>'
        r'<td><span class="channel-funnel-drill">([^<]+)</span>', html)
    assert children == [("PAID_SOCIAL", "linkedin"), ("PAID_SOCIAL", "facebook"),
                        ("PAID_SEARCH", "gulf"),
                        ("PAID_SEARCH", "(other: 9 values)"),
                        ("AI_REFERRALS", "chatgpt.com")]
    toggles = re.findall(r'data-channel-toggle="([A-Z_]+)" aria-expanded="false"', html)
    assert toggles == ["PAID_SOCIAL", "PAID_SEARCH", "AI_REFERRALS"]
    # A channel whose only detail is "(none)" has nothing to open.
    assert 'data-channel-toggle="OFFLINE"' not in html
    assert "(none)" not in html


@pytest.mark.parametrize("sort,first,last", [
    ("contacts", "Paid Social", "Referrals"),
    ("reached_sql", "Offline Sources", "Referrals"),
    ("reached_customer_stage", "Offline Sources", None),
    ("not_a_column", "Paid Social", "Referrals"),
])
def test_09_sorting_reorders_channel_rows_and_nothing_else(sort, first, last):
    html = _render(_payload(), {"sort": sort})
    names = _table_channels(html)
    assert names[0] == "All channels"            # the total never moves
    assert names[1] == first
    if last:
        assert names[-1] == last
    assert sorted(names[1:]) == sorted(
        c["label"] for c in _payload()["channels"])
    # Each source row still sits under its own channel.
    order = re.findall(r'data-channel-(?:toggle|child)="([A-Z_]+)"', html)
    for source in ("PAID_SOCIAL", "PAID_SEARCH", "AI_REFERRALS"):
        at = [i for i, s in enumerate(order) if s == source]
        assert at == list(range(at[0], at[0] + len(at))), source


# ── truth rules ───────────────────────────────────────────────────────────

def test_10_a_count_the_payload_does_not_carry_is_a_dash_never_zero():
    channels = [_channel("PAID_SOCIAL", "Paid Social", (40, 30, 5, 2, 0))]
    del channels[0]["reached_opportunity"]
    channels[0]["reached_customer_stage"] = None
    totals = _row(40, 30, 5, 2, 0)
    del totals["reached_opportunity"]
    html = _render(_payload(totals=totals, channels=channels))
    assert "NaN" not in html and "undefined" not in html and "null" not in html
    text = _text(html)
    # PR-ADS-180A: the totals are the shared funnel block, whose rule is the
    # word and its reason, never a dash (the table below keeps its "—").
    assert "Reached Opportunity Unavailable the server sent no count for this stage share unknown" in text
    table = _text(html[html.index("<table"):html.index("</table>")])
    assert "Paid Social 40 30 (75.0%) 5 (12.5%) — —" in table
    assert "All channels 40 30 (75.0%) 5 (12.5%) — 0 (0.0%)" in table
    # A channel with no count for the compared stage is left out of its
    # ranking; it is not ranked as zero.
    opp = _render(_payload(totals=totals, channels=channels),
                  {"stage": "reached_opportunity"})
    assert "No channel has a count for this stage." in opp


@pytest.mark.parametrize("status,word", [("withheld", "Withheld"),
                                         ("unavailable", "Unavailable")])
def test_11_a_withheld_funnel_shows_no_figure_ranking_control_or_table(status,
                                                                       word):
    payload = _payload(publication={"status": status,
                                    "reason": "source_last_incremental_failed"})
    html = _render(payload, {"stage": "reached_sql", "sort": "reached_sql"})
    assert f"<strong>{word}</strong>" in html
    assert "the last contact sync failed" in html
    for leak in ("2,951", "377", "Paid Social", "<table", "channel-funnel-rank",
                 "data-channel-stage", "data-channel-sort", "dash-outcomes"):
        assert leak not in html, leak


def test_12_names_from_hubspot_are_escaped_everywhere_they_appear():
    evil = '<img src=x onerror=alert(1)>'
    payload = _payload(channels=[
        _channel('A"><script>', evil, (40, 30, 5, 2, 0),
                 [(evil, (40, 30, 5, 2, 0)), ("b", (0, 0, 0, 0, 0))])],
        totals=_row(40, 30, 5, 2, 0))
    html = _render(payload)
    assert "<img" not in html and "<script>" not in html
    assert html.count("&lt;img src=x onerror=alert(1)&gt;") >= 4


def test_13_the_page_has_no_attribution_ratio_wording():
    """PR-ADS-168A changed this contract. Until then the Channels page showed
    contacts only, so the word "customers" was banned from its code outright.
    It now also shows customers WON (distinct companies whose deals closed in
    the same days, PR-ADS-168A). The guard that matters is therefore no longer
    "never the word" but: the two figures carry distinct headers, no outcome
    cell is a percentage, and the total customers figure is the window's own
    metric — never a sum of the rows."""
    src = (ROOT / "static/app.js").read_text()
    span = src[src.index("const CHANNEL_FUNNEL_REASON_LABELS = {"):
               src.index("// ── System: the state of the platform")]
    assert not re.search(r"\b(ROAS|CAC|CPQL)\b", span)
    # Distinct headers: a stage count in contacts, customers won in companies.
    assert '`${st.label} (contacts)`' in span
    assert '"Customers won (distinct companies)"' in span
    # Every "customers" in the code is the won-companies figure or its notes.
    bare = re.findall(r"[^\n]*\bcustomers\b[^\n]*", span.lower())
    allowed = ("customers won", "rowmetrichtml(r, \"customers\"", "on.customers")
    assert bare and all(any(a in line for a in allowed) for line in bare), bare
    # No percentage in an outcome cell: the outcome cell renderer computes none.
    def fn(name):
        i = span.index(f"function {name}(")
        return span[i:span.index("\n}\n", i)]
    cells = fn("channelOutcomeCellsHtml")
    assert "%" not in cells and "channelFunnelPct" not in cells \
        and "channelFunnelRate" not in cells
    # The total row and the strip read the window's metric, not the rows.
    render = fn("renderChannelFunnel")
    assert "channelOutcomeCellsHtml(o.totals, oOpts)" in render
    assert "reduce(" not in render
    assert 'metricOf("customers")' in fn("channelOutcomeStripHtml")


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
    for attr in ("data-channels-window", "data-channel-stage",
                 "data-channel-sort", "data-channel-toggle",
                 "data-channel-expand-all"):
        assert f"[{attr}]" in wiring, attr
    html = _render(_payload())
    for attr in ("data-channel-stage", "data-channel-sort",
                 "data-channel-toggle", "data-channel-expand-all"):
        assert attr in html, attr
