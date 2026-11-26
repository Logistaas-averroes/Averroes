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
  `pipeline_service._funnel`. Neither service calls `fetch_undated_stage_reach`
  itself. Both pair the window's rows and the undated reach inside ONE
  REPEATABLE READ database snapshot (`one_read_snapshot`, added by F1, §7.1);
  as first committed they were two transactions — `@single_snapshot` memoises
  the closed-won universe and holds no database snapshot.
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
exact — falsely; it is now "at least 2" with the all-source wording.

Every Keyword-attributable stage is now a bound as well. That is **not** a
correction: the fixture has no keyword contact, so 178C's exact keyword
counts were true. It is over-marking, accepted per §6 (the all-source
undated reach cannot say how many of its contacts a scope holds); it fails
safe. In production (~9,000 undated Lead contacts, docs/64 §12.7) most stages
of most scopes read "at least N", some of them over-marked the same way.

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

As first committed, the same function's `excluded_non_paid_count` /
`excluded_pseudo_campaign_count` still defaulted an unread count to 0; F1
closed them (§7.3).

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
* **`test_pr_ads_178c` PG test_05** — Google Ads-source SQL is now a bound
  (§1.2): the fixture's `c-nolb` could have entered SQL in the window. Every
  Keyword-attributable stage is a bound too — over-marking, accepted per §6,
  not a correction (F1 reworded the docstring). The narrowing 178C introduced is
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
  rows (§1.1), and a scope whose true undated figure is 0 can be
  over-marked "at least N" (e.g. Keyword-attributable in 178C's fixture).
  Over-marking is accepted: it fails safe.
* ~~**The sibling legacy counts**~~ — closed by F1 (§7.3).
* **Cost.** Pipeline now runs one more count-only scan per request (the
  180B audit measured ~66–90 ms at 22k rows for Activity's); the
  outside-the-rows FILTER re-evaluates the window predicate per row. Not
  measured on production.
* **Browser rendering.** No screenshot.

## 7. PR-ADS-180C-F1 — the truth audit's minors

The truth audit of `98cd829` returned "merge with minors". F1 closes all four
and the observation, in one commit.

### 7.1 Minor 1 — one database snapshot for the two reads (affects a figure)

As committed, the window's rows (`fetch_funnel_contacts`, inside
`crm_funnel.build`) and the undated reach (`fetch_undated_stage_reach`) were
two `get_conn()` calls: two READ COMMITTED transactions. `@single_snapshot`
memoises the closed-won universe; it holds no database snapshot (only a
certification window's `pinned_read_snapshot` does). The auditor's PG probe:
a contact-funnel sync commits between the reads and gives `g-out` (Google
Ads, at SQL, undated, outside the rows) an in-window MQL date.
`outside_window_rows.sql` reads 0 and Google Ads-source SQL prints "2",
exact — while both consistent states say "at least 2". Overview Activity had
the same tear since 180B (rows before a sync that dates g-out's SQL, reach
after it: "SQLs 2", where before is "at least 2" and after is "3").

**Fix.** `canonical_crm_funnel_service.one_read_snapshot()` is the existing
PR-ADS-179A mechanism, `db.connection.pinned_read_snapshot`: ONE
`REPEATABLE READ, READ ONLY` transaction for every `get_conn()` in the block
(each reader a savepoint), nested inside a certification window's snapshot
when there is one, a no-op with no database. Three sites open it around the
build AND its reach: `pipeline_service._funnel`, Overview's
`_lifecycle_funnel_block` and `_lifecycle_previous_period`. A snapshot that
cannot be taken (the pool is down, a migration holds the schema lock past
its 60 s wait) raises and the funnel reads "could not be read" — never a
torn pair. Cost: one pooled connection held for the two reads, and the
schema advisory lock taken shared (as every certification window does).

Guards (PG): 04_a — the probe as a test, the sync committed from another
thread (its own connection) between the reads: "at least 2", both reads
before the sync, and the next build sees the sync. 04_b — the Activity tear:
"at least 2", then "3" after. 04_c — the previous period (the trend
baseline): its SQL count stays a bound, then exact (3) after. All three fail
on `98cd829` (04_a / 04_b on the figure, "2"; 04_c on `count_is_bound`).

### 7.2 Minor 2 — one window clause

`fetch_undated_stage_reach` carried a second copy of `fetch_funnel_contacts`'
window clause. The audit's mutations widening only that copy's end (+2 days,
m2) or start (−1 day, m3) edge — the unsafe direction: a contact outside the
rows read as inside, a false exact — survived every test. Both reads now
build it from ONE helper, `crm_funnel_repository.any_stage_date_in_window`.
PG 05 places four undated SQL contacts, each with one stage date, at the
database's own edge instants (one minute before the start, at the start, one
minute before the day after the end, at the day after): the rows are the two
inside, `outside_window_rows` the two outside, under closed and open bounds.

### 7.3 Minor 3 — Revenue Health, unknown is not zero

On an outage the strip read "Unknown | 0 | 0 | 0". Now None when unread, and
"Unknown" on the page (`fmtCountOrUnknown`) for every cell:
`fetch_lead_date_grain_health`'s `excluded_non_paid_count` /
`excluded_pseudo_campaign_count`, its `counts_by_source_type` (None until its
query has run, None when it fails), `writers.count_lead_exclusions` (None for
no connection or a failed read — it is the source of `legacy_excluded_count`),
and the audit service's own defaults (`legacy_excluded_count = None` when the
call raises). Unit 03_c drives a simulated outage (no connection anywhere)
through the real repository, the real audit and the real page; it fails on
`98cd829`. The lead-reconciliation summary's "Already excluded legacy rows"
reads the same function and now shows "—" rather than 0 when unread (its
renderer was not changed).

### 7.4 Minor 4 — over-marking said as such

§1.2, §5 and 178C PG test_05's docstring now say the Keyword-attributable
bounds are over-marking accepted per §6 (the fixture has no keyword contact;
178C's exact keyword counts were true), not a correction. The assertion is
unchanged.

### 7.5 Observation — the population label

`funnel.undated_reach.population` is "selected_population_narrowed" when the
counts are a narrowed population's (the all-source figures stay in
`all_source_counts`); "all_source" only when they are the all-source ones.

### 7.6 Guards and evidence

New: PG 04_a, 04_b, 04_c, 05 and unit 03_c (the PG module is already in the
CI PG step and its did-run list). 04_a, 04_b, 04_c and 03_c fail on `98cd829`
(on the figure / the value); 05 passes there by design (the two copies then
agreed) and guards the clause by mutation:

| | Mutation | Failed |
| --- | --- | --- |
| m2 | the undated read's clause drifts: end edge +2 days (the audit's m2) | PG 05 |
| m3 | the undated read's clause drifts: start edge −1 day (the audit's m3) | PG 05 |
| m2h | the shared helper: end edge +2 days (both reads) | PG 05 |
| m3h | the shared helper: start edge −1 day (both reads) | PG 05 |
| S | `one_read_snapshot` holds no snapshot (every site) | PG 04_a, 04_b, 04_c |
| Sp | Pipeline's funnel reads outside the snapshot | PG 04_a |
| So | Overview Activity's current-window reads outside the snapshot | PG 04_b |
| Spp | Overview Activity's previous-period reads outside the snapshot | PG 04_c |
| R1 | repository: unread `excluded_non_paid_count` is 0 | 03_c |
| R2 | `count_lead_exclusions`: no connection is 0 | 03_c |
| R3 | audit service: a raising exclusion count is 0 | 03_c |
| R4 | repository: unread per-source counts are zeros | 03_c |
| R5 | Revenue Health prints a dash for an unread count | 03_b, 03_c |

Each applied alone to a copy of the tree, with the 180C modules, 180B PG,
178B PG and 178C PG test_05. None survives. (The audit's m2 / m3 can no
longer be applied as written — there is no second copy — so m2 / m3 drift
the undated read's call to the shared helper, the same effect.)
