# 56 — Revenue by Source is removed; Channels is the one channel page (PR-ADS-168B)

The owner wants fewer pages. The six-item menu plan merges Revenue by Source
into Channels. PR-ADS-168A (`docs/50`) put closed-won deals, customers and
revenue per channel on Channels through the reader contract (`docs/48`), so
Revenue by Source had become a second page answering the same question with a
different taxonomy (acquisition group → channel → platform, business windows)
beside Channels' (HubSpot original source, evidence windows). This PR deletes
it, following the PR-ADS-162D rule: no half of a deleted page is left behind.

## 1. What was removed

| Layer | Removed |
| --- | --- |
| Sidebar | the `data-page="revenue-by-source"` entry under Sales Results |
| Markup | `<section id="page-revenue-by-source">` (window select, refresh, range chip, body) |
| Router | `"revenue-by-source"` in `PAGES` and `REVENUE_PAGES`, its `loadPage` and business-window `case`s, the refresh/window wiring, `_revReqSeq.bySource` |
| Renderers | `loadRevenueBySource`, `renderRevenueBySourcePage`, `renderRevenueBySourceHealth`, `renderSourceGroupSection`, `renderSourceChannelRows`, `renderSourcePlatformRow`, `sourcePlatformExpandControl`, `renderSourceSpendProofChip`, `sourceStatusLabel`, `sourceGaSqlsChip`, `fmtSourceRate`, `sourceGroupDealCount` |
| Drawer | `sourceDrilldownCache`, `sourceDrilldownKey`, `renderSourceDrilldown`, `renderSourceDrilldownRow`, `renderSourceContactRow`, `loadSourceDrilldown` and its `[data-source-expand]` click handler |
| CSS | the PR-ADS-133/140 `.revenue-source-section`, `.source-group-*`, `.source-taxonomy-table`, `.source-channel-*`, `.source-platform-*`, `.source-status`, `.source-expand-button`, `.source-drilldown-*`, `.source-proof-chip`, `.source-spend-native` block |
| Routes | `GET /api/revenue-by-source`, `GET /api/revenue-performance/source-platform-detail` |
| Services | `source_attribution_service.build_source_platform_detail`, `_source_detail_row`, `_source_contact_row` |
| Repository | `revenue_repository.fetch_source_contact_details` (its only caller was the drawer) and `fetch_source_deal_details` (uncalled since PR-ADS-153E-B; deleted in review round 1) |
| Mart | the `source` view of `build_revenue_decision_mart` (`VALID_VIEWS` is `campaign, country, deal`), its `source_spend_scope` diagnostic, the response key `source_spend_truth`, and the `revenue_by_source` row of `build_revenue_performance_audit` (the Revenue Health parity panel) |

**Kept**, because something else reads it: `build_revenue_by_source` and its
taxonomy helpers (the Dashboard Overview, Revenue, Deals and Channels tabs and
`dashboard_channels_service`), `canonical_contract.SOURCE_REVENUE_BY_SOURCE`
(the provenance name those Dashboard figures carry), the
`revenue_by_source` contract of `sql_truth_audit_service` (a diagnostic of the
google-ads-source scope), and the Source Attribution Backfill job and its
endpoints. The NAME `fetch_source_deal_details` stays in the reader audit's
`READER_ENTRY_POINTS` and in 153E-B's `PROHIBITED_LEGACY_PROVIDERS`, as
`build_unit_economics` did after PR-ADS-165A: a module that brings it back is
caught.

## 2. Convention for the retired routes

Every route this repo has retired (PR-ADS-162B/162D/162E/165A) is deleted and
answers **404**; there is no 410 registry. This PR does the same. A request for
`/api/revenue-performance?view=source` is a **400** — the mart's existing
answer to any unknown view. `docs/API_CONTRACT.md` records both.

## 3. The old bookmark

`#/revenue-by-source` lands on **Channels**. The key is in
`RETIRED_PAGE_REDIRECTS` (`{ page: "channels" }`) and nowhere else in
`static/`: `navigate()` consults that map before its `PAGES` fallback, which
would otherwise send an unknown key to the Dashboard.

## 4. What moved, and what was dropped as redundant

**Moved**

| Item | From | To |
| --- | --- | --- |
| Source Attribution Backfill panel (admin: preview / run, progress, result counts) | Revenue by Source | **Revenue Health** — `#source-backfill-panel` after the lead reconciliation panel, rendered by `loadRevenueHealth`; its poll now stops when the reader leaves Revenue Health and refreshes Revenue Health when the job ends |

**Dropped as redundant** — each has its Channels equivalent:

| Revenue by Source item | Channels equivalent |
| --- | --- |
| Won deals / customers / won revenue per group, channel and platform (`reader_row`) | the Outcomes columns per channel (`build_channel_outcomes`, `reader_row` per original source; customers distinct, never summed) and the "Source not resolved" row |
| Leads, SQLs and SQL rate per bucket (legacy classification) | the Funnel columns per channel — the canonical acquisition cohort (contacts, MQL, SQL, opportunity, customer stage, with rates) |
| Google Ads group spend + Period Revenue / Ad Spend, proof chip, native GBP subline | the **Google Ads (canonical evidence)** line: same-days Google Ads spend, both period ratios with the disclosure, native spend when USD is unavailable |
| "Google Ads-source SQLs" chip (campaign-attributable subset) | the Paid Search funnel row; campaign-level SQLs stay on Campaign Evidence |
| Platform drawer: the deals behind a platform | the **Deals** page, which lists every won deal with its source group (Channels has no per-deal drill — known gap §7.1) |
| Platform drawer: the leads / SQLs behind a platform | the Leads page filtered by acquisition group |
| Spend-truth strip and mart diagnostics | Campaign / Country Revenue & Spend, which still render the same strip and diagnostics. (Revenue Health's Spend Truth panel is a separate forensic audit; its parity panel now compares only the Campaign, Country and Deals pages and says so.) |
| Window selector (business windows) | Channels' evidence windows (30 / 60 / 180 days / all time); business-window source mixes stay on the Dashboard tabs |

## 5. Links retargeted to Channels

Every `target_page: "revenue-by-source"` / "Open Revenue by Source" now reads
`target_page: "channels"` with `target_label` "Open Channels (different
windows and grouping)" (`canonical_contract.CHANNELS_LINK_LABEL`, mirrored as
`CHANNELS_LINK_LABEL` in `static/app.js`). The figures are business windows
grouped by source group and Channels shows evidence windows grouped by HubSpot
original source, so the link says that it does not show the same number. The
links: the Dashboard Overview's
best-source signal, period-change movers and the "no unproven SQL pipelines"
card; the Revenue tab's per-source rows and attribution cards; the Deals tab's
source cards; the Dashboard Channels decision cards; and the Revenue tab's
source breakdown panel, retitled "Closed-won by source group · this window".
The Overview's signal tiles now print the link's `target_label` instead of a
fixed "View evidence".

## 6. Registries

* `scripts/audit_customer_revenue_reader_cutover.py`: the
  `source_attribution_service.py` row stays (it is still a reader — Dashboard
  breakdowns and `build_channel_outcomes`); its surface no longer names the
  deleted route. The page pass drops `revenue_decision_mart_source` (the view
  no longer exists) and keeps `revenue_by_source` — the surviving reader is
  still built and checked, so the audit does not pass vacuously for it.
* `analysis/sql_doctrine_registry.py`: the source consumer now names the
  library and the Dashboard routes that surface it, `drawer` is false, and the
  rules for `build_source_platform_detail`, `_source_contact_row`,
  `fetch_source_contact_details`, `get_revenue_by_source` and the `ui.source`
  renderers are gone. `audit_sql_doctrine_inventory --static-only` finds no
  unclassified occurrence and no registry problem.
* `tests/test_pr_ads_154c_cross_page_parity.LEGACY_READ_REGISTRY` drops the
  drawer's `fetch_source_contact_details` read (its stale-entry guard requires
  it).

## 7. Known gaps

1. Channels has no per-deal list behind a channel row; the deals behind a
   source are read on the Deals page (by source group, business windows),
   not filtered to one channel.
2. The platform split (e.g. LinkedIn vs Facebook inside Organic Social) is
   not on Channels, which groups by HubSpot original source. PR-ADS-171B
   (PR 223, pending) adds a platform split of Paid Social **contacts** on
   Channels. After both merge there is still no platform split of **deals or
   revenue**, and no Organic Social split at all.
3. The Dashboard source figures (business windows, PR-ADS-133 source
   groups) link to Channels (evidence windows ending yesterday, HubSpot
   original source), which does not reproduce them. Every such link reads
   "Open Channels (different windows and grouping)"
   (`canonical_contract.CHANNELS_LINK_LABEL`), and the Revenue tab panel is
   titled "Closed-won by source group · this window".

## 8. Proof

`tests/test_pr_ads_168b_revenue_by_source_removed.py`: the routes are 404 on
the running app and `view=source` is 400; the deleted functions are not
defined; no nav entry, section, route key, renderer or CSS remains, and the
only reference to the page id in `static/` is the redirect entry; the real
redirect map, evaluated in `node`, lands the old key on Channels; the backfill
panel is in the Revenue Health section and the real
`renderSourceBackfillPanel` renders it for an admin and not for a viewer; every
Dashboard `target_page` is a real page; the registries name no deleted symbol;
the real `channelOutcomeCellsHtml` renders won deals, customers and revenue per
channel. Eleven of its fourteen cases fail against the parent commit; the three
that pass there (the redirect target exists, the backfill panel renders, the
Channels outcome cells) assert what must survive.
