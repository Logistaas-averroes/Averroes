"""PR-ADS-176B — one Countries page.

The Countries evidence page (``#/geo``, ``GET /api/geo``) and Country Revenue &
Spend (``#/roas-countries``) were two pages under the Countries menu item with
different windows, a different spend gate and a different country population.
The owner's decisions (2026-11-23, ``docs/58`` §3): business windows only, the
strict spend gate, and no lead-quality columns. This PR keeps one page —
``roas-countries``, reading the mart's country view — built in the Dashboard's
visual language, and deletes the other whole.

Every case runs the real ``static/app.js`` in ``node`` (the harness of
``tests/test_pr_ads_161b_marketing_outcome_cohorts``) or reads the real
``static/index.html`` / ``static/styles.css``. Every case fails on 2eda9ac
(PR-ADS-176A-F2). Case 06's strict-gate half already held there (it fails only
on the new table); it is shown to be a guard by mutating the page's gate check
away, which prints the withheld figure.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")

BUSINESS_WINDOWS = ["current_quarter", "last_quarter", "last_6_months", "ytd", "all_time"]
LEAD_QUALITY = ("Junk", "In Progress", "Wrong Fit", "Junk Rate", "Junk rate",
                "Top Campaign", "Top campaign", "Top Keyword", "Top keyword")
#: A figure no legitimate rendering produces.
SENTINEL = 7919


def _run(script, *, js=None):
    from tests.test_pr_ads_161b_marketing_outcome_cohorts import _run_app
    return _run_app(script, js=js)


def _section(page_id):
    m = re.search(r'<section id="%s".*?</section>' % re.escape(page_id), HTML, re.DOTALL)
    assert m, page_id
    return m.group(0)


# ── fixtures: mart country-view payloads, in the mart's own shape ────────────
def _metric(value, status="published", reason=None, **extra):
    return {"value": value if status == "published" else None, "status": status,
            "reason": reason, **extra}


_PERIOD = {"cohort_basis": "close_date_outcomes_over_same_window_spend",
           "ratio_kind": "non_cohort_operational_ratio"}


def _market(name, code, region, spend, *, leads=1, sqls=0, deals=1, revenue=1000.0,
            spend_reason=None, verdict="winner"):
    ratio = (round(revenue / spend, 2) if spend else None)
    return {
        "country": name, "country_code": code, "region": region, "is_residual": False,
        "spend": spend, "spend_reason": spend_reason, "leads": leads, "sqls": sqls,
        "closed_won_deals": deals, "customers": None, "won_revenue": revenue,
        "period_revenue_per_ad_spend": ratio, "verdict": verdict,
        "deal_ids": [f"d-{code}"] if deals is not None else None,
        "metrics": {
            "closed_won_deals": _metric(deals), "revenue_usd": _metric(revenue),
            "customers": _metric(None, "withheld", "customer_identity_unresolved"),
            "period_revenue_per_ad_spend": (
                _metric(ratio, **_PERIOD) if ratio is not None
                else _metric(None, "unavailable", spend_reason or "no_spend_in_window")),
        },
    }


def _residual(spend=15.0, items=True):
    its = [
        {"country": "Cuba", "country_code": "CU", "spend": 2.5, "provenance": "owner_labelled",
         "leads": "in_residual", "sqls": "in_residual", "closed_won_deals": "in_residual",
         "won_revenue": "in_residual", "breakdown_of": "spend_components.outside_listed_markets"},
        {"country": "Fiji", "country_code": "FJ", "spend": 8.75, "provenance": "google_ads",
         "leads": "in_residual", "sqls": "in_residual", "closed_won_deals": "in_residual",
         "won_revenue": "in_residual", "breakdown_of": "spend_components.outside_listed_markets"},
    ] if items else []
    return {
        "country": "Unknown / Unattributed country", "country_code": None, "is_residual": True,
        "region": None, "spend": spend,
        "spend_components": {"outside_listed_markets": 11.25, "country_unidentified": 3.75,
                             "geo_unassigned": 0.0},
        "outside_listed_market_countries": ["Cuba", "Fiji"],
        "outside_listed_market_items": its, "leads": 1, "sqls": 0,
        "closed_won_deals": 1, "customers": None, "won_revenue": 1200.0,
        "period_revenue_per_ad_spend": None, "verdict": "unattributed", "deal_ids": ["d-x"],
        "metrics": {
            "closed_won_deals": _metric(1), "revenue_usd": _metric(1200.0),
            "period_revenue_per_ad_spend": _metric(None, "unavailable", "residual_not_a_market"),
        },
        "attribution_notes": ["Deals and leads whose country is not one of the listed "
                              "markets, or could not be identified. Shown separately so "
                              "totals reconcile; never spread across real countries."],
    }


def _mart(status="verified", *, rows=None, totals=None, gaps=(), primary=None, message=None):
    rows = rows if rows is not None else [
        _market("United Kingdom", "GB", "Europe", 250.0, deals=2, revenue=6000.0),
        _market("Pakistan", "PK", "APAC", None, revenue=900.0,
                spend_reason="spend_location_unresolved", verdict="watch"),
        _residual(),
    ]
    totals = totals if totals is not None else {
        "spend": None, "leads": 3, "sqls": 0, "closed_won_deals": 3, "won_revenue": 6900.0,
        "period_revenue_per_ad_spend": None, "markets": 2, "population": "listed_markets",
        "revenue_scope": "google_ads_source",
        "metrics": {
            "spend": _metric(None, "unavailable", "spend_location_unresolved"),
            "leads": _metric(3), "sqls": _metric(0), "closed_won_deals": _metric(3),
            "revenue_usd": _metric(6900.0),
            "period_revenue_per_ad_spend": _metric(None, "unavailable",
                                                   "spend_location_unresolved"),
        },
    }
    return {
        "view": "country",
        "window": {"key": "last_6_months", "label": "Last 6 Months",
                   "window_range": {"empty": False}},
        "readiness": {"revenue_decision_ready": True},
        "spend_truth": {"country_spend_status": status, "campaign_spend_status": "verified",
                        "fx_status": "verified", "native_currency": "GBP",
                        "country_gap_codes": list(gaps), "country_gap_primary": primary,
                        "country_gap_message": message},
        "rows": rows, "country_totals": totals,
        "regional_mix": [
            {"region": "Europe", "countries": 1, "spend": 250.0, "sqls": 0,
             "closed_won_deals": 2, "won_revenue": 6000.0,
             "metrics": {"spend": _metric(250.0), "sqls": _metric(0),
                         "closed_won_deals": _metric(2), "revenue_usd": _metric(6000.0)}},
            {"region": "APAC", "countries": 1, "spend": None, "sqls": 0,
             "closed_won_deals": 1, "won_revenue": 900.0,
             "metrics": {"spend": _metric(None, "unavailable", "spend_location_unresolved"),
                         "sqls": _metric(0), "closed_won_deals": _metric(1),
                         "revenue_usd": _metric(900.0)}},
        ],
    }


def _page(mart, *, user="admin", js=None):
    """The real renderRoasCountriesPage over ``mart``; the page body's HTML."""
    return _run(
        '_currentUser = {"username": "u", "role": "%s"}; '
        "roasCountryMart = %s; roasCountriesData = roasCountryMart.rows || []; "
        "renderRoasCountriesPage(); "
        '__els["roas-countries-table-body"].innerHTML' % (user, json.dumps(mart)), js=js)


def _cells(html, row_marker):
    """The <td> contents of the first <tr> containing ``row_marker``."""
    i = html.index(row_marker)
    tr = html[html.rfind("<tr", 0, i):html.index("</tr>", i)]
    return re.findall(r"<td[^>]*>(.*?)</td>", tr, re.DOTALL)


# ── 1–3. one page; the old page and its bookmark ─────────────────────────────
def test_01_the_old_geo_bookmark_lands_on_countries():
    out = _run(
        "({redirect: RETIRED_PAGE_REDIRECTS.geo, geoIsPage: PAGES.includes('geo'),"
        " countriesIsPage: PAGES.includes('roas-countries'),"
        " item: navItemForPage('geo'), alias: HASH_ROUTE_ALIASES.countries,"
        " aliasItem: navItemForPage('countries'),"
        " related: datasetRelatedPage('google_ads_api', 'canonical_geo')})")
    assert out["redirect"] == {"page": "roas-countries"}
    assert out["geoIsPage"] is False and out["countriesIsPage"] is True
    assert out["item"] == "countries" and out["aliasItem"] == "countries"
    assert out["alias"] == "roas-countries"
    assert out["related"] == {"page": "roas-countries", "label": "Countries"}


def test_02_countries_is_one_page_with_no_tab_strip():
    out = _run("NAV_ITEMS.find((it) => it.id === 'countries')")
    assert out["pages"] == ["roas-countries"]
    nav = re.search(r'<a class="nav-item"[^>]*data-nav-item="countries"[^>]*>', HTML).group(0)
    assert 'data-page="roas-countries"' in nav
    assert 'data-section="countries"' not in HTML, "a single-page item has no strip"
    assert 'data-page="geo"' not in HTML and 'href="#/geo"' not in HTML
    sec = _section("page-roas-countries")
    assert '<h1 class="page-title">Countries</h1>' in sec


def test_03_the_geo_page_is_deleted_whole():
    assert 'id="page-geo"' not in HTML
    for el in ("geo-map", "geo-kpi-grid", "geo-search", "geo-spend-notice",
               "geo-owner-label-notice", "geo-table-body", "geo-map-metric"):
        assert f'id="{el}"' not in HTML, el
    for fn in ("loadGeo", "renderGeoMap", "renderGeoTable", "applyGeoSearch",
               "geoSpendVerdict", "geoSpendCell", "geoLeadCell", "geoTopText",
               "geoFmtSpend", "ownerZeroNote", "junkRateBadge"):
        assert not re.search(r"\bfunction %s\(" % fn, JS), fn
    assert "/api/geo" not in JS
    assert "choropleth" not in JS and "Plotly" not in JS
    for cls in (".geo-map", ".geo-search-input", ".geo-metric-select", ".junk-rate-badge",
                ".geo-empty-state"):
        assert cls not in CSS, cls
    keys = _run("[PAGE_DATASET_MAP, PAGE_EXPLANATIONS, PAGE_HELP_CONTENT, PAGE_DEPENDENCIES]"
                ".map((m) => Object.keys(m).includes('geo'))")
    assert keys == [False, False, False, False]


# ── 4–5. owner decisions 3 and 1 ─────────────────────────────────────────────
def test_04_no_lead_quality_column_on_the_page():
    """Decision 3: Junk, In Progress, Wrong Fit, Junk rate, top campaign and
    top keyword are dropped — the Leads page owns lead quality."""
    html = _page(_mart())
    head = re.search(r"<thead>(.*?)</thead>", html, re.DOTALL).group(1)
    cols = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<th[^>]*>(.*?)</th>", head)]
    assert cols == ["Country", "Region", "Spend", "Leads", "SQLs", "Won Deals", "Customers",
                    "Won Revenue", "Period Revenue / Ad Spend", "Decision"]
    for word in LEAD_QUALITY:
        assert f">{word}<" not in html, word
    # The deleted page's own lead-quality surfaces, anywhere in static/.
    for gone in ("<th>Top Keyword</th>", "Leads as of", "High Junk Countries",
                 "Junk Rate %"):
        assert gone not in JS and gone not in HTML, gone


def test_05_business_windows_only():
    """Decision 1: Current Quarter (default), Last Quarter, Last 6 Months, YTD,
    All Time. The 7/14/30/60/180-day evidence windows are gone from Countries."""
    sec = _section("page-roas-countries")
    opts = re.findall(r'<option value="([^"]+)"( selected)?', sec)
    assert [v for v, _ in opts] == BUSINESS_WINDOWS
    assert [v for v, sel in opts if sel] == ["current_quarter"]
    assert "data-business-window-select" in sec
    out = _run("(() => { const pages = NAV_ITEMS.find((it) => it.id === 'countries').pages;"
               " return {evidence: pages.filter((p) => EVIDENCE_PAGES.includes(p)),"
               " revenue: pages.every((p) => REVENUE_PAGES.includes(p)),"
               " dflt: DEFAULT_BUSINESS_WINDOW}; })()")
    assert out == {"evidence": [], "revenue": True, "dflt": "current_quarter"}


# ── 6. owner decision 2: the strict spend gate ───────────────────────────────
_UNREADY = [
    ("mismatch", (), None, None),
    ("unavailable", ("totals_differ",), "totals_differ", "the totals differ"),
    ("unavailable", ("spend_not_reread_since_day_ended",), "spend_not_reread_since_day_ended",
     "yesterday was last synced while it was still in progress"),
    # FX only: the mart still sends native spend on rows (docs/58 §2.1). A
    # number with a warning is exactly what decision 2 forbids.
    ("unavailable", ("fx_coverage_incomplete",), "fx_coverage_incomplete", "FX is missing"),
]


def test_06_an_unreconciled_window_shows_the_card_never_the_number():
    """Decision 2. Rows carrying a sentinel spend — as a faulty or FX-only
    payload might — under every non-ready status render the gap-driven
    blocked card (cause, next step, runs: PR-ADS-176A) and no table, no KPI
    and no spend figure. Mutation: without the page's gate check the sentinel
    is printed."""
    rows = [_market("United Kingdom", "GB", "Europe", float(SENTINEL)), _residual()]
    for status, gaps, primary, message in _UNREADY:
        html = _page(_mart(status, rows=rows, gaps=gaps, primary=primary, message=message))
        assert "revenue-blocked-card" in html, status
        assert "Next step:" in html
        assert "ctry-table" not in html and "data-kpi=" not in html, status
        assert "7.9k" not in html and str(SENTINEL) not in html, status
    ready = _page(_mart("verified", rows=rows))
    assert "$7.9k" in ready and "ctry-table" in ready

    gate = 'if (st.country_spend_status !== "verified" && !residualMode) {'
    assert JS.count(gate) == 1
    mutated = JS.replace(gate, "if (false) {")
    leaked = _page(_mart("mismatch", rows=rows), js=mutated)
    assert "$7.9k" in leaked, "the guard must be the gate check, not something else"


# ── 7–9. withheld wording, the residual's items, totals from the server ─────
def test_07_every_withheld_figure_names_its_reason_never_a_dash_or_zero():
    html = _page(_mart())
    kpi = re.search(r'data-kpi="spend".*?dash-kpi-card__source', html, re.DOTALL).group(0)
    assert "Unavailable" in kpi and "some spend in this window has no country" in kpi
    ratio = re.search(r'data-kpi="period_revenue_per_ad_spend".*?dash-kpi-card__source',
                      html, re.DOTALL).group(0)
    assert "Unavailable" in ratio and "some spend in this window has no country" in ratio
    pk = _cells(html, 'aria-label="Open Pakistan detail"')
    assert "Unavailable — some spend in this window has no country" in pk[2]
    res = _cells(html, "revenue-residual-name")
    assert "Unavailable" in res[8] and "the residual is not a market" in res[8]
    for cells in (pk, res):
        for c in cells:
            text = re.sub(r"<[^>]+>", "", c).strip()
            assert text not in ("—", "-", "$0", "0x", "0.00x"), (cells, text)
    # The Regional Mix's APAC spend is unknown: said, with the reason, no bar.
    apac = html[html.index(">APAC<"):html.index("</li>", html.index(">APAC<"))]
    assert "Unavailable" in apac and "some spend in this window has no country" in apac


def test_08_outside_market_countries_are_in_residual_sub_rows():
    """PR-ADS-176A itemised the residual's outside-market spend; the page
    shows each country under the residual, labelled In residual, with its
    outcomes "In residual" — never a number, never a market row."""
    html = _page(_mart())
    body = html[html.index("<tbody>"):html.index("</tbody>")]
    order = sorted(("revenue-residual-name", "</span>Cuba", "</span>Fiji"), key=body.find)
    assert [body.find(m) > -1 for m in order] == [True] * 3
    assert order == ["revenue-residual-name", "</span>Cuba", "</span>Fiji"]
    for name, spend in (("</span>Cuba", "$3"), ("</span>Fiji", "$9")):
        cells = _cells(html, name)
        assert "In residual" in cells[0]
        assert cells[2].strip() == spend
        for c in cells[3:9]:
            assert re.sub(r"<[^>]+>", "", c).strip() == "In residual", (name, c)
        assert 'data-country-row' not in body[body.rfind("<tr", 0, body.index(name)):
                                              body.index(name)]
    assert "set by owner" in _cells(html, "</span>Cuba")[0]


def test_09_the_page_sums_nothing_its_totals_are_the_servers():
    """The KPI cards print `country_totals`; the page never adds rows up (the
    client-side `summarizeRoasCountryRows` is gone). With rows that sum to one
    figure and server totals saying another, the server's is printed."""
    assert "function summarizeRoasCountryRows" not in JS
    totals = {"spend": float(SENTINEL), "leads": 1, "sqls": 0, "closed_won_deals": 41,
              "won_revenue": 123456.0, "period_revenue_per_ad_spend": 15.59, "markets": 1,
              "metrics": {"spend": _metric(float(SENTINEL)), "leads": _metric(1),
                          "sqls": _metric(0), "closed_won_deals": _metric(41),
                          "revenue_usd": _metric(123456.0),
                          "period_revenue_per_ad_spend": _metric(15.59, **_PERIOD)}}
    html = _page(_mart(rows=[_market("United Kingdom", "GB", "Europe", 250.0)],
                       totals=totals))
    cards = dict(re.findall(r'data-kpi="([^"]+)".*?dash-kpi-card__value">(.*?)</div>',
                            html, re.DOTALL))
    assert cards["spend"] == "$7.9k" and cards["deals"] == "41"
    assert cards["revenue"] == "$123.5k" and "15.59x" in cards["period_revenue_per_ad_spend"]
    assert "Customers" not in re.findall(r'dash-kpi-card__label">(.*?)<', html)


# ── 10–11. the drawer, and a sub-dollar spend ───────────────────────────────
def test_10_a_market_row_opens_a_drawer_the_residual_never_does():
    html = _page(_mart())
    assert html.count("data-country-row=") == 2          # UK, Pakistan
    res_tr = html[html.rfind("<tr", 0, html.index("revenue-residual-name")):
                  html.index("revenue-residual-name")]
    assert "data-country-row" not in res_tr
    row = _market("Pakistan", "PK", "APAC", None, deals=None, revenue=None,
                  spend_reason="spend_location_unresolved")
    drawer = _run("renderCountryDrawer(%s)" % json.dumps(row))
    assert "Closed-won deals are withheld for this country in this window." in drawer
    assert "No attributed closed-won" not in drawer
    assert "some spend in this window has no country" in drawer


def test_11_a_spend_under_a_dollar_is_not_printed_as_zero():
    assert _run("[fmtCountrySpend(0.39), fmtCountrySpend(0), fmtCountrySpend(null),"
                " fmtCountrySpend(1234)]") == ["<$1", "$0", "Unavailable", "$1.2k"]
    row = _market("Jordan", "JO", "Middle East", 0.39)
    assert _cells(_run("renderRoasCountryTable([%s])" % json.dumps(row)), ">Jordan<")[2] == "&lt;$1"


# ── 12. the totals' window gate (the mart, pure) ─────────────────────────────
def test_12_a_window_that_withholds_a_family_publishes_no_country_total_of_it():
    """`country_totals` is a subset of the window: when the window's canonical
    verdict withholds the deal count, no country total of it publishes — not
    even a sum over no rows, which would read as a measured 0. Spend, which
    is not a closed-won family, still publishes."""
    import sys
    sys.path.insert(0, str(ROOT))
    from services.revenue_decision_mart import _country_page_blocks
    st = {"country_spend_status": "verified", "country_gap_codes": []}
    withheld = {"closed_won_deals": _metric(None, "withheld", "won_deals_without_close_date"),
                "revenue_usd": _metric(None, "withheld", "won_deals_without_close_date")}
    for rows in ([], [{"country": "United Kingdom", "country_code": "GB", "is_residual": False,
                       "spend": 10.0, "leads": 1, "sqls": 0, "closed_won_deals": 0,
                       "won_revenue": 0.0, "metrics": None}]):
        _, t, mix = _country_page_blocks(rows, st, window_metrics=withheld)
        assert t["closed_won_deals"] is None and t["won_revenue"] is None
        assert t["metrics"]["closed_won_deals"]["status"] == "withheld"
        assert t["metrics"]["revenue_usd"]["reason"] == "won_deals_without_close_date"
        assert t["metrics"]["period_revenue_per_ad_spend"]["status"] == "withheld"
        assert t["spend"] == (10.0 if rows else 0)
        assert all(r["closed_won_deals"] is None for r in mix)
    published = {"closed_won_deals": _metric(5), "revenue_usd": _metric(10.0)}
    _, t, _ = _country_page_blocks([], st, window_metrics=published)
    assert (t["closed_won_deals"], t["won_revenue"]) == (0, 0)


# ── 13. what the deleted page guarded, on the page that is left ─────────────
def test_13_the_deleted_page_s_row_rules_hold_on_countries():
    """Ported from the #/geo page's cases (165A 13/18, 170A 13/18/24): an
    unknown lead count reads "Unavailable", never "—" or 0; an owner-labelled
    market says so beside its name; and a stored Google code that differs from
    the owner's label is a notice on the page."""
    row = _market("Cuba", "CU", "LATAM", 12.0)
    row.update({"leads": None, "sqls": None, "spend_country_provenance": "owner_labelled"})
    mart = _mart(rows=[row, _residual(items=False)])
    mart["spend_truth"]["owner_labelled_geo"] = {"mismatches": [
        {"criterion_id": "2192", "owner_code": "CU", "stored_codes": ["MX"]}]}
    html = _page(mart)
    cells = _cells(html, 'aria-label="Open Cuba detail"')
    assert "set by owner" in cells[0]
    assert re.sub(r"<[^>]+>", "", cells[3]).strip() == "Unavailable"      # leads
    assert re.sub(r"<[^>]+>", "", cells[4]).strip() == "Unavailable"      # SQLs
    assert "Owner country label not applied" in html
    assert "location 2192: the owner set CU, Google Ads reports MX" in html
