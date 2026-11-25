# 65 — Pipeline reads the undated-reach bound (PR-ADS-180C)

Builds on `docs/64` (PR-ADS-180B / 180B-F1). Closes `docs/64` §8's open
item "Pipeline's stage-entry bound reads only the window's rows" (the 180B
truth audit's m3) and the two minors the 180B-F1 re-review carried.

## 1. m3 — one population, one reading, on both pages

**Before.** For the same window and the same contacts, Overview → Lifecycle
Activity read "Leads at least 10 … 7 contacts reached this stage with no
entry date", while Pipeline read "Entered Lead 10", exact. The Pipeline bound
(`stageEntryLowerBounds`) read only the window's own rows
(`fetch_funnel_contacts`: contacts with some stage date in the window), so an
undated contact with no date in the window was never seen.

**Now.**

* **One backend rule.** `undated_stage_reach(window)` and
  `stage_count_is_bound(event, coverage, undated)` moved from
  `dashboard_overview_service` to `canonical_crm_funnel_service`. Overview's
  `_undated_reach` / `_stage_count_is_bound` call them; so does
  `pipeline_service._funnel`, inside `build_pipeline`'s snapshot. Neither
  service calls `fetch_undated_stage_reach` itself.
* **The Pipeline payload** carries `funnel.undated_reach` and, per stage in
  `funnel.events`, the Activity block's two fields: `undated_contacts_could_belong`
  (None when not read) and `count_is_bound` (the server's verdict).
* **One page rule.** `dashActivityLowerBounds` is now `stageEntryCountBounds(block,
  stages)`; `dashActivityFunnelModel` and `pipelineFunnelModel` both call it,
  and the window-row rule (`stageEntryLowerBounds`) is reached only through it.
  Same population, same window: the same value and the same bound reason on
  both pages (unit 01_b; PG 01 through the real reads and builders).

### 1.1 Lead filters: the all-source figure is never the subset's

`fetch_undated_stage_reach` counts every canonical contact — all sources, no
lead filter. Under a Scope or Source filter that figure is only an upper
limit on the subset's, so it is never published as the subset's
(`undated_reach_for_population`):

* the read now also returns `outside_window_rows`: per stage, the part of the
  figure with **no stage date in the window** (the predicate is
  `fetch_funnel_contacts`' window clause, character for character);
* where the all-source figure is **0**, the subset's is 0 (a subset of none
  is none);
* where `outside_window_rows` is **0**, every undated contact that could belong is among the rows
  the funnel build read, so the build's own coverage for the selected scope
  and group (`_funnel_coverage`, PR-ADS-178C) is the complete figure — a narrow
  scope with no gap of its own is exact;
* above 0, or unread, the subset's figure is `None` and the stage is a bound.
  The page names the all-source figure as such (`undated_contacts_all_sources`):
  "N contacts across all sources … could belong to this window; how many of
  them the lead filters select is not measured".

Nothing is dated, nothing is assigned to a scope: the narrowing uses only
"none outside the rows", a fact the database states.

### 1.2 Consequence: 178C's narrow-scope exactness was partly false

178C made a narrow scope exact where its own rows had no gap. That missed the
same contacts m3 is about. In 178C's PG fixture, `c-nolb` is Google Ads, at
SQL, with no SQL-entry date, created in the window and with no stage date in
it: it may have entered SQL in the window. Google Ads-source SQL was printed
exact; it is now "at least 2" with the all-source wording, and so is every
Keyword-attributable stage. In production (~9,000 undated Lead contacts,
docs/64 §12.7) most stages of most scopes read "at least N".

### 1.3 Hardening

`stageEntryCountBounds` fails closed on a stage whose `count_is_bound` is not
an explicit `false` (missing included), and on a stage whose undated reach is
not measured (`None`) whatever the server says. 180B-F1 had left the missing key to the
page's own rules (§12.1 of docs/64; the re-review's probe printed "MQL 12").
The real producers always set it; the 180A activity fixtures now carry it.

## 2. Carried minor (a) — withheld means absent, the notes too

`revenue_decision_mart._withhold_cohort` (a window with no settled day) copied
`metadata.coverage_notes` from the published cohort, so a withheld block still
said "4 cohort SQL contact(s) are proven by lifecycle stage …": a floor on the
SQL count it withholds. The notes and `coverage_status` are now rebuilt for
the verdict by `marketing_outcome_cohort_service.unpublished_coverage_notes`,
the one function both unpublished paths use (the cohort's own verdict, through
`coverage_notes`, and the mart's). Only the reason and the table-wide
no-created-date disclosure survive. A non-`withheld` status is named as itself
("is unavailable (no_settled_day_in_window)"), not as "withheld … not proven
complete".

`audit_marketing_outcome_cohorts.mart_withheld_exposures` now scans the notes:
a note matching `^\d+ cohort SQL contact`, or any note that leads with a count
other than the no-created-date disclosure, is an exposure.

## 3. Carried minor (b) — unknown is not zero

The legacy audit's `missing_contact_created_at_count` (lead-date grain of the
legacy `leads` table; not the cohort/readiness figure) defaulted an unread
count to 0 in `revenue_repository.fetch_lead_date_grain_health` and in
`build_revenue_attribution_audit`. It is `None`; Revenue Health's "Included
paid leads missing dates" reads "Unknown".

Not changed: the same function's `excluded_non_paid_count` /
`excluded_pseudo_campaign_count` still default an unread count to 0 (same
defect class, outside this brief; §6).

## 4. Guards and evidence

`tests/test_pr_ads_180c_pipeline_undated_bound.py` (12) and
`tests/test_pr_ads_180c_pipeline_undated_bound_pg_integration.py` (4, added
to the CI PG step and its did-run list). **All 16 fail on `7cd5e22`**, and
180B-F1's extended 04_c fails there too. On the figure: 01_a ("10"), 01_b
(the two pages differ), 01_c, 01_d ("3"), 01_f ("12"), 02_a, 02_b, 03_a,
03_b, PG 01, PG 03_a (the old bound wording), PG 03_b ("2"). On a missing
name: 01_e, 01_g, 02_c (structural one-rule guards) and PG 02 (the new
`outside_window_rows`).

### Named mutations

Each applied alone to a copy of the tree; the 180C, 180B, 180B-F1, 180A
modules and 178C's test_09 / PG 05 run.

| | Mutation | Failed |
| --- | --- | --- |
| A | `pipelineFunnelModel` reads only the window-row rule (the pre-180C model) | 01_a, 01_b, 01_c, 01_d, 01_e, 01_f, PG 01, PG 03_a, PG 03_b, 178C PG 05 |
| B | a lead filter ignores what lies outside the window's rows | 01_d, 01_g, PG 03_b, 178C PG 05 |
| C | a lead filter reads the all-source figure as its own (`whole=True`) | 01_d, PG 03_a, PG 03_b, 178C test_09, 178C PG 05 |
| D | the page reads a missing `count_is_bound` as complete (`=== true`) | 01_f |
| E | `_withhold_cohort` keeps the published cohort's notes | 02_a, 02_c, 180B-F1 04_c |
| F | `mart_withheld_exposures` does not scan the notes | 02_b |
| G | the repository reads an unread missing-date count as 0 | 03_a |
| H | the audit service reads an absent missing-date count as 0 | 03_b |
| I | Revenue Health prints a dash, not "Unknown", for None | 03_b |
| J | `outside_window_rows` counts every undated contact, in the rows too | PG 02, PG 03_a, 178C PG 05 |
| K | Pipeline's `count_is_bound` ignores the undated reach | 01_a, 01_b, 01_c, 01_d, PG 01, PG 03_b, 178C PG 05 |
| L | Pipeline publishes the window-row gap as the undated reach | 01_a, 01_b, 01_c, 01_d, PG 01, PG 03_b, 178C PG 05 |
| M | the page reads an unmeasured stage (`None`) as complete when the server says so | 01_f |

Every mutation fails at least one test; none survives. (A mutation reading
an unread all-source figure as 0 for a subset was dropped as equivalent:
where it could apply, "nothing outside the rows" already decides.)

## 5. Changes to existing tests (each keeps its truth content)

* **`test_pr_ads_178b_pipeline_one_page._payload`** — each stage carries the
  fields `_funnel` now publishes (no undated contact, every count complete),
  plus `coverage` and `undated_reach`. Without them every stage is, correctly,
  a bound.
* **`test_pr_ads_178c` test_09** — the three funnels are built by the real
  `pipeline_service._funnel` over the same coverage, with the undated read the
  fixture implies (its one undated contact is in the rows). Same marks.
* **`test_pr_ads_178c` PG test_05** — Google Ads-source SQL and every
  Keyword-attributable stage are now bounds (§1.2): the fixture's `c-nolb`
  could have entered SQL in the window. The narrowing 178C introduced is
  still proven where nothing lies outside the rows (180C PG 03_a, unit 01_d).
* **`test_pr_ads_166a._pipeline`** — the hand-built funnel slice is paired
  with its own undated read (none anywhere) and carries the coverage a real
  build always reports; the database case keeps the real read
  (`undated=None`).
* **`test_pr_ads_180a`** `_activity` / `_bound_activity` — stages carry
  `count_is_bound` (§1.3).
* **`test_pr_ads_180b_f1` 04_c** — also asserts no `^\d+ cohort SQL contact`
  note survives, and the no-created-date note does.

## 6. Not done, and why

* **A scope-aware undated reach.** Counting undated contacts outside the rows
  per scope needs the source taxonomy and the campaign resolver, which live
  in Python; done in SQL it would be a second copy of the taxonomy. Until
  then a narrow scope is exact only where nothing undated lies outside the
  rows (§1.1).
* **The sibling legacy counts** (`excluded_non_paid_count`,
  `excluded_pseudo_campaign_count`) still default to 0 when unread (§3).
* **Cost.** Pipeline now runs one more count-only scan per request (the
  180B audit measured ~66–90 ms at 22k rows for Activity's); the
  outside-the-rows FILTER re-evaluates the window predicate per row. Not
  measured on production.
* **Browser rendering.** No screenshot.
