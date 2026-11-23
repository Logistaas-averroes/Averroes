"""PR-ADS-162F — the sidebar says what each group holds.

"Command Center", "Platform Evidence" and "CRM & Revenue" told a new reader
nothing. The groups are now:

* **Overview** — the Dashboard and the all-channel funnel;
* **Google Ads** — everything that is about that one ad platform (the Action
  Queue and Reports sat here until both pages were deleted in PR-ADS-162G);
* **Sales Results** — leads, deals and revenue from HubSpot;
* **Admin** — System, with the technical pages folded under Advanced tools.

No page was added, removed or renamed by 162F, and no route key changed: it
was order and labels only. PR-ADS-162G then removed two pages, which is why the
exact list below is 18 long, not 20.

PR-ADS-174A replaced the four labelled groups with the owner's six-item menu
(Overview, Channels, Campaigns, Countries, Pipeline, System). An item with more
than one page shows them as a tab strip at the top of the page. The group
labels are gone from the sidebar; the pages, their ids and the admin gating are
unchanged. The full contract is tests/test_pr_ads_174a_six_item_menu.py.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "static/index.html").read_text(encoding="utf-8")
JS = (ROOT / "static/app.js").read_text(encoding="utf-8")

# PR-ADS-174A: the six menu items and their pages, in order. Until 174A this
# was four labelled groups:
#   Overview [dashboard, channels]; Google Ads [campaigns, search-terms,
#   keywords, geo]; Sales Results [leads, deals, roas-campaigns,
#   roas-countries]; Admin [system, scheduler, gclid-attribution, health,
#   revenue-health, backfill].
EXPECTED = [
    ("Overview", ["dashboard"]),
    ("Channels", ["channels"]),
    ("Campaigns", ["campaigns", "roas-campaigns", "keywords", "search-terms"]),
    ("Countries", ["geo", "roas-countries"]),
    ("Pipeline", ["leads", "deals"]),
    ("System", ["system", "scheduler", "health", "revenue-health", "backfill",
                "gclid-attribution"]),
]


def _sidebar() -> str:
    start = HTML.index('<ul class="sidebar__nav" role="list">')
    return HTML[start:HTML.index("</ul>", start)]


def _strip(item: str) -> list:
    m = re.search(rf'data-section="{item}"[^>]*>(.*?)</nav>', HTML, re.S)
    return re.findall(r'data-page="([a-z0-9-]+)"', m.group(1)) if m else []


def _groups() -> list:
    out = []
    for page, item, label in re.findall(
            r'<a class="nav-item" data-page="([a-z0-9-]+)" data-nav-item="([a-z-]+)"'
            r'[^>]*>.*?<span class="nav-item__label">([^<]+)</span>', _sidebar(), re.S):
        out.append((label, _strip(item) or [page]))
    return out


def test_01_the_groups_their_order_and_their_pages_are_exact():
    assert _groups() == EXPECTED


def test_02_the_old_group_names_are_gone_from_the_screen():
    visible = re.sub(r"<!--.*?-->", "", HTML, flags=re.S)
    for old in ("Command Center", "Platform Evidence", "CRM &amp; Revenue"):
        assert old not in visible, old


def test_03_no_page_was_added_or_lost_and_every_one_still_routes():
    pages = set(re.findall(r'"([a-z0-9-]+)"', re.search(
        r"const PAGES = \[(.*?)\];", JS, re.S).group(1)))
    nav = [p for _, ps in _groups() for p in ps]
    # PR-ADS-165A: 18 until the Unit Economics page was deleted.
    # PR-ADS-168B: 17 until the Revenue by Source page was deleted.
    # PR-ADS-174A: still 16 pages, now under six items.
    assert len(nav) == len(set(nav)) == 16
    assert set(nav) <= pages
    for page in nav:
        assert f'id="page-{page}"' in HTML, page
        assert f'case "{page}":' in JS, page


def test_04_admin_gating_is_unchanged_and_lives_in_the_system_strip():
    # PR-ADS-174A: the four admin ids moved from sidebar <li>s to the System
    # tabs (same ids, same showApp() checks); the Advanced tools fold is gone,
    # because the System strip lists its pages without one.
    side = _sidebar()
    system = HTML[HTML.index('data-section="system"'):]
    system = system[:system.index("</nav>")]
    for item, page in (("nav-system-item", "system"), ("nav-health-item", "health"),
                       ("nav-revenue-health-item", "revenue-health"),
                       ("nav-backfill-item", "backfill")):
        assert f'id="{item}"' not in side, item
        assert re.search(rf'id="{item}" class="section-tab[^"]*" data-page="{page}"',
                         system), item
        assert f'document.getElementById("{item}")' in JS, item
    assert "nav-advanced" not in HTML and "nav-item--admin" not in HTML
