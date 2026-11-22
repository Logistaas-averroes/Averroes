# 53 — Meta ad-set detail and Meta-reported lead actions (PR-ADS-171C)

Read-only. Each Meta ad set has its own budget, targeting and countries, so
the owner judges ad sets separately. Averroes now reads, per ad account, one
row **per ad set per day**: spend, impressions, clicks and Meta's own
lead-related action counts. Nothing is written to Meta, and the existing
`ads_read` token is all it needs.

This PR adds the data and its read function. **No page and no API route shows
it yet** (PR-ADS-171B / 171D). It builds on `docs/52` (PR-ADS-171A) and
changes none of its rules.

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

**The reconciliation is against the STORED campaign rows**, not a fresh
campaign read, so what is stored at the two levels agrees by construction at
the moment of the write. It can stop agreeing later: the campaign level is
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
  `meta_lead_action_type_unverified`, until one type is confirmed.
  `config/thresholds.yaml` → `meta_ads.native_lead_action_type` is `null`.
  When it is set to ONE action type, the read function publishes that type's
  count and nothing else. It is never a list.
* **The open question for the owner** is in §10 step 2: the smoke test prints
  which lead-related action types the production account really emits.

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
in `static/app.js`. The System page's "Meta Ads spend" line is unchanged.

USD: the ad-set rows are amounts on the same days in the same currency as the
campaign rows, so the rates the campaign step ensured are the rates they need.
The ad-set step fetches no rates and has no `partial` of its own for FX.

---

## 6. Backfill

```
python -m scripts.backfill_meta_ads_spend --from 2026-04-01 --level adset
```

`--level adset` backfills the ad-set detail, month by month, per account,
with the same `--dry-run`, `--resume`, `--account`, `--to` and
`--accept-zero-restatement`. **Without `--level` the command does exactly
what it did before.**

* **Run the campaign level first, for the same range.** A month whose campaign
  level is not verified fails `meta_adset_campaign_base_unverified`, stores
  nothing, and that account stops there.
* It reads the account from Meta without storing it, and writes only the two
  ad-set tables: no campaign row, no campaign ledger row, no account sync
  state, no `sync_batches` row, no `runs` row.
* `--resume` skips a month the **ad-set** ledger holds `verified` with those
  bounds. A verified campaign month does not make an ad-set month done.

---

## 7. The read function

`services/meta_ads_adset_truth_service.build_meta_adset_breakdown_for_days(start, end, *, settled=False, now=None)`

Returns `status` (`published` | `withheld` | `unavailable`), `reason`,
`detail`, `rows`, `accounts`, and metadata: `metric_family`
(`meta_reported_adset_delivery`), `window_basis`
(`meta_account_local_spend_date`), `names_are_labels`,
`native_lead_action_type`, the lead disclosure, and the owner-retirement
disclosure of `docs/52` §6 passed through unchanged.

One row per account, campaign and ad set. Each figure is
`{value, status, reason}`:

| Key | Value | Unavailable when |
|---|---|---|
| `spend` | account currency (`currency`) | — |
| `spend_usd` | each day at its own rate; the amount itself for a USD account | a spend day has no rate (`fx_rates_missing`) or the currency is not priced (`fx_currency_not_supported`) — the same rules as `docs/52` §7 |
| `impressions`, `clicks` | the sum | any day's row does not carry the figure (`meta_count_missing_on_some_days`) |
| `meta_reported_leads` | the count of the ONE configured action type | no type is configured (`meta_lead_action_type_unverified`); or a day carried no `actions` (`meta_actions_missing_on_some_days`) |
| `meta_reported_lead_actions` | `{action_type: count}`, flagged `overlapping_types` | a day carried no `actions` |

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
noted. `meta_ads_smoke_test --adsets` (§10) exercises 1–7.

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
   Meta's documentation.
6. `use_unified_attribution_setting=true` is accepted with `ads_read`, and
   `attribution_setting` is returned at `level=adset`.
7. Whether a row always carries `impressions` and `clicks` (a missing one
   makes that figure unavailable for the range, never zero). The smoke test
   prints `Rows w/o clicks`.
8. The default `action_report_time`. It is not sent. It decides which day a
   conversion is counted on; Meta restates either way, and the 28-day re-read
   follows.
9. That the value of an `actions` entry is always a whole number.
10. Timing: the campaign read and the ad-set read are two requests seconds
    apart. If Meta restates a recent day between them, that morning's ad-set
    chunk fails reconciliation, the run is `partial`, and the next run
    corrects it.

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
3. **Backfill the ad sets**, after the campaign level is backfilled for the
   same range (`docs/52` §10 step 4). Spend is stored from 2026-04-20:

   ```
   python -m scripts.backfill_meta_ads_spend --from 2026-04-20 --level adset --dry-run
   python -m scripts.backfill_meta_ads_spend --from 2026-04-20 --level adset
   ```

   Exit 0 = every month of both accounts verified at ad-set level. Re-run
   with `--resume` after an interruption. A month that fails with
   `meta_adset_campaign_base_unverified` needs the campaign backfill first; a
   month that fails with `meta_adset_reconciliation_mismatch` names the day
   and campaign id — report it (VERIFY 3 or 4).
4. **Once the lead action type is confirmed**, set
   `meta_ads.native_lead_action_type` in `config/thresholds.yaml` to that one
   type. Until then `meta_reported_leads` stays unavailable and the raw types
   are stored, so nothing has to be re-read when it is set.

---

## 11. Decisions to review

* **`meta_reported_leads` is unavailable until one action type is confirmed.**
  Meta's documentation does not name it; guessing would publish a number
  under a name it may not deserve.
* **A row with no `actions` key is unknown, and one such row withholds the
  range's lead figures** (VERIFY 2).
* **Lead-related means "the action type contains `lead`".** A custom
  conversion whose name contains "lead" would be kept too; it is stored under
  its own type and is never published as `meta_reported_leads` unless it is
  the one configured type.
* **The ad-set step is a sibling with its own batch**, so its failure is a
  `failed` or `partial` dataset in a `partial` run and never changes the
  campaign dataset's status or proven-through date.
* **One account short at ad-set level withholds the breakdown for every
  account.**
* **Ad-set rows are reconciled to the stored campaign rows, and re-checked at
  read time.** A morning on which Meta restates between the two reads leaves
  the ad-set detail withheld for the affected range until the next run.
* **`clicks`, not `inline_link_clicks`** — the same figure the campaign rows
  hold.
* **No name history** (§8).
