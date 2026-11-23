# 57 — The six-item menu (PR-ADS-174A)

The owner approved a six-item menu: Overview, Channels, Campaigns, Countries,
Pipeline, System. It ships before the merged pages behind it exist. This PR
changes **navigation only**. No page content, loader, service, API, window
selector or figure changes, and no page id is renamed or removed. The owner
treats the current Dashboard pages as the design standard, so the menu reuses
their look and adds no new visual language.

## 1. The menu

| # | Item | Pages, in order (the first is the item's default) |
| --- | --- | --- |
| 1 | Overview | `dashboard` (the label was "Dashboard"; the page id is unchanged) |
| 2 | Channels | `channels` |
| 3 | Campaigns | `campaigns`, `roas-campaigns` (Campaign Revenue & Spend), `keywords` (Keyword Evidence), `search-terms` |
| 4 | Countries | `geo` (Countries), `roas-countries` (Country Revenue & Spend) |
| 5 | Pipeline | `leads`, `deals` |
| 6 | System | `system`, `scheduler` (Data Runs), `health` (System Status), `revenue-health`, `backfill` (Admin Backfill), `gclid-attribution` |

The table lives in one place, `NAV_ITEMS` in `static/app.js`.
`navItemForPage(page)` returns the item a page belongs to. A retired key
(`RETIRED_PAGE_REDIRECTS`) or a hash alias (`HASH_ROUTE_ALIASES`) resolves to
its destination first, so `#/revenue-by-source` highlights Channels,
`#/waste` and `#/ngrams` highlight Campaigns, and `#/opportunities`
highlights Pipeline.

## 2. The sub-page pattern: a tab strip at the top of the page

An item with more than one page shows its pages as a tab strip at the top of
the page (`#section-tabs-bar`, one `<nav class="section-tabs"
data-section="…">` per item). Sub-entries in the sidebar were the other
option. The strip was chosen for three reasons:

* **It is the Dashboard's own pattern.** Each tab is a `.dashboard-tab`, the
  same pill the Dashboard's inner tabs use, so no new style is introduced.
* **The sidebar stays six items**, which is what the mock-up shows.
* **It works at phone width.** At 768px and below, the sidebar collapses to
  a 56px icon rail with the labels hidden. Sub-entries there would be a column
  of unlabelled icons. The strip keeps its labels and wraps
  (`flex-wrap: wrap`) instead of scrolling sideways.

The strip is the only sub-page pattern, and every multi-page item uses it.
Single-page items (Overview, Channels) have no strip. A strip with fewer than
two pages the viewer may open is not shown.

The Dashboard's inner tabs (Overview, Revenue, Channels, …) are page content
and are unchanged. Overview is a single-page item, so the Dashboard never
shows two strips.

## 3. Highlighting

`navigate()` calls `renderNavActive(page)` after the redirect, the unknown-page
fallback and every admin check have run, so it highlights the page that
actually loads:

* the item for that page gets `.active` and `aria-current="page"`;
* that item's strip is shown, and the page's own tab gets `.is-active` (the
  Dashboard tab active style) and `aria-current="page"`.

## 4. Admin gating is unchanged

Before this PR, `showApp()` hid four sidebar entries from non-admins by id:
`nav-system-item`, `nav-health-item`, `nav-revenue-health-item` and
`nav-backfill-item`. Those ids are now on the matching System tabs, and
`showApp()` hides them with the same four lines. `navigate()` still refuses
`system`, `health`, `backfill` and `revenue-health` to a non-admin, using the
same checks.

`applyNavGating()` (called from `showApp()`) reads those tabs back. It does not
decide access. Each item opens the first page the viewer may see, and an item
with no such page is hidden, so no viewer sees an empty group.

Before this PR, a non-admin could open Data Runs and GCLID Attribution under
the "Advanced tools" toggle, and neither was ever admin-gated. That is
unchanged: for a non-admin, System opens Data Runs, and its strip shows Data
Runs and GCLID Attribution.

The "Advanced tools" fold (`ADVANCED_ADMIN_PAGES`, `setAdvancedNavOpen`,
`#nav-advanced-toggle`, `.nav-advanced`) is deleted. It only existed to shorten
the sidebar, and the System strip lists those pages without it. The muted
`.nav-item--admin` style went with it: all six items now look the same.

## 5. Keyboard and phone width

* The six items keep `role="button"`, `tabindex="0"`, an `aria-label` (the
  only accessible name once the 768px rail hides the label) and the existing
  Enter/Space handler. `.nav-item:focus-visible` draws the outline.
* The tabs are real links (`href="#/<page>"`, the router's own hash), so Enter
  activates them natively. The click is routed through `navigate()`.
  `.section-tab:focus-visible` draws the same outline.
* The six icons fit the 56px rail. The strip wraps rather than scrolling
  horizontally.

## 6. What did not change

* `PAGES`, `REVENUE_PAGES`, `RETIRED_PAGE_REDIRECTS`, `HASH_ROUTE_ALIASES`,
  `loadPage()`, the refresh wiring and every window selector.
* Every page section, including the Dashboard's `<h1>Dashboard</h1>` page
  title. That title is page content; only the menu label changed.
* Every `target_page` a Dashboard service emits. Each one resolves to a page in
  the menu (test 07).

## 7. Tests

`tests/test_pr_ads_174a_six_item_menu.py` has 17 cases. The menu table, the
lookup and the gating code are taken from `static/app.js` and run in `node`
against a minimal stand-in DOM built from the real `index.html`. Case 09 shows
the lookup guard failing on a deliberately broken table.

Some existing tests pinned the old menu shape (groups, group labels, the
Advanced fold, the 16-entry sidebar). Only those were updated, and none of the
updates touches page content:

| Test | Old expectation | New expectation |
| --- | --- | --- |
| `162f::test_01` | four groups: Overview / Google Ads / Sales Results / Admin and their pages | six items and their pages (§1) |
| `162f::test_04` | admin ids on sidebar `<li>`s; `#nav-advanced-toggle`; five `.nav-advanced` entries | the same four ids on the System tabs and still looked up by `showApp()`; no fold |
| `162c::test_38` | the five technical pages folded under Advanced (`ADVANCED_ADMIN_PAGES`, collapsed CSS, toggle, auto-open) | the five technical pages after System in the System strip; fold gone; `.section-tab[hidden]` hides |
| `162g::test_09` | 16 distinct `nav-item` entries; the Google Ads group is campaigns, search-terms, keywords, geo | 16 distinct pages across items and tabs; the Campaigns strip is campaigns, roas-campaigns, keywords, search-terms |
| `165a::test_19` | Sales Results holds leads, deals, roas-campaigns, roas-countries | Pipeline holds leads and deals; each Revenue & Spend page sits in its item's strip |
| `sidebar_navigation_structure::test_sidebar_groups_exist` | labels Overview, Google Ads, Sales Results, Admin | the six item labels, in order |
| `sidebar_navigation_structure::test_revenue_attribution_section_exists` | `>Sales Results</li>` | the Revenue & Spend pages are in the Campaigns and Countries strips |
| `sidebar_navigation_structure::test_section_labels_are_non_clickable` | the sidebar uses `sidebar__section-label` | no section labels; six `<li>`s, six links |
| `153c::test_leads_nav_item_exists_under_crm_and_revenue` | Leads under `>Sales Results</li>` | Leads is the Pipeline item's first page and in its strip |
| `153d::test_search_terms_stays_under_platform_evidence` | the four pages between the Google Ads and Sales Results comments | the Campaigns strip holds search-terms, campaigns, keywords; geo is the Countries item |
| `153d::test_gclid_attribution_moves_to_admin` | GCLID after `>Admin</li>`, not in Sales Results | GCLID in the System strip, not in the Pipeline strip |

## 8. Not verified

Browser rendering was not checked. Nothing here was opened in a real browser,
so the strip's spacing above each page title, wrapping at 390px, and the
appearance of the focus rings are covered only by structural assertions
against the CSS.
