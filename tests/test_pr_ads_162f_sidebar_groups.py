"""PR-ADS-162F — the sidebar says what each group holds.

"Command Center", "Platform Evidence" and "CRM & Revenue" told a new reader
nothing. The groups are now:

* **Overview** — the Dashboard and the all-channel funnel;
* **Google Ads** — everything that is about that one ad platform, including
  the Action Queue and Reports, which are built on Google Ads data only;
* **Sales Results** — leads, deals and revenue from HubSpot;
* **Admin** — System, with the technical pages folded under Advanced tools.

No page was added, removed or renamed, and no route key changed: this is
order and labels only.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "static/index.html").read_text(encoding="utf-8")
JS = (ROOT / "static/app.js").read_text(encoding="utf-8")

EXPECTED = [
    ("Overview", ["dashboard", "channels"]),
    ("Google Ads", ["campaigns", "search-terms", "keywords", "geo",
                    "action-queue", "reports"]),
    ("Sales Results", ["leads", "deals", "revenue-by-source", "roas-campaigns",
                       "roas-countries", "unit-economics"]),
    ("Admin", ["system", "scheduler", "gclid-attribution", "health",
               "revenue-health", "backfill"]),
]


def _sidebar() -> str:
    start = HTML.index('<ul class="sidebar__nav" role="list">')
    return HTML[start:HTML.index("</ul>", start)]


def _groups() -> list:
    out = []
    parts = re.split(
        r'<li class="sidebar__section-label"[^>]*>([^<]+)</li>', _sidebar())
    for name, body in zip(parts[1::2], parts[2::2]):
        out.append((name.replace("&amp;", "&"),
                    re.findall(r'data-page="([a-z0-9-]+)"', body)))
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
    assert len(nav) == len(set(nav)) == 20
    assert set(nav) <= pages
    for page in nav:
        assert f'id="page-{page}"' in HTML, page
        assert f'case "{page}":' in JS, page


def test_04_admin_gating_and_the_advanced_group_are_unchanged():
    side = _sidebar()
    for item in ("nav-system-item", "nav-health-item", "nav-revenue-health-item",
                 "nav-backfill-item"):
        assert f'id="{item}"' in side, item
    assert 'id="nav-advanced-toggle"' in side
    advanced = set(re.findall(
        r'<li(?: id="[a-z-]+")? class="nav-advanced">\s*<a class="nav-item[^"]*" '
        r'data-page="([a-z-]+)"', side))
    assert advanced == {"scheduler", "gclid-attribution", "health",
                        "revenue-health", "backfill"}
    # nothing outside Admin carries the admin styling or the folded class
    before_admin = side.split("<!-- Admin -->")[0]
    assert "nav-advanced" not in before_admin
    assert "nav-item--admin" not in before_admin
