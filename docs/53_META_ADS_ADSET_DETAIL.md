# 53 — Meta ad-set detail and Meta-reported lead actions (PR-ADS-171C)

Read-only. Each Meta ad set has its own budget, targeting and countries, so
the owner judges ad sets separately. Averroes now reads, per ad account, one
row **per ad set per day**: spend, impressions, clicks and Meta's own
lead-related action counts. Nothing is written to Meta, and the existing
`ads_read` token is all it needs.

This PR adds the data and its read function. **No page and no API route shows
it yet** (PR-ADS-171B / 171D). It builds on `docs/52` (PR-ADS-171A) and
changes none of its rules.

**PR-ADS-171D** (2026-11-23) made the ad-set backfill work on the live
account: each month re-reads its campaign level first and reconciles the ad
sets to that live read (§3, §6), an end past the last verifiable day stops
there instead of failing (§6), the native lead action type is set on
production evidence (§4), and the attribution setting is reported beside the
lead counts (§7). §10 is the corrected owner procedure.

---

## 1. What is read

One more GET per account per run, against the same Insights edge as 171A:

`act_{id}/insights` with `level=adset`, `time_increment=1`,
`time_range={"since","until"}`, `use_unified_attribution_setting=true`,
`limit=500` and
`fields=adset_id,adset_name,campaign_id,campaign_name,spend,impressions,clicks,actions,attribution_setting,account_currency,date_start,date_stop`.

Everything in `docs/52` §1 holds unchanged, because it is the same code path
(`connectors/meta_ads.py::_request`, `_pages`): GET only, the token only in
the `Authorization` header, `paging.next` never requested, exact decimal
micros, the retry table, today never fetched, days of the account's own zone.

| Field | What Meta says it is | Stored as |
|---|---|---|
| `spend` | — | `cost_micros`, `spend_raw` |
| `clicks` | "The number of clicks on your ads." | `clicks` |
| `impressions` | — | `impressions` |
| `actions` | "The total number of actions Accounts Center accounts took that are attributed to your ads. Actions may include engagement, clicks or conversions." | `meta_reported_lead_actions` (§4) |
| `attribution_setting` | "The default attribution window to be used when attribution result is calculated. Each ad set has its own attribution setting value." | `attribution_setting` |

**Clicks are `clicks`, not `inline_link_clicks`.** `clicks` is every click on
the ad and is the field the campaign rows already store. Meta describes
`inline_link_clicks` as "The number of clicks on links to select destinations
or experiences … Inline link clicks use a fixed 1-day-click attribution
window" — a narrower count on a different window. It is not read.

**`use_unified_attribution_setting=true`** — Meta: "your ads results will be
shown using unified attribution settings defined at ad set level". So
`actions` are counted the way Ads Manager shows them for each ad set, and the
setting used is returned on the row.

(Source for every quotation in this section:
`https://developers.facebook.com/documentation/ads-commerce/marketing-api/reference/ad-campaign/insights`,
read 2026-11-22.)

---

## 2. Storage

Two new tables, both keyed by `account_id`, written only by the ad-set sync.

| Table | Holds |
|---|---|
| `meta_ads_adset_daily_spend` | one row per account, ad set and account-local day: `adset_id`, `adset_name`, `campaign_id`, `campaign_name`, `cost_micros`, `spend_raw`, `currency_code`, `impressions`, `clicks`, `meta_reported_lead_actions` (JSONB), `attribution_setting` |
| `meta_ads_adset_coverage` | the ad-set fetch ledger: one row per fetched range, `verified` or `failed`, with the failure's reason, pages read, restated days and `rows_without_actions` |

### Why a second ledger, not the existing one

The requirement is that the ad-set level can **never** demote or block
campaign-level coverage. Reusing `meta_ads_spend_coverage` would mean a
`level` column, a rewritten unique key, and a `level = 'campaign'` filter in
every existing reader and writer of that table — and one missed filter would
let an ad-set row decide whether campaign spend is published. With its own
table the guarantee is structural: no statement of the ad-set writer or the
ad-set failure recorder names a campaign table
(`tests/test_pr_ads_171c_meta_adsets.py::test_28` asserts exactly that, and
the PostgreSQL module asserts every campaign table is byte-identical across
each kind of ad-set failure).

For the same reason the ad-set sync writes nothing to
`meta_ads_account_sync_state`: an ad-set read Meta refuses does not mark the
account "not readable" (its campaign level was read a moment earlier), and so
cannot start the retirement logic of `docs/52` §6.

### The write

`db/writers.py::replace_meta_adset_chunk` — the same shape as the campaign
writer. One transaction: validate every row; read the range's per-day sums;
**delete the range; insert the response**; record the range `verified` with
`fetched_at = clock_timestamp()`. Any failure rolls all of it back.

* A range is **replaced, never merged**: an ad-set-day Meta stops reporting
  stops counting.
* An empty response is a **verified nothing** for a range that held no ad-set
  spend.
* **The zero-restatement rule applies**: a day a verified ad-set chunk covers,
  with stored ad-set spend above zero, is not taken to nothing by a re-read
  (`meta_restatement_implausible`). The backfill's
  `--accept-zero-restatement` is the only override.
* A **failed** read stores nothing, deletes nothing, and records a `failed`
  row in the ad-set ledger — which never replaces a `verified` ad-set row for
  the same range.
* A day with no verified ad-set chunk is **unknown**, not zero.

---

## 3. Reconciliation: the campaign level is the base

An ad-set chunk is `verified` only when paging was exhausted **and**

1. every day of the range is inside a `verified` **campaign** chunk
   (otherwise `meta_adset_campaign_base_unverified` — there is nothing to
   reconcile against); and
2. for every day **and every campaign**, the ad-set rows sum to the stored
   campaign-level row within the existing tolerance — one minor unit of the
   account currency (`services/meta_ads_adset_service.py::reconcile_adsets_to_campaigns`).
   A `(day, campaign)` present on one side only is compared with zero on the
   other.

A mismatch fails the whole chunk with `meta_adset_reconciliation_mismatch`,
names the first day and campaign id, and stores nothing. Equal per campaign
per day implies equal per day, and so equal to the account-level total the
campaign rows were themselves reconciled to (`docs/52` §5). The check is per
campaign on purpose: two campaigns whose errors cancel across a day do not
reconcile.

This is the guard against an ad set the `level=adset` read leaves out (an
archived or deleted one, if Meta does that — VERIFY 3): its spend would be in
the campaign row and missing from the ad-set rows.

**The reconciliation is against the STORED campaign rows**, so what is stored
at the two levels agrees by construction at the moment of the write. Both
callers make that stored row a fresh one: the daily step reads the ad sets
right after the campaign step re-read the same window in the same run, and
— since PR-ADS-171D — the backfill re-reads the campaign level of the same
month immediately before the ad-set read (§6). Before 171D the backfill
compared a live ad-set read with a campaign row stored by an EARLIER run, so
any restatement inside Meta's adjustment window failed the month: on
2026-11-23, 21 Nov of campaign 6951621703494 read 61,320,000 micros at
ad-set level against a stored 61,300,000 (read the previous morning) — Meta
had restated the day by $0.02. It can stop agreeing later: the campaign level is
re-read every morning, and if that morning's ad-set read then fails, the
ad-set rows still describe the previous campaign figures while their ledger
row still says `verified`. So the read function re-checks, in the database,
at read time (§7) — the ledger alone cannot see this.

---

## 4. Leads

### What Meta's documentation says

* The Insights reference describes `actions` only as "The total number of
  actions Accounts Center accounts took that are attributed to your ads.
  Actions may include engagement, clicks or conversions." **It does not list
  the action types.** The word "lead" does not occur anywhere on the ad-set
  or ad-account Insights reference pages.
  (`https://developers.facebook.com/documentation/ads-commerce/marketing-api/reference/ad-campaign/insights`)
* The type of each entry is `AdsActionStats`. Its reference page
  (`https://developers.facebook.com/docs/marketing-api/reference/ads-action-stats`)
  could not be retrieved from the build environment on 2026-11-22: it
  answered "Sorry, something went wrong", and the new documentation site
  served only its navigation for that path.
* The Breakdowns page says the entries of `actions` **overlap**: "This result
  may not equal `total_actions` since fields returned in `actions` are
  hierarchical and include detailed actions not counted", with the example
  that "`post_engagement` is a sum of `link_click`, `comment`, `like`, and
  `post_reaction`".
  (`https://developers.facebook.com/documentation/ads-commerce/marketing-api/insights/breakdowns`)
* Counting actual form submissions through the Leads edge is not possible
  with this token: "To retrieve lead data … You must include the
  `leads_retrieval` and `pages_manage_ads` permissions."
  (`https://developers.facebook.com/documentation/ads-commerce/marketing-api/guides/lead-ads`)
  Insights `actions` is the only lead count `ads_read` gives.

### The decision

Meta's own documentation does **not** make the choice unambiguous: it names no
action type as "the count of native lead-form submissions", and it says the
types are hierarchical. So:

* **No named lead column is stored.** `meta_reported_lead_actions` holds the raw count of
  every action type whose name contains `lead` (any letter case), one entry
  per type, exactly as Meta returned it. That is a rule, not a list — no list
  written here could be shown to be complete — and it keeps `lead`,
  `leadgen_grouped`, `onsite_conversion.lead_grouped`,
  `offsite_conversion.fb_pixel_lead` and anything else of that family.
* **The types are never added together.** Not in the connector, the writer,
  the repository or the read function. A native-form submission that Meta
  reports as `lead = 3`, `leadgen_grouped = 3` and
  `onsite_conversion.lead_grouped = 3` is three leads, not nine.
* **`meta_reported_leads` is unavailable**, with the reason
  `meta_lead_action_type_unverified`, while no type is configured. When
  `config/thresholds.yaml` → `meta_ads.native_lead_action_type` names ONE
  action type, the read function publishes that type's count and nothing
  else. It is never a list. **PR-ADS-171D set it to
  `onsite_conversion.lead_grouped`** — see "The production evidence" below.
* **A configured type is not thereby evidence** (review round 1). A type the
  sync never stores — one without `lead` in it, e.g. a custom conversion's
  `offsite_conversion.custom.<id>` — is refused: `meta_reported_leads` is
  unavailable (`meta_lead_action_type_not_stored`), the string is not echoed,
  and the read function's `config_problems` carries a fixed message. A type
  that is stored-shaped but not the exact key (`Lead`, `leads`) would be
  absent from every row and read as 0 on every ad set. So **0 is published
  only when that exact key appears in that account's stored ad-set rows on
  at least one day** (any day, not only the range — decided in the same
  snapshot as the rows); otherwise unavailable,
  `meta_lead_action_type_not_observed`. This is per account: in a range over
  two accounts, the one that has never emitted the type is unavailable for
  its rows while the other publishes, and any future total over both must be
  unavailable too. `accounts[].meta_reported_leads_type_observed` says which.
* **The open question for the owner** was §10 step 2: which lead-related
  action types the production account really emits. Answered on
  2026-11-23 (below).

### The production evidence (PR-ADS-171D, 2026-11-23)

The owner's ad-set backfill of 2026-11-23 (account 944370805110952, USD,
Asia/Amman, read from the real output on the Render shell) stored, on every
row and in every month, **five lead action types with identical counts**:
`lead`, `onsite_conversion.lead_grouped`,
`offsite_complete_registration_add_meta_leads`,
`offsite_content_view_add_meta_leads` and `offsite_search_add_meta_leads`
(e.g. July 2026: 311 each; October 2026: 235 each). The ads use **Meta native
lead forms only** (the owner's statement). Five types that never differ, on
an account with one lead mechanism, are five names for one count — which is
exactly why they are never added together (that would have published 1,555
for July instead of 311).

`onsite_conversion.lead_grouped` is the one chosen: it is Meta's on-Facebook
lead counter — leads submitted on Meta's own (native) forms, what Ads
Manager shows as on-Facebook leads — which is the only lead mechanism these
ads use. The other four are wider or offsite-named families that happen to
equal it here. This rests on the production observation above and the
owner's statement about the forms; Meta's `AdsActionStats` reference, which
would define the types, still could not be retrieved (above), so if the
account ever adds website leads the choice must be revisited, not assumed.

The 171C rules are unchanged: the configured type publishes **only for an
account whose stored rows have carried that exact key on some day**
(otherwise `meta_lead_action_type_not_observed`, never 0), one type's count
only, and a day without `actions` withholds the range's figure.

**Attribution, production observation (2026-11-23):** the same output
reported `attribution_setting = 1d_view_7d_click` — one-day view, seven-day
click. It is recorded here as an observation of that account on that day, not
a constant: the read function reports what the stored rows carry
(`meta_reported_leads_attribution`, §7), and says "not stored" when no row
carries one.

### Unknown is not zero

| The row Meta returned | Stored `meta_reported_lead_actions` | Means |
|---|---|---|
| has no `actions` key | SQL `NULL` | Meta said nothing about actions for that ad set that day. **Unknown.** |
| has `actions`, none a lead type (including `[]`) | `{}` | Meta reported actions and none was a lead. A reported nothing. |
| has `actions` with lead types | `{"lead": 3, …}` | Meta's counts, per type |

An `actions` that is not a list of `{action_type, value}`, a type given twice
on one row, or a count that is not a whole number fails the read
(`meta_malformed_response`) — it is never read as zero.

Over a range, a figure is published only when **every** day's row carried
`actions`; one row without them makes the range's lead figures
`unavailable` (`meta_actions_missing_on_some_days`), never the sum of the
days that did. If Meta turns out to omit `actions` whenever an ad set had no
action at all (VERIFY 2), that rule will withhold more than it needs to, and
is the thing to revisit — with evidence, not by assuming.

### What these numbers are, and are not

They are **Meta's own attributed counts under each ad set's attribution
setting**. They are named `meta_reported_leads` / `meta_reported_lead_actions`
everywhere. They are not contacts, not leads in the CRM, not MQLs and not
SQLs. Nothing in this PR adds them to, subtracts them from, divides them by or
places them in one total with a HubSpot count; the ad-set modules import no
CRM, cohort or revenue code (`test_11`).

---

## 5. The daily sync

`scheduler/incremental_sync.py::_sync_meta_ads_adsets` is a **sibling step**,
run immediately after `_sync_meta_ads_spend`, over the **same window** each
account's campaign level was just synced for (the last 28 complete days of
that account's own zone).

It has its own dataset key (`meta_ads_api/meta_adset_spend`), its own
`sync_batches` row and its own ledger. By the time it starts, the campaign
batch is already closed with the campaign's own status and proven-through
date. That is why it is a sibling and not a branch inside the campaign step:
folded into one batch, a failed ad-set read would have made the campaign
dataset's batch `partial` and withheld its proven-through date — demoting
campaign freshness for a reason that has nothing to do with campaign spend.

For each account of the campaign step's result:

* campaign chunk **verified** → read the ad sets, reconcile (§3), replace;
* campaign chunk **not verified** in this run → the ad sets are not requested
  (there is no base); the range is recorded `failed` in the ad-set ledger
  with `meta_adset_campaign_base_unverified`.

| Outcome | Ad-set step | The run |
|---|---|---|
| every account's ad sets verified | `success` | unaffected |
| some verified, some not | `partial` — batch `partial` | `partial` |
| none verified, campaign level verified | `failed` — batch `failed` | **`partial`** — one dataset succeeded, one did not; never `success`, never `failed` |
| Meta not configured | `skipped` (no batch) | does not vote |

No new run type (`RUN_TYPE_CADENCE` is unchanged — the step is part of
`daily_incremental_sync`) and no new freshness status. The pair is registered
where 171A's is: `services/dataset_keys.py`, `ACTIVE_SYNC_PAIRS`,
`services/freshness_service.py` (`meta_ads_adsets`, depending on
`meta_ads_spend`), `services/system_status_service.py`, and the dataset label
in `static/app.js`. The System page's "Meta Ads spend" line itself is
unchanged. Its "Meta Ads API" source card, and the worst-of verdict over the
sources, now also reflect ad-set health: `meta_ads_adsets` is a dataset of
that source (`services/system_status_service.py`, `SOURCE_DEFINITIONS`), so a
failed or stale ad-set step shows there even while campaign spend is current.

USD: the ad-set rows are amounts on the same days in the same currency as the
campaign rows, so the rates the campaign step ensured are the rates they need.
The ad-set step fetches no rates and has no `partial` of its own for FX.

---

## 6. Backfill

```
python -m scripts.backfill_meta_ads_spend --from 2025-01-01 --level adset
```

`--level adset` backfills the ad-set detail, month by month, per account,
with the same `--dry-run`, `--resume` and `--account`. **Without `--level`
the command does exactly what it did before.**

PR-ADS-171D changed how each month is loaded
(`services/meta_ads_spend_service.py::_backfill_adset_chunk`):

1. **The campaign level of the same account and days is re-read first**, in
   the same run, through the campaign chunk sync of 171A (`sync_chunk`):
   reconciled to the account-level total, replaced atomically, recorded in
   the campaign ledger, and refused by `MetaRestatementImplausible` if it
   would take a day with verified spend to zero. Nothing is bypassed.
2. **Only if that re-read is `verified`** are the ad sets read and
   reconciled per day and campaign (§3) — to the rows step 1 stored seconds
   earlier. Live to live, at the same one-minor-unit tolerance.
3. **A re-read that is not `verified` fails the ad-set month with the
   re-read's own reason** (e.g. `meta_reconciliation_mismatch`,
   `meta_restatement_implausible`): no ad set is requested, no ad-set row is
   stored, a `failed` row goes to the ad-set ledger (never replacing a
   verified one), and that account stops there. A re-read whose USD rates are
   missing leaves the month verified and the run `partial`, as at campaign
   level.

**The end.** The last day the campaign level can be verified is the
account's local yesterday — today is in progress and is never read. That is
the default end. A `--to` later than it is cut at it and reported, per
account: `requested_to`, `campaign_verifiable_through`, `stopped_at`,
`stop_reason` (`meta_adset_end_after_last_verifiable_day`) and `stop_detail`.
The days before it are stored normally; no day after it is read or stored.
Before 171D the newest day of a month could be one the daily sync had not yet
read (2026-11-23, 04:2x UTC: the end was 22 Nov, the campaign level stood at
21 Nov), and the whole month failed `meta_adset_campaign_base_unverified`.

* So the campaign backfill no longer has to run first, and `--to` is never
  needed.
* `--accept-zero-restatement` at `--level adset` lifts the refusal for
  **ad-set rows only**. The campaign re-read never takes it: a campaign day
  going to zero is accepted, if ever, by the campaign-level backfill with
  that flag, after checking in Meta.
* Because it now writes the campaign level, it handles the account exactly
  as the campaign backfill does: it stores it through `refresh_account`,
  which refuses a currency or zone change for an account with stored spend,
  and an account read that fails is recorded in `meta_ads_account_sync_state`
  as the campaign backfill records it. Like every backfill it writes no
  `sync_batches` row, no `sync_state` and no `runs` row.
* `--resume` skips a month the **ad-set** ledger holds `verified` with those
  bounds (and so re-reads nothing for it). A verified campaign month does
  not make an ad-set month done.

The daily step was never affected: it reads the campaign level and then the
ad sets of the same window in one run, minutes apart
(`test_pr_ads_171d_meta_adset_backfill_pg_integration.py::test_08`).

---

## 7. The read function

`services/meta_ads_adset_truth_service.build_meta_adset_breakdown_for_days(start, end, *, settled=False, now=None)`

Returns `status` (`published` | `withheld` | `unavailable`), `reason`,
`detail`, `rows`, `accounts`, and metadata: `metric_family`
(`meta_reported_adset_delivery`), `window_basis`
(`meta_account_local_spend_date`), `names_are_labels`,
`native_lead_action_type` (None when refused), `config_problems`, the lead
disclosure, and the owner-retirement
disclosure of `docs/52` §6 passed through unchanged.

One row per account, campaign and ad set. Each figure is
`{value, status, reason}`:

| Key | Value | Unavailable when |
|---|---|---|
| `spend` | account currency (`currency`) | — |
| `spend_usd` | each day at its own rate; the amount itself for a USD account | a spend day has no rate (`fx_rates_missing`) or the currency is not priced (`fx_currency_not_supported`) — the same rules as `docs/52` §7 |
| `impressions`, `clicks` | the sum | any day's row does not carry the figure (`meta_count_missing_on_some_days`) |
| `meta_reported_leads` | the count of the ONE configured action type; 0 only when that exact type is in the account's stored rows on some day | no type is configured (`meta_lead_action_type_unverified`); the type is one the sync never stores (`meta_lead_action_type_not_stored`); the account has never carried it (`meta_lead_action_type_not_observed`); or a day carried no `actions` (`meta_actions_missing_on_some_days`) |
| `meta_reported_lead_actions` | `{action_type: count}`, flagged `overlapping_types` | a day carried no `actions` |
| `attribution_settings`, `attribution_setting_missing_rows` | the distinct settings the row's stored days carry, and how many stored none | — (never inferred) |

**`meta_reported_leads_attribution`** (PR-ADS-171D, payload metadata beside
`native_lead_action_type`): `{settings, rows_without_setting, status,
detail}` over the published rows, read from the stored `attribution_setting`
column only. `status` is `stored`, `partly_stored`, `not_stored` (with
`detail` "not stored: …" — no default is ever filled in) or `no_rows`. It is
`None` when the breakdown is not published: withheld means absent.

**Which accounts a range needs, through which day, and what a retirement
declaration means are not re-implemented**: the function calls
`meta_ads_spend_truth_service.build_meta_ads_spend_truth_for_days` and uses
its `accounts` and each one's `required_through`. Then:

| Check | If it fails |
|---|---|
| the campaign-level truth publishes the range | `meta_campaign_spend_not_published` |
| every relevant account's **ad-set** ledger is verified for every required day (for all time: from that account's first verified **campaign** day) | `withheld` — `meta_adset_coverage_incomplete` |
| with `settled`: those days are held completely in that account's zone | `withheld` — `meta_adset_days_not_settled` |
| the stored ad-set rows still sum to the stored campaign rows, per day and campaign — one SQL statement, in the same REPEATABLE READ snapshot as the rows | `withheld` — `meta_adset_reconciliation_mismatch` |
| the tables can be read | `unavailable` — `meta_adset_source_unavailable` |

**Withheld means absent**: `rows` is empty unless `status` is `published`.
And one account short withholds **every** account — a breakdown with an
account missing would not add up to the published campaign spend.

A consumer (171B/171D) must show `owner_declared_retired_disclosure` beside
these figures when it is set, exactly as `docs/52` §6 requires for the total.

---

## 8. Names are labels

**Ids are the identity. A name is never a join key** — not for an ad set, not
for a campaign. (HubSpot shows 992 contacts under a campaign name that no
longer has spend in Meta: a rename. A join on names would have lost them.)

Precisely what is stored:

* Insights returns the name the ad set and its campaign have **now**, on
  every row, whatever the row's day. It does not return the name as of that
  day.
* `adset_name` and `campaign_name` on a stored row are therefore **the name
  Meta held when that row was last fetched**. Rows inside the daily 28-day
  window carry the current name; an older row keeps the name from the day it
  was last read, until a backfill re-reads it.
* A row that arrives with no name keeps the newest name stored for its id.
* The read function returns, for each id, the **newest stored label** (over
  the whole table, by `fetched_at`), so a renamed ad set is one row under its
  new name with all of its spend — never two ad sets.
* No SQL in the ad-set writer or readers joins, groups or filters on a name
  (`test_24`), and reconciliation is keyed by `(day, campaign_id)`.

There is no history of names: the name an ad set had on a past day is not
recorded and cannot be recovered from Insights.

---

## 9. What is not verified against the live API

Each is either guarded by a failure that says so, or affects only what is
noted. `meta_ads_smoke_test --adsets` (§10) exercises 1, 2 and 4–7. It reads
yesterday only, so it does **not** exercise 3 (archived or deleted ad sets);
the ad-set backfill does, and fails that month loudly with
`meta_adset_reconciliation_mismatch` rather than storing a short detail.

1. `level=adset` with `time_increment=1` returns `adset_id`, `adset_name`,
   `campaign_id`, `campaign_name`, `spend`, `impressions`, `clicks`,
   `attribution_setting` as requested. A row without `adset_id` or
   `campaign_id` fails the read.
2. **Whether Meta omits the `actions` key for an ad-set-day with no action
   at all.** If it does, those days read as unknown and the range's lead
   figures are withheld (§4). The smoke test prints `Rows w/o actions`.
3. Whether `level=adset` includes archived and deleted ad sets' spend. If
   not, reconciliation fails that range loudly (§3). Check a month that
   contains one before backfilling.
4. The ad-set rows sum to the campaign row within one minor unit. If Meta
   rounds each row, a campaign with many ad sets could differ by more.
5. **Which `action_type` is the count of native lead-form submissions**, and
   which lead types this account emits at all (§4). Not answerable from
   Meta's documentation. *Answered by production observation on
   2026-11-23: five identical types; `onsite_conversion.lead_grouped`
   configured (§4).*
6. `use_unified_attribution_setting=true` is accepted with `ads_read`, and
   `attribution_setting` is returned at `level=adset`. *Observed on
   2026-11-23: `1d_view_7d_click` (§4).*
7. Whether a row always carries `impressions` and `clicks` (a missing one
   makes that figure unavailable for the range, never zero). The smoke test
   prints `Rows w/o clicks`.
8. The default `action_report_time`. It is not sent. It decides which day a
   conversion is counted on; Meta restates either way, and the 28-day re-read
   follows.
9. That the value of an `actions` entry is always a whole number. One
   malformed `actions` entry — a null entry, a fractional count on a lead
   type, any type (lead or not) given twice on a row — fails the whole chunk
   (`meta_malformed_response`), including that chunk's ad-set spend: loud,
   not wrong.
10. Timing: the campaign read and the ad-set read are two requests seconds
    apart — in the daily step, and since PR-ADS-171D in the backfill too. If
    Meta restates a recent day between them, that ad-set chunk fails
    reconciliation (the daily run is `partial` and the next run corrects it;
    a backfill month fails and a rerun corrects it).

`docs/52` §9 items 1–14 are unchanged and still open as listed there.

---

## 10. Owner steps

1. **Deploy** this PR. The two tables are created on startup. The next 06:00
   UTC sync reads ad sets for the last 28 days of each account.
2. **Smoke test** (Render shell; reads no database, writes nothing):

   ```
   python -m scripts.meta_ads_smoke_test --adsets
   ```

   Expected, with the real figures in place of these:

   ```
   API version      : v27.0 (from config)
   Credential set 1 : token set; accounts 4730305973961806; app secret proof not sent
   Credential set 2 : token set; accounts 944370805110952; app secret proof not sent
   --- Ad account 4730305973961806 ---
   Yesterday        : 2026-11-20 (America/Los_Angeles)
   Ad sets found    : 0 in 0 campaign(s), 0 row(s), 1 page(s)
   Ad-set sum       : 0.00 GBP
   Campaign total   : 0.00 GBP
   Rows w/o actions : 0 of 0 (no `actions` key: unknown, not zero)
   Rows w/o clicks  : 0 of 0; w/o impressions: 0
   Attribution      : not returned
   Lead action types: 0 (Meta-reported; overlapping — never add them together)
   RESULT           : OK — every campaign's ad-set rows sum to its campaign row
   --- Ad account 944370805110952 ---
   Yesterday        : 2026-11-21 (Asia/Amman)
   Ad sets found    : <n> in 2 campaign(s), <n> row(s), 1 page(s)
   Ad-set sum       : <x> USD
   Campaign total   : <x> USD
     campaign <id>: ad sets <a>, campaign row <a> USD
     campaign <id>: ad sets <b>, campaign row <b> USD
   Rows w/o actions : <n> of <n> (no `actions` key: unknown, not zero)
   Rows w/o clicks  : <n> of <n>; w/o impressions: <n>
   Attribution      : 7d_click_1d_view
   Lead action types: <n> (Meta-reported; overlapping — never add them together)
     lead = <n>
     onsite_conversion.lead_grouped = <n>
     ...
     ad set <id> (campaign <id>): spend <x> USD; lead=<n>; onsite_conversion.lead_grouped=<n>
     ad set <id> (campaign <id>): spend <x> USD; actions present, no lead type
   RESULT           : OK — every campaign's ad-set rows sum to its campaign row
   === 2 of 2 account(s) OK ===
   ```

   Exit code 0. **Send back the whole output.** Three things in it decide what
   happens next:

   * the `Lead action types` lines — which one equals the number of form
     submissions Ads Manager shows for yesterday (VERIFY 5);
   * `Rows w/o actions` — whether it is ever above zero (VERIFY 2);
   * `RESULT`. A `MISMATCH` line means the ad-set rows do not add up to the
     campaign rows: **do not backfill ad sets**; report it.

   It prints ids, never names, and never a token.
3. **Backfill the ad sets** (corrected by PR-ADS-171D). **Use the SAME
   `--from` as the campaign backfill** (the owner ran it with
   `--from 2025-01-01`). All-time ad-set detail counts from each account's
   first verified CAMPAIGN day (§7), so a later ad-set `--from` leaves
   all-time ad-set detail withheld (`meta_adset_coverage_incomplete`) for
   good. **Do not pass `--to`**, and it does not matter whether the morning
   sync has run yet:

   ```
   python -m scripts.backfill_meta_ads_spend --from 2025-01-01 --level adset --dry-run
   python -m scripts.backfill_meta_ads_spend --from 2025-01-01 --level adset --resume
   ```

   `--resume` keeps the months already verified on 2026-11-23 (Jan 2025 –
   Oct 2026) and loads the rest. Each month re-reads its campaign level
   first, so the campaign backfill does not have to run first, and the end is
   each account's local yesterday — the last day the campaign level can be
   verified. (If a `--to` later than that is ever given, the run stops there
   and prints `stopped_at` with the reason; that is not a failure.)

   Exit 0 = every month of both accounts verified at ad-set level. Re-run
   with `--resume` after an interruption.

   **A recent-day mismatch can no longer come from the script's timing.**
   The 2026-11-23 failures (a month ending on a day the campaign level did
   not yet hold; a campaign row stored the previous morning that Meta had
   since restated by $0.02) cannot recur: the campaign level of every month
   is read again seconds before its ad sets. A month still fails, and names
   the day and campaign id, only when:

   * Meta restates that day in the **seconds between** the month's campaign
     re-read and its ad-set read — rerun with `--resume`;
   * the ad-set rows really do not add up to the campaign rows — an ad set
     `level=adset` leaves out (VERIFY 3) or per-row rounding (VERIFY 4):
     `meta_adset_reconciliation_mismatch` on every rerun. Report it;
   * the campaign re-read itself fails — the month fails with the campaign
     level's reason (`meta_reconciliation_mismatch`,
     `meta_restatement_implausible`, a Meta error). Report it; for
     `meta_restatement_implausible` check in Meta first, then (only if the
     day really has no spend) run the CAMPAIGN level with
     `--accept-zero-restatement` for that range — the ad-set level never
     overrides it.
4. **Lead action type — done in PR-ADS-171D.** `meta_ads.native_lead_action_type`
   in `config/thresholds.yaml` is `onsite_conversion.lead_grouped` (§4).
   Nothing has to be re-read: the raw types were stored all along, and the
   read function now publishes that one type's count for an account whose
   stored rows carry it.

---

## 11. Decisions to review

* **`meta_reported_leads` is unavailable until one action type is confirmed.**
  Meta's documentation does not name it; guessing would publish a number
  under a name it may not deserve. *PR-ADS-171D: confirmed by production
  observation — `onsite_conversion.lead_grouped` (§4).*
* **The ad-set backfill re-reads the campaign level of each month** (PR-ADS-171D)
  instead of trusting a campaign row from an earlier run, and stops at the
  last verifiable day instead of failing the month. It therefore writes the
  campaign tables — only through the campaign chunk sync, never with the
  zero-restatement override.
* **A row with no `actions` key is unknown, and one such row withholds the
  range's lead figures** (VERIFY 2).
* **Lead-related means "the action type contains `lead`".** Whether a custom
  conversion is ever kept is **unproven**: its `action_type` carries its id
  (`offsite_conversion.custom.<id>`), not its name, so one named "Lead" is
  most likely not stored. A configured type the rule would not keep is
  refused (§4).
* **The ad-set step is a sibling with its own batch**, so its failure is a
  `failed` or `partial` dataset in a `partial` run and never changes the
  campaign dataset's status or proven-through date.
* **One account short at ad-set level withholds the breakdown for every
  account.**
* **Ad-set rows are reconciled to the stored campaign rows — read seconds
  earlier in the same run, by the daily step and the backfill alike — and
  re-checked at read time.** A morning on which Meta restates between the two
  reads leaves the ad-set detail withheld for the affected range until the
  next run.
* **`clicks`, not `inline_link_clicks`** — the same figure the campaign rows
  hold.
* **No name history** (§8).
