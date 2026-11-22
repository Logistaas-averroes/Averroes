# 54 — Search Terms pages: bounded memory (PR-ADS-173A)

## 1. What was wrong

Production is one web process on a 512 MB instance, idle at about 120 MB.
PR-ADS-172A (`docs/51_*`) bounded the Campaign drawer sections and listed the
Search Terms pages under "Not fixed". They are these routes:

| route | builder |
| --- | --- |
| `/api/search-term-evidence` (Terms) | `build_search_term_evidence` |
| `/api/search-term-evidence/flagged` | `build_flagged_search_terms` (complete population) |
| `/api/waste` (compatibility adapter) | `build_flagged_search_terms` |
| `/api/search-term-evidence/patterns` | `build_search_pattern_evidence` |
| `/api/search-term-evidence/patterns/detail` | `build_search_pattern_drawer` |
| `/api/search-term-evidence/term` | `build_search_term_drawer` |
| `/api/search-term-evidence/export` | `build_search_term_export` |

Every one of them called `_build_population`, which built every term ×
campaign unit of the selected window in Python — on every request, with no
cache and no limit. A signed-in user choosing "All time" in the Evidence Window
selector and opening Search Terms could get the server killed.

## 2. What was measured, and on what

**Everything below is measured on a synthetic PostgreSQL dataset, not on
production.** It is PR-ADS-172A's dataset: the live tables' row counts on
2026-11-22 (`search_terms` 228,043 rows) with invented content — 27 campaigns,
62,706 distinct queries over 180 source dates, 102,990 term × campaign units,
9,563 flagged fact rows, 9,596 `waste_terms` annotations. Production on that day
(read-only query): 66,455 distinct terms, 101,852 units, **zero** flagged rows
and an **empty** `waste_terms`. So the unit count is close to production's and
the flagged figures are for a flag state production does not have today.
The dataset holds 180 source dates, so its `180d` and `all_time` populations
are the same size.

Peak resident growth of ONE request in a warm process (`VmHWM` after − `VmRSS`
before, each in a fresh interpreter), MB:

| route | 30d before | 30d after | 180d before | 180d after | all_time before | all_time after |
| --- | --- | --- | --- | --- | --- | --- |
| Terms, page 1 | 145 | 24 | 673 | 75 | 673 | 75 |
| Terms, page 3, `q=freight`, sort clicks | 146 | 24 | 676 | 74 | 675 | 74 |
| Flagged page | 145 | 18 | 678 | 79 | 677 | 79 |
| `/api/waste` | 144 | 19 | 677 | 80 | 678 | 79 |
| Patterns (n = 2) | 156 | 46 | 678 | 107 | 680 | 107 |
| Pattern drawer | 147 | 19 | 681 | 77 | 679 | 78 |
| Term drawer (a head query in 27 campaigns) | 147 | 0.1 | 680 | 0.5 | 681 | 1.6 |
| CSV export (20 MB file, 102,990 rows) | 159 | 21 | 673 | 95 | 673 | 95 |

The largest request is now 107 MB (Patterns), against the 150 MB this change
was asked to stay within. Wall time fell with it: Terms 12.2 → 7.8 s, Flagged
32.8 → 8.7 s, Patterns 16.0 → 8.9 s, the term drawer 11.3 → 0.5 s at
`all_time`.

**The payloads are the same.** For each of the 24 cells above the response
(JSON, or the CSV bytes) was hashed before and after on the same database: all
24 digests are equal. That is a development observation; the committed proof
is §5.

**Top allocators before** (`tracemalloc`, at the end of `_build_population`,
`all_time`, 622 MB traced):

| MB | where |
| --- | --- |
| ≈ 222 | the ten `set` objects of each unit (`search_term_variants`, currency codes, source systems, junk categories, matched patterns, ad groups, keywords, match types, source labels, campaign ids) |
| 91 | the unit dict itself, 25 keys |
| 62 | `db/search_term_repository.py` `_rows_as_dicts` — one dict per (term, campaign, day) cost row (the grouped rows — ≈ 255 MB of resident memory while they were being merged, `docs/51` §2 — had already been released at this point) |
| 41 | each unit's `_daily_micros` dict, its keys and amounts |
| 19 | each unit's stored `_provenance` dict |

and, added by the page on top of the population: Terms — 49 MB of per-unit
SQL-attribution state (`_row_state` dicts and unit-key strings); Patterns —
80 MB (one dict and two sets per unique term, the membership sets, the n-gram
strings).

## 3. Why SQL does not do the paging

The units are merged in Python by `normalize_search_term` (NFKC, casefold,
whitespace collapse) and by the canonical campaign identity resolver. Neither
is expressed in SQL here, and neither is approximated in SQL by this change: a
rule written twice can disagree with itself, and then a row and its KPI
describe two populations. Sorting, filtering, the KPIs and the facets are all
defined over merged units and their per-date FX conversion, so they stay where
the units are.

What changed is how the units get there and what they cost while they are
there.

## 4. The change

### 4.1 The rows are streamed into the units

`fetch_search_term_aggregates(sink=…)` and
`fetch_search_term_daily_costs(sink=…)` read the same statements through
server-side cursors and hand each row to the caller instead of returning a
list. `_build_population` merges each row into its unit as it arrives, with
the same `_merge_group`. Neither result set exists in the process as a list of
dicts.

* The grouped statement gains `ORDER BY search_term COLLATE "C", campaign_name
  COLLATE "C", campaign_id COLLATE "C"`. It had no order before, and a few
  published details followed whatever order PostgreSQL's aggregate emitted:
  which of two same-named campaigns a facet lists first, which of two flagged
  rows with an equal sort key comes first, the order of a term drawer's
  campaign list, the display name of a campaign id the spend table does not
  know. Those are now deterministic. No value changes.
* A reader that only offers the row-list form — the two-argument stand-ins
  every existing test installs — is asked for the rows, and gets the same
  merge. `reader_accepts(reader, "sink")` decides, from the reader's
  signature.
* A stream that breaks midway answers `available: False` and the build
  publishes nothing — never the units that happened to arrive. An exception
  raised by the consumer propagates; it is not reported as the source being
  unavailable.

### 4.2 The unit is compact, and reads as the dict it replaces

`_Unit` holds the same fields under the same keys and is read and written
through the same `unit["key"]` / `unit.get("key")` protocol, so every filter,
sort, KPI, facet and row builder is the code it was. Only the storage differs:

* fixed slots instead of a hash table per unit;
* set-valued fields are frozensets, and equal ones are **one object** for the
  whole population (4,819 distinct keyword sets for 102,990 units here);
* a unit with one raw spelling stores the string, not a set of it;
* the per-date native amounts are one flat tuple;
* `_provenance` is derived on read by `_assess_currency_provenance`, from the
  fields it was always derived from;
* the SQL-attribution state is referenced, not copied out of.

A key that was never set is absent, as a dict key would be. A field the unit
does not have is a `KeyError` on write. A set read from a unit is immutable, so
a stray in-place `.add()` fails instead of changing a set other units share.

Nothing is rounded, dropped or summarised. `fx_complete`, `currency_status`,
`spend_usd` and the three-state monetary summary come from the unchanged
`_fx_convert_population` / `_convert_daily_micros`: the same integer micros
summed under the same keys, each day multiplied by its own rate in the same
date order, the same rounding. A rate missing for a day is `fx_incomplete`; a
rate read that FAILED, or a per-date cost read that failed, is an unavailable
population — exactly as PR-ADS-172A established.

### 4.3 Only the page is serialized

* **Terms.** `_sorted_page` selects the page with `heapq.nsmallest`, which is
  defined as `sorted(...)[:n]`, ties included; a deep page uses the plain sort.
  Totals, KPIs and facets are still computed over the complete filtered
  population. The population is released before the independent canonical
  reconciliation read runs, instead of being held beside it.
* **Flagged / `/api/waste`.** One full row is still built per flagged unit, but
  only the fields the filters, sorts, KPIs, facets and the identity invariant
  read are kept for all of them (`_FlaggedIndexRow`, which **raises** on any
  other key rather than answering `None`). The page's full rows are built
  again by the same call on the same unit. The units that are not flagged are
  released once the audit block has read them. A set that the KPI step rebuilt
  once per flagged unit is now built once.
* **Patterns.** The per-term and per-pattern dicts are slotted records with
  shared frozensets; the units are released before the patterns are
  aggregated. Two lists nothing read (`campaign_rows`, `_units`) are no longer
  kept — the first pinned every unit for as long as the terms lived. The
  overlap count is taken per term over its own grams instead of per term over
  every pattern row: the same number, since a term's grams are distinct and
  each pattern is one row.
* **Pattern drawer.** Membership depends on the term's text alone, so it is
  decided first and only the member terms are unified.
* **Term drawer.** The drawer publishes one term, so it loads the units of that
  normalized term only — every raw spelling, every campaign
  (`fetch_search_term_aggregates(normalized_terms=…)`, spellings decided by
  `normalize_search_term` over a server-side cursor, as 172A does for flagged
  candidates). If the CRM carries exact user queries, attribution needs every
  unit and the complete population is built, as the flagged view does.
* **CSV export.** `rows` is a sequence that serializes each row as it is read,
  and the route is a `StreamingResponse`. The population is built and sorted
  before the response starts, so an unavailable source is still a 503 and never
  a started-then-empty file. The bytes are unchanged.

### 4.4 SQL attribution without a query

`leads` persists no user query (`LEADS_HAVE_SEARCH_TERM = False`), so no
contact can match any unit and every unit's state is decided by its mapping
status. The service now asks the attribution service for that state per
STATUS (`unit_state_without_text_evidence`, the rule `_attribute` itself uses)
instead of copying 100,000 units into dicts to compute 100,000 equal answers.
The condition is "no contact carries a query", read from the contacts
themselves; one contact with a query takes the ordinary per-unit path.

### 4.5 One snapshot

`population_snapshot()` runs every `search_terms` / `waste_terms` statement of
a build — grouped rows, source totals, per-date costs, annotations — on one
`REPEATABLE READ, READ ONLY` connection. Before, each reader took its own
connection: a sync committing between the grouped rows and the per-date costs
produced units whose total cost and whose per-date amounts described two
different tables, in a response that reconciled (test_48 fails on the parent
commit for exactly this). If the snapshot cannot be opened the readers fall
back to their own connections and, the database being unreachable, answer
unavailable.

### 4.6 Out of memory is not "the database is unavailable"

`_search_term_evidence_call`, `/api/waste` and the export route caught
`Exception`, so a `MemoryError` from a builder was answered with the
db-unavailable shape — a reason that is false. Each now lets it through.

## 5. Proof

`tests/test_pr_ads_173a_search_terms_pages_memory.py` (in CI's PostgreSQL step
and did-run list):

* **Equality with the parent commit, real PostgreSQL.** 364 requests — every
  route; four windows; every sort; filters; pages 2+, the last partial page,
  past the end; empty results; sort-key ties; mixed, missing and native-USD
  currency; a day with no FX rate; four spellings that normalize together
  (one NFKC); two campaign ids sharing a display name; an id under two labels;
  approved aliases; review decisions; a quarantined population; a population
  with nothing flagged; invalid parameters — publish payloads **equal as whole
  documents** to the ones captured from commit `6c1cb04` by
  `tests/pr_ads_173a_scenarios.py` and committed in `tests/fixtures/`. Nothing
  is normalized before comparing. The capture hands the old code its grouped
  rows in the order the new statement specifies (§4.1); on this dataset the
  old code's payloads were identical with and without that.
* **Measured, production-scale synthetic data.** Every route at `180d` and
  `all_time`, each in a fresh interpreter, with a limit under 150 MB and an
  assertion that the page was actually published (16 cases).
* **Failure paths.** A failed cost read and a failed FX read are unavailable on
  every route (and the export is a 503); `MemoryError` is raised by every
  builder and every route handler; a stream that breaks midway is unavailable;
  a consumer's own failure is raised; an unconfigured account reads nothing.
* **Pure.** The compact unit equals the pre-173A dict field for field after the
  same merges; the page slice equals the slice of the full sort for every sort;
  a streaming reader and a row-list reader build one population; the per-status
  attribution state equals the per-unit one; the flagged index carries every
  field the view reads from all rows.

Against the parent commit the 16 measured cases fail with, for example,
"terms at all_time added 673 MB of resident memory (limit 115 MB)", and the
snapshot case fails on a unit whose per-date amounts include a row its total
does not. Every existing test passes unmodified.

## 6. Not guarded, and why

No route needs a refusal guard at the sizes measured: the largest is 107 MB.
None was added.

## 7. Not fixed

1. **A CRM that carries exact user queries.** Not today's state. Simulated by
   giving 200 contacts a query, Terms at `all_time` costs ≈ 157 MB (673 MB
   before): the attribution service then builds one working row and one state
   dict per unit. That is above the 150 MB target and is not guarded. Whoever
   turns `LEADS_HAVE_SEARCH_TERM` on must bound `_attribute` first.
2. **Concurrent requests add up.** Nothing serialises population builds. Three
   simultaneous `all_time` Patterns requests are ≈ 320 MB on top of idle.
3. **A build holds two pooled connections at once** — the snapshot, plus one
   briefly for each `revenue_repository` read. The pool has ten.
4. **The snapshot covers `search_terms` and `waste_terms` only.** Canonical
   spend, campaign identity, FX rates, review decisions and SQL contacts are
   read on their own connections, as before.
5. **The CSV export holds the sorted population for as long as the client takes
   to download the file** (≈ 95 MB at `all_time` here). A failure while a row
   is being serialized mid-download ends the transfer without its final chunk;
   it cannot become a 503 once the response has started. (The population read,
   the FX conversion and the sort all happen before it starts.)
6. **Patterns still unifies every term of the window** (62,706 here); n-grams
   are computed in Python.
7. **The Flagged page still builds the complete population.** It could take
   `flagged_candidates`; `docs/51` §6.1 records why that is a contract
   decision.
8. **Production peaks are not measurable from here**, and production's query
   length, campaign count and keyword mix differ from the synthetic dataset's.
