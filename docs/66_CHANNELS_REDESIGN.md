# 66 — The Channels page leads with customers; Dashboard → Channels retired (PR-ADS-181A)

Builds on `docs/50` (channel outcomes, PR-ADS-168A), `docs/55` (Meta spend on
Channels, PR-ADS-171B), `docs/63` (the one funnel block, PR-ADS-180A) and
`docs/64` (the acquisition-cohort funnel, PR-ADS-180B). The product owner
approved a mock-up of the page; this PR builds it, retires the Dashboard's
Channels tab (owner decision) and tidies the System page.

**Nothing here changes a figure or a publication verdict.** The Channels page
reads the same `/api/channel-funnel` response; every number on it is a value
that response published, or a share of two published counts of one
population. The backend changes are a deletion (the retired tab's route,
service and repository read), one label ("Deals with no single source") and one
stale sentence ("such as Deals" → "such as Pipeline").

## 1. The Channels page (`#/channels`)

Top to bottom:

1. **Header.** "Channels" / "Where your customers come from." The period
   control (30 / 60 / 180 days / All time) and two chips: "Contacts updated
   26 Nov, 06:06 UTC" (the funnel's `freshness.as_of`, the contact sync's last
   successful incremental; F1) and "Read-only". The period is
   in the hash (`#/channels?window=60d`; the default 180 days needs none), so a
   refresh or a shared link holds it (`channelsWindowFromHash`,
   `channelsHashFor`, `navigateToHash`).
2. **Two labelled KPI groups.**
   * **Deals closed · 31 May – 25 Nov** — Customers won (distinct companies)
     and Closed-won revenue (with the deal count), the window's own figures
     from `outcomes.totals.metrics` through `canonicalMetricHtml`: a published
     0 is "0", anything else "Withheld"/"Unavailable" with the reason. The
     range's title is the outcome window's disclosure (it ends yesterday).
   * **Contacts acquired · 31 May – 26 Nov** — New contacts and "Reached SQL
     so far" (its share of the cohort), read from the shared model
     (`channelFunnelModel` → `acquisitionFunnelModel`) and rendered by the
     block's own `funnelBlockValueHtml`: under an unpublished verdict the
     verdict line (`funnelBlockVerdictHtml`) and the word in both cards, no
     count read; under a lower bound "at least N" with the reason.
   * Each group follows its own verdict, and carries its own
     `data-metric-family` / `data-window-basis` from its payload.
   * **"Most from <channel> · N"** under each card (`channelLeader`): computed
     in the browser from published channel rows only — never the deals with
     no single source, the "No source recorded" funnel row, or the Google Ads /
     Meta lines. It is said only when certain: every candidate published, the
     leader above zero and ahead of the runner-up by MORE than the part no
     channel holds (the unresolved deals; for contacts, the no-source row plus
     the contacts with no created date — any of it could be the runner-up's).
     A tie, an unpublished row or an unknown remainder says nothing.
3. **One ranked table**, seven columns: Channel | Customers | Revenue | Share of
   revenue | New contacts | SQLs | SQL rate.
   * Default order: customers won, descending ("Channels ranked by customers
     won"). Every column sorts (a second click reverses); unknown values sort
     last. Sorting by SQL rate ranks channels under 30 contacts after the
     ones with enough to compare (PR-ADS-162I's rule; the small-sample pill
     stays).
   * "All channels" stays on top: its customers are the window's published
     distinct count — never a sum of the rows. The legend says why the rows
     do not add up to it.
   * Zero-customer rows are dimmed. "No source recorded" stays (its outcome
     cells read n/a: those deals are counted under the unresolved row). The
     unresolved deal row is renamed **"Deals with no single source"** (also in
     the API's `UNRESOLVED_LABEL` and its attribution note); its funnel cells
     read n/a. Both stay last whatever the sort.
   * A half that is not published is **absent**, not zeroed: its columns are
     dropped and a line under the title says "… are withheld — <reason>".
   * No paid-media column (the ~30 per-cell "Unavailable" spend and ratio
     cells are gone; spend lives in the paid block). `table-layout: fixed`: no
     horizontal scroll at 1440. Under 600px the table shows Channel |
     Customers | Revenue | SQLs; the rest is in the drawer.
   * One legend line ("customers are distinct companies … so no rate joins the
     two") ending in "How this is counted", which expands the funnel and
     outcome notes, the outcome window's disclosure and the funnel legend
     (collapsed, not deleted).
4. **The channel drawer.** A click (or Enter) on any row, "All channels"
   included, opens a right-side drawer (a full-width sheet on a phone); Esc,
   the close button or a click outside closes it. It holds:
   * the channel's 5-stage funnel — `funnelBlockHtml(channelFunnelModel(...),
     {layout: "vertical"})` over THIS response's channel row: the same block,
     the same model, the same bound and withheld rules (one funnel, one
     design). `layout: "vertical"` is a presentation option of the block
     (`data-funnel-layout`); it changes the CSS only;
   * the channel's deals closed in these days (closed-won deals, customers,
     revenue through `rowMetricHtml`);
   * the top HubSpot source drill-downs ("(none)" is named "No detail
     recorded"); for Paid Social, the platform split (Meta / LinkedIn / other /
     unknown — never counted as Meta), contacts only;
   * "Open in Campaigns →" and "Open in Pipeline →".
   * "Deals with no single source" opens its outcome figures and why each deal
     has no single source; it has no funnel.
5. **Paid channels.**
   * **Google Ads** (from `outcomes.google_ads_line`): Spend, Revenue,
     `PERIOD_RATIO_LABEL` and `PERIOD_SPEND_PER_CUSTOMER_LABEL`. Beside each
     period value its disclosure in short ("Non-cohort operational ratio"),
     the full disclosure as its title, and a note: "N customers from M deals
     with a Google click ID or HubSpot Paid Search. Deals closed in these days
     may come from earlier ads." "How deals are matched" expands the evidence
     split and the spend note.
   * **Meta Ads** (from `outcomes.meta_ads_line`): Spend only, through the one
     gate (`metaLineSpendMetric`), its own-currency total beside it only when
     it says something the USD figure does not. "Not yet matched to HubSpot
     deals, so there is no revenue ratio." "Account details" expands the
     account IDs, day basis and withheld detail. The owner's retirement
     disclosure stays BESIDE the figure, never behind the link.
6. **Formats** are the Overview's: `fmtMoney` ($k / $M), "26 Nov" dates (the
   years when a range crosses one), one-decimal percentages. The Overview's
   delta chips print "0.0%" for a zero, so a zero share does too.

Every renderer takes the response (and the view) as an argument
(`renderChannelFunnel(data, view)`, `channelDrawerHtml(data, key)`); none reads
page state.

## 2. Dashboard → Channels retired (owner decision)

* **Frontend.** The tab, its root, its loader (`loadDashboardChannels`,
  `/api/dashboard/channels`) and its renderers (`renderDashboardChannels`,
  `renderChan*`, `chan*`, `CHAN_*`) are deleted. `LEAD_STATUS_QUALIFIED_LABEL`
  stays (Revenue Health's mapping cards read it). A bookmark of
  `#/dashboard?tab=channels` opens the Channels page (`navigate` maps it
  before the hash is rewritten; `dashTabIsRetiredChannels`).
* **Backend.** `GET /api/dashboard/channels` (`api/server.py`),
  `services/dashboard_channels_service.py` and the repository read only it
  used (`db.revenue_repository.fetch_source_leads_daily`) are deleted.
* **Registries.** The reader-cutover audit's INVENTORY row and META_LINE_PAGES
  row are removed and the route and builder join `RETIRED_READER_ROUTES` /
  `RETIRED_READER_SYMBOLS` (`retired_readers_absent` fails if either returns);
  `_page_builders` no longer builds it; `META_JS_RENDERERS` names the Meta card.
  Cross-page parity drops the `dashboard/channels` consumer and its one
  identity of its own, `source_group_sqls` (with its two DISTINCT_BY_DESIGN
  pairs). The SQL doctrine registry drops the "Dashboard Channels" consumer,
  its `chan` and `ui.channels` rules and the `ui.channels_sums_none_as_zero`
  issue (closed by deletion), and binds the redesigned page's functions under
  `ui.channelfunnel`.
* **The Overview's Channel Mix donut stays** (it reads `/api/dashboard/overview`).
* **`CHANNELS_LINK_LABEL` stays on the links that remain.** The three on the
  retired tab went with it. The seven on Overview and Dashboard → Revenue
  link a BUSINESS-window figure to the Channels page's EVIDENCE windows; the
  label "Open Channels (different windows and grouping)" is the PR-ADS-168B
  disclosure that the page does not reproduce the figure, so a plain "Open
  Channels" would promise what the page does not show. They already link to
  `#/channels`.

## 3. System page tidy-up

* **Run types in plain words.** `RUN_TYPE_LABELS` / `runTypeLabel`:
  `daily_incremental_sync` → "Daily data sync", `daily` → "Daily check
  (morning)", `monthly`, `weekly` (retired), `backfill`, `revenue_recovery`.
  A run type not listed is shown **as itself** — an unregistered scheduler is
  something to see. Used by the global freshness pill and the per-page strip.
* **Freshness maps.** Against `CanonicalFreshnessStatus.ALL` nothing was
  unregistered (INVENTORY Part 2). One gap was found in the fallback path:
  `datasetDisplayStatus` passes a row's own run status through, and a
  `partial` one read "Unknown" beside a tally that missed it. `_shortLabels`
  gains `partial` and the warning tally counts it. `success` / `stale` stay
  (that fallback emits them).
* **Healthy is green.** `.system-source--ok` had no rule, and
  `.data-table td` outranked `.system-source--warn`, so a failed source read
  in the same grey as a healthy one. Both are now scoped to the table cell.
* **Stale labels.** "Phase 1 — Read Only" → "Read-only" (login and sidebar).
  The certification note no longer names the retired Leads page and no longer
  says the check skips "the Channels page" whole (the reader-cutover audit's
  evidence pass checks its won-deal figures; its contact counts are what it
  does not re-check). The settled-window disclosure says "such as Pipeline"
  (the Deals page was retired in PR-ADS-178B).
* **Unused CSS deleted.** 263 classes defined in `styles.css` and referenced
  nowhere in `app.js` or `index.html` (literal token match; classes reachable
  only through a dynamic prefix such as `system-card--${tone}` were kept, and
  so was `is-desc`, built as `is-${dir}`). 303 rules and 319 selectors, about
  1,570 lines. Five classes pinned by existing tests stay
  (`revenue-spend-summary*`, `camp-theme-list`, `evidence-drawer-shell`).
  Every page the harness renders (Channels ×5 states, System ×2, Overview) is
  pixel-identical before and after the deletion.

## 4. Tests

### New: `tests/test_pr_ads_181a_channels_redesign.py` (30 cases)

Default sort by customers; every column sorts; the totals row never sums
customers (and stays withheld when the window's figure is); n/a rows; the phone
columns; each group withheld while the other publishes, both ways; metadata on
both groups; "Most from" (7 parametrised cases + the page); the drawer is the
shared block over the shared model, with the bound and withheld rules; drawer
sources, links and the three ways to close; the Google Ads card's disclosure
and check 18; Meta with no ratio; the tab, loader, renderers and route gone and
the audit failing if the route returns; no registry naming the tab; run-type
labels (unknown shown as itself); the freshness maps; stale labels; the period
in the hash; formats; no page state in a renderer.

All 30 fail on `bb4d4a0` (git-archive copy). Mutations, each caught:

| # | Mutation | Caught by |
| --- | --- | --- |
| M1 | the totals row sums the rows' customers | 181A 03, 168A 23 |
| M2 | a tie names a leader | 181A 09 (and 06) |
| M3 | an unpublished row is read as 0 for the leader | 181A 09, 10 |
| M4 | the funnel group reads the totals under a withheld verdict | 181A 06 |
| M5 | the drawer computes its own shares (no bound) | 181A 11, 12 |
| M6 | a period ratio without its disclosure | 181A 14, 168A 21b, 27 |
| M7 | a ratio on the Meta card | 181A 15 |
| M8 | an unknown run type hidden behind a generic label | 181A 19 |
| M9 | the period not read from the hash | 181A 22 |
| M10 | default sort by new contacts | 181A 01 |
| M11 | `/api/dashboard/channels` registered again | 181A 17 |
| M12 | a channel cell reads the row's raw figure around the gate | 181A 03, 04, 168A 24 |

### Removed with the retired tab (each tested only the tab or its route)

* `tests/test_pr_ads_136_dashboard_channels.py` (whole module): the route,
  its builder, the tab's renderers and their fixtures.
* `test_pr_ads_171b` 09, 09b, 09c, 09d: the Meta line on the tab
  (`build_dashboard_channels`, `renderChanMetaPanel`). Their rules hold on the
  Channels page (171B 01–03b, 04/04b, 06, 07b, 07c, 08). 15 is **ported**: the
  store-unreadable case on the Channels page (the tab's first-day-of-quarter
  case was a business window with no settled day; evidence windows always
  hold one, and 03b holds an empty settled window on the line).
* `test_pr_ads_177a_f1` m2_d and `test_pr_ads_177a_f2` f: the tab's
  lead-status labels (`CHAN_METRICS`, `renderChan*`). The label constant
  survives where the count does.
* `test_pr_ads_153e_b` 13c: `dashboard_channels_service._canonical_daily_revenue`'s
  UTC bounds; 13c2 holds the rule for every consumer at the one boundary.

### Ported (every truth assertion kept)

* **162A** 13: SQL rate in the table; stage shares and sources in the drawer.
* **162I** (rewritten in place, numbering kept): 01 the KPI group and the All
  channels drawer; 02/03 the volume and rate rankings are the table's SQLs /
  SQL rate sorts (small samples after comparable channels); 05 a column
  changes the order, never a number; 06 a zero stage is a published 0 with no
  leader; 07 every count and share (table + drawer); 08 sources in their own
  channel's drawer; 09 sorting; 10 a missing count is a dash or the word,
  never 0, and no leader; 11 withheld: no figure, table, control or drawer
  count; 12 escaping (page and drawer); 13 no ratio word, distinct headers, no
  % in an outcome cell, window customers never reduced; 16 the new controls.
* **168A** 01 (the label); 21–28, 31, 33 on the table's customers/revenue
  cells, the groups and the Google Ads card. 21's per-row spend/ratio
  "Unavailable" cells are replaced by an assertion that no channel row carries
  spend or a ratio; 23/24's counterfactual mutations target the new renderers.
* **171B** 07, 07b, 07c, 08, 14 on the Meta card (07c also asserts the
  retirement disclosure is outside "Account details"); 07d on the drawer's
  platform split; 10b/16 mutations target `channelMetaSpendCellHtml`,
  `channelMetaCardHtml` and `channelPaidFigure`.
* **180A** `_SURFACES`, 01_a/01_b, 04_b, 04_c, 06_e via `channelDrawerHtml`;
  06_f withheld on the drawer and the KPI group.
* **180B** 04_b: the bound in the drawer, the KPI cards ("at least") and the
  leader rule (kept when the undated count cannot flip it, omitted when it is
  not stated).
* **168B** 04 (service list), 14 (customers/revenue cells and the drawer's
  deal count through `rowMetricHtml`), 15 (link count 10 → 7: the tab's three
  went with it; the label is unchanged on the seven left).
* **154C** 1d, the fixture, f2_1, f2_2d, f2_4d (service lists); f2_1b and
  f1_4b moved from `dashboard/channels` to `dashboard/revenue` (the rule is per
  consumer); f2_2b two SQL populations, plus "every pair names a registered
  identity"; f2_3d moved to `pipeline/customers_won` (it reads the deal ledger
  directly); f2_7 identities 25 → 24. **154C-F3** revenue paths, f3_7 (four
  populations), f3_22. **154C PG** consumer list.
* **161E** 10, **153E-A2**, **153E-B** 1 (lists), **177B** ×2, **179A** (pinned
  page and builder lists), **178C** 03 (`RETIRED_READER_ROUTES` keeps its two
  routes first; 181A 17 holds the third), **162C** 25 (certification wording),
  **125**, **167A** ×2, **177B** ×2 ("such as Pipeline").
* **153E-A** consumer list (the deleted service was being skipped as "not
  present" — a skip is not evidence, so it is removed instead); **162G** 05
  (dashboard services ≥ 5, one deleted); **165A** 20 (the audit's page
  builders, and `dashboard_channels` is not one); **174A** 17 (the Dashboard's
  inner tabs without Channels).

## 5. Not done / decided here

* `CLAUDE.md` still says there are two baseline deselects. Only
  `test_daily_empty_search_terms_marks_success_not_success_empty` exists; the
  weekly case was deleted in PR-ADS-165A, and the workflow already deselects
  the daily case alone. CLAUDE.md was left unchanged here (its owner updates
  it); its "Campaigns, Countries and Channels dashboard rows" sentence also
  predates this retirement.
* The System page's sources table still scrolls inside its panel at 390px
  (INVENTORY Part 2 #4); its layout was not redesigned here.
* The funnel note `notes.maturity` mentions CPQL; it is the API's text, shown
  only under "How this is counted".

## §F1 — the truth audit's minors (PR-ADS-181A-F1)

The audit of `ccdfdc1` returned "merge with minors". One commit on top:

* **M3 — a leader under a lower bound read as exact.** With
  `contacts_without_created_at > 0` the cards said "at least 2,951" but the
  line under them "Most from Paid Social · 1,147". The leader's figure (and
  every table count) now goes through the block's own `funnelBlockValueHtml`
  via `channelCountHtml` — "at least N" with the reason, no local wording.
  180B test_04_b had pinned "Most from Paid Social · 1,147" under a bound:
  that assertion pinned the defect and now reads "· at least 1,147".
* **M4 — a stale ledger was worded as a missing evidence partition.** With
  no `google_ads_line` the Google Ads card's revenue and ratios now give the
  window's own verdict (`channelOutcomeMetric(o, …)`: "the deal sync is
  overdue"); only a published window with no line says the partition is
  unavailable. Spend still shows.
* **M5 — a reload could leave the drawer on the previous period.** An open
  drawer is dimmed while the new response loads, then redrawn from it — or
  closed when its channel is not in it (`channelDrawerKeyExists`); a failed
  load closes it. A row of the table being replaced (`is-refreshing`) opens
  nothing. The period buttons no longer close the drawer.
* **M6** — the chip says "Contacts updated … UTC": it is the contact sync's
  time, not the deals'.
* **M1 (test gap)** — page cases where the funnel leader is refused by the
  no-source row's margin and by the undated contacts (181A 28). The logic
  was right in `ccdfdc1` (28 passes there); the forced-zero-margin mutant
  fails it.
* **M2** — under a lower bound every SQL rate is unknown, so sorting by it
  ranks nothing: the small-sample rule now applies only between two known
  rates, and the API's order stands (181A 29).
* **Observation** — `datasetDisplayStatus` emits a raw `stale`; it had a label
  and no tally. It now counts as a warning (181A 30).
* **M7** is left as declared in §5 (no figure in that case).

Tests: 181A 23 (chip), 25–30 (new). 23, 25, 26, 27, 29 and 30 fail on
`ccdfdc1`; 28 passes there (a test gap, not a defect). Mutations, each
caught: margin forced to 0 (28); rate computed under a bound (29); small-sample
rule on unknown rates (29); leader exact under a bound (25); missing line
always "partition" (26); drawer not redrawn / vanished channel kept / row
opened while refreshing (27 ×3); chip "Updated" (23); `stale` dropped from the
tally (30). Ported: 162A 13, 162I 01 (chip wording), 180B 04_b (above).
The auditor's "PR-ADS-181A audit" note is appended to its MEMORY.md.
