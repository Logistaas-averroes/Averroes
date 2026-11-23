"""PR-ADS-174A — the six-item menu.

The owner approved a six-item menu: Overview, Channels, Campaigns, Countries,
Pipeline, System. This PR changes navigation only. No page, loader, service,
API or figure changes, and no page id changes:

* the sidebar holds exactly six items, in that order;
* an item with more than one page shows its pages as a tab strip at the top of
  the page, in the Dashboard tab style (`.dashboard-tab`). The strip is the
  only sub-page pattern;
* every page id in `PAGES` belongs to exactly one item. A retired key belongs
  to the item of the page it redirects to;
* admin gating is unchanged: showApp() hides the same four admin-only entries
  by the same ids. Those ids are now on tabs, and an item opens the first page
  the viewer may see.

There is no JS test harness. The menu table, the page-to-item lookup and the
gating code are taken from `static/app.js` and run in `node` (the binary CI
already uses for `node --check`) against a minimal stand-in DOM. That stand-in
is built from the real `static/index.html`.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "static/index.html").read_text(encoding="utf-8")
JS = (ROOT / "static/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/styles.css").read_text(encoding="utf-8")

EXPECTED_ITEMS = [
    ("overview", "Overview", ["dashboard"]),
    ("channels", "Channels", ["channels"]),
    ("campaigns", "Campaigns", ["campaigns", "roas-campaigns", "keywords", "search-terms"]),
    # PR-ADS-176B: Countries is one page (#/geo is retired into it), so it is
    # a single-page item with no strip.
    ("countries", "Countries", ["roas-countries"]),
    ("pipeline", "Pipeline", ["leads", "deals"]),
    ("system", "System", ["system", "scheduler", "health", "revenue-health",
                          "backfill", "gclid-attribution"]),
]

# Every key navigate() accepts, and the item it must highlight. Typed out here
# rather than derived from NAV_ITEMS, so a wrong table cannot agree with itself.
EXPECTED_ITEM_FOR_PAGE = {
    "dashboard": "overview",
    "channels": "channels",
    "campaigns": "campaigns", "roas-campaigns": "campaigns",
    "keywords": "campaigns", "search-terms": "campaigns",
    # PR-ADS-176B: "geo" is a retired key now (RETIRED_PAGE_REDIRECTS).
    "geo": "countries", "roas-countries": "countries",
    "leads": "pipeline", "deals": "pipeline",
    "system": "system", "scheduler": "system", "health": "system",
    "revenue-health": "system", "backfill": "system", "gclid-attribution": "system",
    # retired keys (RETIRED_PAGE_REDIRECTS) and hash aliases (HASH_ROUTE_ALIASES)
    "waste": "campaigns", "opportunities": "pipeline", "revenue-by-source": "channels",
    "ngrams": "campaigns", "countries": "countries", "data-runs": "system",
    "system-status": "system",
}

ADMIN_IDS = {
    "nav-system-item": "system",
    "nav-health-item": "health",
    "nav-revenue-health-item": "revenue-health",
    "nav-backfill-item": "backfill",
}


# ── helpers ───────────────────────────────────────────────────────────────


def _node(script: str) -> str:
    node = shutil.which("node")
    assert node, "node is required: CI runs `node --check static/app.js`"
    out = subprocess.run([node, "-e", script], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _block(start: str, end: str) -> str:
    i = JS.index(start)
    return JS[i:JS.index(end, i) + len(end)]


def _fn(signature: str) -> str:
    return _block(signature, "\n}\n")


def _pages() -> list:
    return re.findall(r'"([a-z0-9-]+)"', re.search(
        r"const PAGES = \[(.*?)\];", JS, re.S).group(1))


def _menu_js() -> str:
    """The real menu table, redirect map, aliases and lookup."""
    return "\n".join([
        _block("const RETIRED_PAGE_REDIRECTS = {", "\n};"),
        _block("const HASH_ROUTE_ALIASES = {", "\n};"),
        _block("const NAV_ITEMS = [", "\n];"),
        _fn("function navItemForPage(page) {"),
    ])


def _sidebar() -> str:
    start = HTML.index('<ul class="sidebar__nav" role="list">')
    return HTML[start:HTML.index("</ul>", start)]


def _top_items() -> list:
    found = re.findall(
        r'<a class="nav-item" data-page="([a-z0-9-]+)" data-nav-item="([a-z-]+)"'
        r'[^>]*>.*?<span class="nav-item__label">([^<]+)</span>',
        _sidebar(), re.S)
    return [(i, p, l) for p, i, l in found]


def _strips() -> dict:
    bar = HTML[HTML.index('<div id="section-tabs-bar"'):]
    bar = bar[:bar.index("\n      </div>")]
    out = {}
    for section, body in re.findall(
            r'<nav class="dashboard-tabs section-tabs" data-section="([a-z-]+)"'
            r'[^>]*>(.*?)</nav>', bar, re.S):
        out[section] = re.findall(
            r'<a (?:id="([a-z-]+)" )?class="section-tab dashboard-tab" '
            r'data-page="([a-z0-9-]+)" href="([^"]+)"[^>]*>([^<]+)</a>', body)
    return out


def _fake_dom_js() -> str:
    """A stand-in for the few DOM calls the menu code makes, populated from
    the real index.html: the six sidebar links, the strips and their tabs."""
    links = [{"navItem": i, "page": p, "label": l} for i, p, l in _top_items()]
    strips = {s: [{"id": tid, "page": p, "label": l.replace("&amp;", "&")}
                  for tid, p, _, l in tabs] for s, tabs in _strips().items()}
    return f"""
const _LINKS = {json.dumps(links)};
const _STRIPS = {json.dumps(strips)};
function _el(kind, props) {{
  const el = Object.assign({{ kind, hidden: false, attrs: {{}}, classes: new Set() }}, props);
  el.classList = {{
    toggle(c, on) {{ if (on) el.classes.add(c); else el.classes.delete(c); }},
    contains(c) {{ return el.classes.has(c); }},
  }};
  el.setAttribute = (k, v) => {{ el.attrs[k] = v; }};
  el.removeAttribute = (k) => {{ delete el.attrs[k]; }};
  return el;
}}
const _all = [];
_LINKS.forEach((l) => {{
  const li = _el("li", {{}});
  const a = _el("nav-item", {{ dataset: {{ navItem: l.navItem, page: l.page }}, label: l.label, li }});
  a.closest = (sel) => (sel === "li" ? li : null);
  _all.push(a);
}});
Object.entries(_STRIPS).forEach(([section, tabs]) => {{
  _all.push(_el("section-tabs", {{ dataset: {{ section }}, hidden: true }}));
  tabs.forEach((t) => _all.push(_el("section-tab", {{ id: t.id, dataset: {{ page: t.page }}, label: t.label }})));
}});
const _bar = _el("bar", {{ id: "section-tabs-bar", hidden: true }});
function _match(sel) {{
  const m = sel.match(/^\\.([a-z-]+)\\[data-([a-z-]+)(?:="([^"]*)")?\\]$/);
  if (!m) throw new Error("unsupported selector " + sel);
  const key = m[2].replace(/-([a-z])/g, (_, c) => c.toUpperCase());
  return _all.filter((el) => el.kind === m[1] && el.dataset && key in el.dataset
    && (m[3] === undefined || el.dataset[key] === m[3]));
}}
const document = {{
  querySelectorAll: (sel) => _match(sel),
  querySelector: (sel) => _match(sel)[0] || null,
  getElementById: (id) => (id === "section-tabs-bar" ? _bar : _all.find((el) => el.id === id) || null),
}};
"""


def _gating_js() -> str:
    """The real gating code: showApp()'s admin-only hiding lines, verbatim,
    then the menu functions."""
    show = _fn("function showApp(user) {")
    hiding = show[show.index("  // Show/hide System Health nav item"):
                  show.index("  applyNavGating();") + len("  applyNavGating();")]
    return "\n".join([
        _menu_js(),
        "let _currentPage = null;",
        _fn("function navVisiblePages(item) {"),
        _fn("function applyNavGating() {"),
        _fn("function renderNavActive(page) {"),
        "function _showAppGating(user) {\n" + hiding + "\n}",
    ])


def _render_state() -> str:
    return """
function _state() {
  const items = _all.filter((e) => e.kind === "nav-item").map((a) => ({
    item: a.dataset.navItem, opens: a.dataset.page, shown: !a.li.hidden,
    active: a.classes.has("active"), current: a.attrs["aria-current"] || null }));
  const strips = _all.filter((e) => e.kind === "section-tabs").filter((s) => !s.hidden)
    .map((s) => s.dataset.section);
  const tabs = _all.filter((e) => e.kind === "section-tab").filter((t) => !t.hidden)
    .map((t) => t.dataset.page);
  const activeTabs = _all.filter((e) => e.kind === "section-tab" && e.classes.has("is-active"))
    .map((t) => [t.dataset.page, t.attrs["aria-current"] || null]);
  return { items, strips, tabs, activeTabs, bar: !_bar.hidden };
}
"""


# ── 1. the six items ──────────────────────────────────────────────────────


def test_01_the_sidebar_holds_exactly_six_items_in_order_with_their_labels():
    top = _top_items()
    assert [(i, l) for i, _, l in top] == [(i, l) for i, l, _ in EXPECTED_ITEMS]
    # and nothing else in the sidebar is a page link, a group label or a toggle
    assert len(re.findall(r"data-page=", _sidebar())) == 6
    assert "sidebar__section-label" not in _sidebar()
    assert "nav-advanced" not in HTML and "nav-advanced" not in JS
    # every item opens its first page
    assert [p for _, p, _ in top] == [ps[0] for _, _, ps in EXPECTED_ITEMS]


def test_02_the_menu_table_in_app_js_is_the_same_six_items():
    out = json.loads(_node(_menu_js() + "\nprocess.stdout.write(JSON.stringify("
                           "NAV_ITEMS.map((it) => [it.id, it.label, it.pages])));"))
    assert [tuple(x) for x in out] == [(i, l, p) for i, l, p in EXPECTED_ITEMS]


def test_03_dashboard_is_no_longer_a_nav_label_but_stays_the_page_id():
    nav_labels = re.findall(r'<span class="nav-item__label">([^<]+)</span>', _sidebar())
    tab_labels = [l for tabs in _strips().values() for *_, l in tabs]
    assert "Dashboard" not in nav_labels + tab_labels
    assert 'aria-label="Dashboard"' not in HTML
    assert 'data-page="dashboard" data-nav-item="overview"' in _sidebar()
    assert "dashboard" in _pages() and 'id="page-dashboard"' in HTML
    assert 'case "dashboard":' in JS


# ── 2. every page in exactly one item ─────────────────────────────────────


def test_04_every_page_id_is_reachable_from_exactly_one_item():
    pages = _pages()
    in_menu = [p for _, _, ps in EXPECTED_ITEMS for p in ps]
    assert len(in_menu) == len(set(in_menu)), "a page sits in two items"
    redirects = json.loads(_node(_menu_js() + "\nprocess.stdout.write(JSON.stringify("
                                 "{r: RETIRED_PAGE_REDIRECTS, a: HASH_ROUTE_ALIASES}));"))
    for page in pages:
        target = (redirects["r"].get(page) or {}).get("page") or redirects["a"].get(page) or page
        owners = [i for i, _, ps in EXPECTED_ITEMS if target in ps]
        assert len(owners) == 1, (page, target, owners)
    # nothing in the menu is unroutable
    assert set(in_menu) <= set(pages)
    for page in in_menu:
        assert f'id="page-{page}"' in HTML, page
        assert f'case "{page}":' in JS, page


def test_05_each_multi_page_item_has_one_strip_holding_its_pages_in_order():
    strips = _strips()
    multi = {i: ps for i, _, ps in EXPECTED_ITEMS if len(ps) > 1}
    assert {s: [p for _, p, _, _ in tabs] for s, tabs in strips.items()} == multi
    for tabs in strips.values():
        for _, page, href, _ in tabs:
            assert href == f"#/{page}", (page, href)   # pageToHash(page)
    # a page link appears once in the menu, or twice when it is an item's first page
    for i, first, _ in _top_items():
        if i in strips:
            assert strips[i][0][1] == first


def test_06_the_strip_labels_are_the_old_sidebar_labels():
    labels = {p: l.replace("&amp;", "&") for tabs in _strips().values()
              for _, p, _, l in tabs}
    assert labels == {
        "campaigns": "Campaigns", "roas-campaigns": "Campaign Revenue & Spend",
        "keywords": "Keyword Evidence", "search-terms": "Search Terms",
        "leads": "Leads", "deals": "Deals",
        "system": "System", "scheduler": "Data Runs", "health": "System Status",
        "revenue-health": "Revenue Health", "backfill": "Admin Backfill",
        "gclid-attribution": "GCLID Attribution",
    }


def test_07_every_dashboard_target_page_and_redirect_target_is_in_the_menu():
    targets = set()
    for f in sorted((ROOT / "services").glob("*.py")):
        src = f.read_text(encoding="utf-8")
        targets |= set(re.findall(r'"target_page":\s*"([a-z0-9-]+)"', src))
        targets |= set(re.findall(r'target_page="([a-z0-9-]+)"', src))
    assert {"channels", "deals", "leads", "revenue-health", "roas-campaigns",
            "roas-countries", "search-terms"} <= targets
    redirects = json.loads(_node(_menu_js() + "\nprocess.stdout.write(JSON.stringify("
                                 "Object.values(RETIRED_PAGE_REDIRECTS).map((r) => r.page)));"))
    in_menu = {p for _, _, ps in EXPECTED_ITEMS for p in ps}
    for page in targets | set(redirects):
        assert page in in_menu, page


# ── 3. the active item, evaluated ─────────────────────────────────────────


def test_08_the_active_item_for_every_page_is_evaluated_in_node():
    keys = sorted(set(_pages()) | set(EXPECTED_ITEM_FOR_PAGE))
    out = json.loads(_node(_menu_js() + f"\nconst keys = {json.dumps(keys)};\n"
                           "process.stdout.write(JSON.stringify(Object.fromEntries("
                           "keys.map((k) => [k, navItemForPage(k)]))));"))
    assert out == {k: EXPECTED_ITEM_FOR_PAGE[k] for k in keys}
    # an unknown key belongs to no item (navigate() sends it to the Dashboard)
    out = _node(_menu_js() + "\nprocess.stdout.write(String(navItemForPage('nope')));")
    assert out == "null"


def test_09_the_lookup_guard_fails_on_a_broken_table():
    """Shown failing: drop Deals from Pipeline and the lookup loses it."""
    broken = _menu_js().replace('pages: ["leads", "deals"]', 'pages: ["leads"]')
    assert broken != _menu_js()
    out = _node(broken + "\nprocess.stdout.write(String(navItemForPage('deals')));")
    assert out == "null"


def test_10_navigate_highlights_after_redirects_and_the_access_checks():
    nav = _fn("function navigate(page, options) {")
    at = nav.index("  renderNavActive(page);")
    assert nav.index("const retired = RETIRED_PAGE_REDIRECTS[page];") < at
    assert nav.index("if (!PAGES.includes(page) || adminOnlyDenied(page)) {") < at
    assert nav.index('if (page === "revenue-health" && (!_currentUser') < at
    assert 'el.dataset.page === page' not in nav


def test_11_the_highlight_and_strip_for_each_page_in_the_real_code():
    script = _fake_dom_js() + _gating_js() + _render_state() + """
_showAppGating({ role: "admin" });
const out = {};
for (const p of ["dashboard", "channels", "keywords", "roas-countries", "deals", "backfill"]) {
  renderNavActive(p);
  out[p] = _state();
}
process.stdout.write(JSON.stringify(out));
"""
    out = json.loads(_node(script))
    for page, item in (("dashboard", "overview"), ("channels", "channels"),
                       ("keywords", "campaigns"), ("roas-countries", "countries"),
                       ("deals", "pipeline"), ("backfill", "system")):
        state = out[page]
        active = [x["item"] for x in state["items"] if x["active"]]
        assert active == [item], (page, active)
        assert [x["current"] for x in state["items"] if x["active"]] == ["page"]
        assert all(x["current"] is None for x in state["items"] if not x["active"])
        if item in ("overview", "channels", "countries"):   # single-page items (176B)
            assert state["strips"] == [] and state["bar"] is False
            assert state["activeTabs"] == []
        else:
            assert state["strips"] == [item] and state["bar"] is True
            assert state["activeTabs"] == [[page, "page"]]


# ── 4. admin gating ───────────────────────────────────────────────────────


def test_12_admin_entries_are_hidden_by_the_same_checks_on_the_same_ids():
    show = _fn("function showApp(user) {")
    hidden = re.findall(
        r'const (\w+) = document\.getElementById\("(nav-[a-z-]+)"\);\n'
        r'  if \(\1\) \1\.hidden = user\.role !== "admin";', show)
    assert {i for _, i in hidden} == set(ADMIN_IDS)
    tabs = {tid: p for tabs in _strips().values() for tid, p, _, _ in tabs if tid}
    assert tabs == ADMIN_IDS
    system = [p for _, p, _, _ in _strips()["system"]]
    assert set(ADMIN_IDS.values()) <= set(system)
    # no admin id in the sidebar itself: the six items are for everyone
    for tid in ADMIN_IDS:
        assert f'id="{tid}"' not in _sidebar()
    # navigate() still refuses the admin pages to a non-admin, unchanged
    nav = _fn("function navigate(page, options) {")
    for page in ("health", "backfill", "revenue-health"):
        assert (f'if (page === "{page}" && (!_currentUser || '
                f'_currentUser.role !== "admin")) {{') in nav
    assert "if (!PAGES.includes(page) || adminOnlyDenied(page)) {" in nav
    assert ('return page === "system" && '
            '!(_currentUser && _currentUser.role === "admin");') in JS


def test_13_admin_and_non_admin_menus_in_the_real_gating_code():
    script = _fake_dom_js() + _gating_js() + _render_state() + """
const out = {};
_showAppGating({ role: "admin" });
renderNavActive("system");
out.admin = _state();
_all.forEach((e) => { e.hidden = e.kind === "section-tabs"; });
_showAppGating({ role: "viewer" });
renderNavActive("scheduler");
out.viewer = _state();
process.stdout.write(JSON.stringify(out));
"""
    out = json.loads(_node(script))
    admin, viewer = out["admin"], out["viewer"]
    assert [(x["item"], x["opens"], x["shown"]) for x in admin["items"]] == [
        ("overview", "dashboard", True), ("channels", "channels", True),
        ("campaigns", "campaigns", True), ("countries", "roas-countries", True),
        ("pipeline", "leads", True), ("system", "system", True)]
    assert admin["strips"] == ["system"]
    assert [t for t in admin["tabs"] if t in EXPECTED_ITEMS[-1][2]] == EXPECTED_ITEMS[-1][2]
    # a non-admin keeps exactly what they could open before: Data Runs and GCLID
    # Attribution, never System, System Status, Revenue Health or Admin Backfill
    assert [(x["item"], x["opens"], x["shown"]) for x in viewer["items"]][-1] == (
        "system", "scheduler", True)
    assert [t for t in viewer["tabs"] if t in EXPECTED_ITEMS[-1][2]] == [
        "scheduler", "gclid-attribution"]
    assert viewer["strips"] == ["system"] and viewer["bar"] is True
    for page in ADMIN_IDS.values():
        assert page not in viewer["tabs"]


def test_14_an_item_with_no_page_the_viewer_may_open_is_not_shown():
    """No empty group: hide every System tab and the System item goes."""
    script = _fake_dom_js() + _gating_js() + _render_state() + """
_all.filter((e) => e.kind === "section-tab" && ["scheduler", "gclid-attribution"]
  .includes(e.dataset.page)).forEach((e) => { e.hidden = true; });
_showAppGating({ role: "viewer" });
renderNavActive("dashboard");
process.stdout.write(JSON.stringify(_state()));
"""
    state = json.loads(_node(script))
    assert [(x["item"], x["shown"]) for x in state["items"]][-1] == ("system", False)
    assert all(x["shown"] for x in state["items"][:-1])
    # and a strip with one visible page is not shown either
    script = _fake_dom_js() + _gating_js() + _render_state() + """
_all.filter((e) => e.kind === "section-tab" && e.dataset.page === "gclid-attribution")
  .forEach((e) => { e.hidden = true; });
_showAppGating({ role: "viewer" });
renderNavActive("scheduler");
process.stdout.write(JSON.stringify(_state()));
"""
    state = json.loads(_node(script))
    assert state["strips"] == [] and state["bar"] is False


# ── 5. keyboard and phone width ───────────────────────────────────────────


def test_15_every_entry_is_focusable_with_a_visible_focus_state():
    for m in re.finditer(r'<a class="nav-item"[^>]*>', _sidebar()):
        tag = m.group(0)
        assert 'tabindex="0"' in tag and 'role="button"' in tag and "aria-label=" in tag, tag
    # the sidebar links answer Enter and Space; the tabs are real links (Enter)
    wiring = JS[JS.index('document.querySelectorAll(".nav-item[data-page]").forEach'):]
    wiring = wiring[:wiring.index("// Hash routing")]
    assert 'e.key === "Enter" || e.key === " "' in wiring
    assert 'document.querySelectorAll(".section-tab[data-page]").forEach' in wiring
    for tabs in _strips().values():
        for _, page, href, _ in tabs:
            assert href.startswith("#/")
    assert re.search(r"\.nav-item:focus-visible \{\s*outline: 2px solid", CSS)
    assert re.search(r"\.section-tab:focus-visible \{\s*outline: 2px solid", CSS)


def test_16_phone_width_the_items_fit_and_the_strip_wraps():
    # the 768px rail keeps one icon per item; the label is hidden, aria-label is not
    assert len(re.findall(r'<span class="nav-item__icon" aria-hidden="true">',
                          _sidebar())) == 6
    rail = CSS[CSS.index("/* ── Responsive — tablet (768px)"):]
    rail = rail[:rail.index("/* ── Responsive — phone")]
    assert ".nav-item__label," in rail
    # the strip wraps rather than scrolling sideways, and [hidden] really hides it
    assert re.search(r"\.section-tabs \{\s*flex-wrap: wrap;\s*overflow-x: visible;", CSS)
    assert re.search(r"\.section-tabs\[hidden\] \{\s*display: none;", CSS)
    assert re.search(r"\.section-tab\[hidden\] \{\s*display: none;", CSS)
    # the strip reuses the Dashboard tab look rather than a new one
    for tabs in _strips().values():
        assert tabs
    assert 'class="section-tab dashboard-tab"' in HTML


def test_17_the_dashboard_inner_tabs_are_untouched():
    assert re.findall(r'class="dashboard-tab(?: is-active)?" data-dash-tab="([a-z]+)"', HTML) == [
        "overview", "revenue", "channels", "campaigns", "countries", "deals"]
    assert 'data-dash-tab="overview" aria-current="page">Overview</button>' in HTML
