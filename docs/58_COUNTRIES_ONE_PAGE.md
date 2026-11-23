# 58 — Countries: one page, one rule per figure

Series doc for the Countries merge. **PR-ADS-176A** (this PR) fixes the shared
country builder so each figure has one rule, and changes nothing in navigation
or page layout. **PR-ADS-176B** will merge the two pages into one.
**PR-ADS-176C** will retire `/api/geo`.

---

## 1. The two pages today

Both pages are under the Countries menu item (PR-ADS-174A).

| | Countries (`#/geo`) | Country Revenue & Spend (`#/roas-countries`) |
| --- | --- | --- |
| Endpoint | `GET /api/geo` | `GET /api/revenue-decision-mart?view=country` (the Dashboard's Countries tab is `GET /api/dashboard/countries`, built on the same mart view) |
| Builder | `services/country_evidence_service.build_country_evidence` | `services/revenue_attribution_service._build_from_db` → `revenue_decision_mart` (country view); `services/dashboard_countries_service.build_dashboard_countries` adds its own geo read |
| Window | Evidence windows `7d`/`14d`/`30d`/`60d`/`180d`/`all_time`, N account days ending yesterday | Business windows (quarters, last 6 months, YTD, all time), settled-day: an open window ends yesterday (PR-ADS-167A) |
| Spend gate | Geo coverage only: every day has a verified chunk fetched after the day ended | The full country gate: campaign coverage, FX, geo coverage (same settle rule) and geo ↔ campaign reconciliation, or an accepted residual |
| Country population | Every ISO code Google reports, about 162 in production. Lead text is resolved against the complete ISO table | The 96 listed markets (`country_identity.SUPPORTED_COUNTRIES`) plus one residual row |
| Unresolved spend | One "(unresolved location)" row | Before 176A this spend was dropped. It is now in the residual's `country_unidentified` component |
| Countries outside the 96 | Their own rows | Before 176A they were one number in the residual's `outside_listed_markets`. They are now itemised (§2.3) |
| Outcomes | Leads, SQLs, In Progress, Junk, Wrong Fit, Junk rate, top campaign, top keyword. These come from the lead country summary, by raw country text | Leads, SQLs, won deals, customers (distinct companies), won revenue, Period Revenue / Ad Spend, decision |
| Drawer | None | `/api/revenue-performance/country-detail` (Country Revenue & Spend). The Dashboard embeds `deals` per row |
| Zero rule | PR-ADS-167A: `$0` only when proven | Before 176A, `$0` for any country with no geo row whenever the geo read succeeded |

## 2. One rule per figure (PR-ADS-176A)

### 2.1 Spend `$0`: one zero rule, `services/country_spend_zero_rule.py`

The PR-ADS-167A rule has moved, unchanged, from `country_evidence_service`
into a shared module. All three builders call it.

A country with a geo spend row in the window has measured spend. A country
with no row reads `$0` only if one of these holds:

* no spend in the window is unresolved, or
* every unresolved criterion has no code on any stored row, and the
  country's code is carried on record by exactly one criterion that carries
  no other code (`repo.fetch_geo_code_carriers`).

Otherwise the spend is `None` with `spend_reason: "spend_location_unresolved"`.
A country that has never appeared in geo spend is never "carried". If the
carriers read fails, no zero is proven.

* `assess(spend_rows, candidate_codes)` reads the carriers. Callers run it
  inside the `repo.geo_read_snapshot()` that read the spend rows.
* `read_country_spend(start, end, customer_id, candidates)` reads the spend
  rows (with criterion identity resolved in the same statement) and the
  carriers in one snapshot. The read is scoped to the configured account.
  The mart path and the Dashboard both use it.
* Countries keeps its own spend → identities sequence inside its snapshot and
  calls `assess` there.
* If the snapshot cannot be opened, the result is unreadable geo with no zero
  proven. It is never a crash: the mart opens the snapshot for every view
  through the revenue contract.

The rule's effect on each surface:

* Mart / Country Revenue & Spend: `_build_db_rows` counts the geo rows each
  bucket receives. A real country bucket with none is passed through
  `zero_spend_verdict`. `_finalize_row(spend_withheld_reason=…)` publishes
  spend `None`, the reason, a note, and no ratio.
* Dashboard: `_build_country_rows` gives `spend_native` / `spend_usd` `None`
  and `spend_reason`. The listed markets' total (`verified_spend_*`) is then
  unknown and is withheld, with `kpis.verified_spend_reason` and
  `zero_withheld_countries`. The other figures follow:
  * The `unavailable` entries, the "Unavailable" decision card and the ratio
    reason name the markets and the cause. They say "their $0 is not proven",
    never "their spend is not $0": the evidence says the spend is unknown
    (round 2). Before the truth audit they said "FX
    coverage is incomplete — native GBP spend is still shown", although FX
    was verified and native spend was withheld too.
  * `metric_truth.country_attributed_spend_usd` is `not_ready` with
    `unavailable_reason: spend_location_unresolved`.
  * `kpis.countries_with_spend` is `None`, because it counts rows whose spend
    is unknown. The KPI reads "Countries with spend withheld".
  * Country Revenue & Spend and the Dashboard print "Unavailable — some spend
    in this window has no country", never "—".

### 2.2 Code-less criteria count, on the residual

`_build_from_db` used to drop a canonical geo row whose criterion named no
country (`if not name: continue`). That spend was in the reconciliation total
but on no row. It is now bucketed into the residual's `country_unidentified`
component. In a reconciled window, Σ market rows + residual spend = campaign
total.

### 2.3 Outside-market countries itemised

The residual row (mart) and the Dashboard residual now carry
`outside_listed_market_items`. Each item is
`{country, country_code, spend (mart) | spend_native/spend_usd (Dashboard),
provenance: google_ads | owner_labelled, breakdown_of}`. The items break down
`spend_components.outside_listed_markets` (mart) or
`kpis.spend_outside_listed_markets` (Dashboard). They sum to it and are never
added to it. Leads, SQLs, deals and revenue on an item are `"in_residual"`,
never `0`. When the gate withholds the residual, these items are withheld
with it.

The residual row never publishes a period ratio (truth audit M2). Its spend
belongs to no market, and its deals' CRM countries are no listed market.
These are two unrelated populations. Before the audit, £3 of code-less spend
beside a $5,000 deal from "Atlantis" published 1333.33, and with no residual
revenue it published 0.0. `period_revenue_per_ad_spend` and
`period_spend_per_customer_won` are now `None`, with their metrics
`unavailable` / `residual_not_a_market`. This matches the spend-only residual
row.

### 2.4 Each drawer lists its row's own population

* Country Revenue & Spend drawer (`build_country_deal_details`): it now uses
  the settled-day window and `settled_revenue_bounds`, the same days as the
  row. It also applies the same "not Google Ads" label exclusions, read for
  the same account as the row. Before this, a deal closed today appeared in a
  drawer whose row did not count it. The unscoped campaign spend read also
  took another account's mappings (its `MIN(customer_id)`), and with them an
  excluded deal.
* Dashboard drawer: each mart country row now carries `deal_ids`, the deals it
  counts. The drawer lists exactly those, at `google_ads_source` scope. Before
  this it listed every won deal of the country from any channel, and so did
  the revenue verdict's deal denominators. The mart withholds `deal_ids` along
  with the row's other figures. A row whose deal count is withheld carries
  `deal_ids: None`, never `[]`, and the reader-cutover audit's proof-list check
  now knows `deal_ids`. When any row's population is withheld,
  `country_revenue_disclosure` publishes no deal denominators (`None`, never
  `0`).
* Round 2: a Dashboard row whose deal count or population is withheld carries
  `deals: None`, never `[]`. This holds in `_build_country_rows` and in
  `_withhold_when_geo_not_ready`. The audit's proof-list check flags any
  non-None `deals` beside a withheld count, `[]` included. The drawer
  (`ctryDealBody`) says "Closed-won deals are withheld", never "No closed-won
  deals".

### 2.5 The Dashboard geo read is the configured account's

`fetch_geo_daily_spend_by_country` is called with the configured customer id
on the Dashboard and in the mart's per-country read (PR-ADS-154B). The geo
total these rows are reconciled against was already scoped. A second
account's spend was landing on market rows.

### 2.6 No status claims customers from deals

`"Customer proven"` → `"Won deal, revenue not proven"`.
`"Spend without customer proof"` → `"Spend without a won deal"`.
Both are computed from the market's won-deal count. A status may say
"customer" only if it is computed from distinct companies through the reader
contract, and none is. In `static/app.js`, `CTRY_STATUS_META` /
`CTRY_NEXT_ACTION` are re-keyed to the new strings, so colours and next
actions stay the same.

Regional Mix follows the country rule (truth audit M3):

* a withheld deal count reads "Closed-won outcome withheld", never "Spend
  without a won deal";
* an unknown spend reads "Geo attribution needs review", never "No spend in
  window".

### 2.7 The blocked card names the gap

The mart's `spend_truth` now carries two fields:

* `country_gap_primary`: the one gap, by the gate's repair priority
  (`google_ads_geo_sync_service.primary_geo_gap`);
* `country_gap_message`: that gap's sentence.

`renderCountrySpendUnreconciledState` takes its cause and its next step
(`COUNTRY_GAP_NEXT_STEP`) from that one code.

The run buttons (`roasCountryCtaButtons(run)`) are the union of
`COUNTRY_GAP_RUN` over every gap code, so a secondary gap that a run fixes
still offers that run (round 2). For example,
`[comparison_not_like_for_like, geo_coverage_incomplete]` offers the Geo
Sync. `account_time_zone_unknown` offers the Spend Backfill, which writes the
account time zone.

A payload that names gaps without a primary gets "Open Revenue Health and
review…" and no run. It never falls back to the "run the Geo Sync" default.

Round 2 also fixes unreadable campaign spend. When canonical campaign spend is
unreadable (`spend_source = "geo_fallback"`), the mart now counts it as
unreadable. The gate no longer reports `comparison_not_like_for_like`
without a readable baseline: with no baseline there is no comparison, so
"not like for like" is not a finding. The PR-ADS-154B ranking is unchanged.
Before this fix the card blamed accounts or currencies. Before the truth audit, the
cause came from the full list and the next step from a subset, so
`[campaign_coverage_incomplete, spend_not_reread_since_day_ended]` named
campaign coverage and then said "Nothing to run". An admin also saw "Nothing
to run" beside a "Run Google Ads Geo Sync" button.

* Before the morning sync: "Nothing to run: the morning sync re-reads the
  newest day …". There is no run button, and no "ask an admin to run".
* Never-fetched coverage still says to run the Geo Sync and offers it.

The old "no canonical geo spend yet — run the Geo Sync" copy is kept only for
a payload that names no gap.

The UI changes in this PR:

* this card;
* the re-keyed status maps;
* the spend cells and the KPI sub-line (§2.1).

There is no navigation or layout change.

## 3. Decisions

**Owner decisions, 2026-11-23** (recorded for PR-ADS-176B; they do not change
the scope of 176A):

1. **Business windows.** The merged page uses business windows: quarters, last
   6 months, YTD and all time. The default is Current Quarter. The 7- and
   14-day views go away.
2. **Strict spend gate.** When geo and campaign spend do not reconcile for a
   window, country spend is withheld with the reason. It is not shown with a
   warning.
3. **Lead-quality columns dropped.** Junk, In Progress, Junk rate, top
   campaign and top keyword are dropped from the merged page. The Leads page
   owns lead quality.

**PR-ADS-176A decisions.** Each one is a reading of standing doctrine, not a
new owner call.

* A spend-less market's `$0` follows one rule on every surface (§2.1).
* When one market's spend is unknown, the listed markets' spend total is
  unknown too. It is withheld with the reason, never shown as a partial.
* Outside-market countries are a breakdown of the residual. Their outcomes are
  `"in_residual"`.
* A drawer lists its row's population: same scope, same days, same exclusions.

## 4. What 176B / 176C will do

* **176B**: one Countries page on business windows (decision 1) with the strict
  gate (decision 2). It covers the 96 markets plus the residual and its
  itemised outside-market countries. Lead-quality columns are dropped
  (decision 3). The Countries and Country Revenue & Spend tabs become one.
* **176C**: retire `/api/geo` and `services/country_evidence_service.py`. The
  shared zero rule stays.

## 5. Not changed here

* Navigation, page layout and windows. Both pages keep their current windows.
* The Dashboard payload still declares `revenue_scope: all_source`. Its row
  deals were already `google_ads_source`; the declaration predates this PR and
  is left for 176B.
* `/api/geo` keeps its own lead-text population. Countries outside the 96 are
  still rows there.

## 6. Tests

`tests/test_pr_ads_176a_country_builder_pg_integration.py`: 24 cases. The
file is registered in the PG step and the did-run list.

* Nine of the first eleven fail on origin/main. Case 02 is the counterfactual
  and passes on both. Case 10 fails when its `except` is mutated away.
* The truth-audit round strengthened 01b and 09 and added 01c, 09b, 11–14, 16b,
  17a, 17b and 18. All of these fail on `98e0b5e`.
* Round 2 strengthened 01b and added 19, 20a, 20b, 21 and 22a–d. All of these
  fail on `122eb3e`.
* Cases 15 and 16 guard claims that were already true at `98e0b5e`. Each fails
  under a mutation that removes its guard:
  * case 15 fails when the mart's or the Dashboard's withholding of
    `deal_ids` / items is removed;
  * case 16 fails when the drawer's exclusion filter is removed.

* 01a: Pakistan reads Unavailable on the mart country view. Jordan's `$0`
  stands.
* 01b: the same on the Dashboard Countries payload, with the KPI withheld and
  its reason.
* 02: counterfactual. With nothing unresolved in the window, Pakistan is a
  measured `$0`.
* 03: mutation. Making the shared verdict always "publishable" turns Pakistan
  back into `$0` on both builders, and every builder imports the one module.
* 04: conservation with a code-less criterion, Fiji and owner-labelled Cuba,
  plus the itemised outside-market countries on both payloads.
* 05: a deal closed today appears in neither the row nor its drawer.
* 06: an all-source deal does not inflate the Dashboard drawer.
* 07: in a two-account fixture, the Dashboard geo map sums only the configured
  account.
* 08: no status mentions customers.
* 09: the blocked card for the settle gap, rendered by the real
  `renderCountrySpendUnreconciledState` from the real mart payload.
* 10: a geo snapshot that cannot open reads as unreadable geo, with no zero
  proven. It never crashes.
* Truth audit:
  * 01b (M1): no FX blame.
  * 01c (MINOR 3): the spend metric names the cause.
  * 09 / 09b (MINOR 1): as admin and as viewer, case-insensitive, no run
    button for the settle gap; one gap drives cause, step and CTA.
  * 11 (M2): the residual ratio, with and without residual revenue.
  * 12 / 12b / 13 (M3): Regional Mix and `countries_with_spend`, through the
    zero rule, an unreadable Dashboard geo read and a withheld deal count.
  * 14 (MINOR 2): no deal denominators while the gate withholds.
  * 15 / 16 / 16b (MINOR 4): withheld `deal_ids` and items; the drawer's
    exclusion, on one and on two accounts.
  * 17a / 17b (MINOR 5): `deal_ids` is None beside a withheld count; the
    audit catches `[]`.
  * 18 (MINOR 6 / M3): the cells and the KPI, in the node harness.
* Truth audit, round 2:
  * 01b: no "spend is not $0".
  * 19: unreadable campaign spend, on the real mart, with the rendered cause
    and step.
  * 20a / 20b: runs for the time-zone gap and for secondary gaps, as admin and
    as viewer.
  * 21: no primary means no "run the Geo Sync".
  * 22 / 22b: `deals: None` when the geo gate withholds and when the ledger
    is not current.
  * 22c: the audit flags `deals: []`.
  * 22d: the drawer renders a null deal list as withheld.

Existing tests changed:

* Ten fakes of `fetch_geo_daily_spend_by_country` (`lambda s, e:`) now also
  accept the account-scope argument the real reader has had since
  PR-ADS-165A. The fixtures' data is unchanged. The files are 124, 125, 134,
  135, 136, 137, 138, 139, 140 and 153e_b.
* `test_pr_ads_167a_settled_day_windows::test_11` pinned the carriers read as
  a literal call in `country_evidence_service`. The call moved with the rule
  (§2.1). The test now asserts three things:
  * the page calls `zero_rule.assess(` inside the same snapshot block;
  * `assess` makes the carriers read;
  * `assess` opens no snapshot of its own.
* Truth-audit round: `test_pr_ads_124::test_blocked_country_card_has_recovery_cta`
  and `test_pr_ads_127::test_country_card_copy_branches_on_status` asserted
  the literal call `roasCountryCtaButtons()`. That call is the
  gap-independent CTA that MINOR 1 removes. Both now assert
  `roasCountryCtaButtons(guide.run)`. Their other assertions are unchanged,
  and the admin / non-admin copy they check is still there.
* Round 2: `test_pr_ads_138::test_deal_ledger_unavailable_does_not_claim_no_deals`
  and `test_pr_ads_138::test_167a_a_…_withholds` asserted `deals == []` beside
  a withheld or unknown deal count. That is the defect of round-2 item 5, so
  both now assert `None`.
* `analysis/sql_doctrine_registry.py` now classifies the two new
  sub-item builders. Their `"sqls": "in_residual"` is a marker, not a count.
* `tests/conftest.py` gains an autouse fixture, `_restore_db_pool`. It returns
  `db.connection._pool` to its pre-test value after every test. About a dozen
  PG fixtures (144, 146*, 151, 152, 156_f3, 157*, 158, 159, 160) assign the
  pool directly and then stop their cluster, so a dead pool outlived them.
  Once every mart build opened the geo snapshot, 113 later mock-based tests
  failed in a full-suite run. The failures were in 153e_b, 154c, 161e, 164a,
  167a, 171b and `test_revenue_attribution_*`, and depended on test order.
  No assertion changed.
