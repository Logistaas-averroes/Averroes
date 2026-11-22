# 52 — Meta Ads spend connector (PR-ADS-171A)

Read-only. Averroes reads how much each Meta (Facebook/Instagram) ad account
spent per campaign per day, stores it, and can say for any range of days
whether that spend may be published and what it is. Nothing is ever written to
Meta.

This PR adds the data and its truth function. **No page shows Meta spend yet**
— the Channels page is PR-ADS-171B. The System page gains one line.

**PR-ADS-171B** puts the spend on the Channels page and the Dashboard's
Channels tab as a separate "Meta Ads (spend)" line, read only through
`build_meta_ads_spend_truth_for_days` (via
`services/meta_ads_spend_line_service.py`), with the retirement disclosure
beside the figure and no ratio to any HubSpot count:
`docs/55_META_SPEND_ON_CHANNELS.md`.

**PR-ADS-171C** adds the ad-set detail of this spend — one row per ad set per
day, with Meta's own lead-related action counts — in its own tables, ledger
and sync step: `docs/53_META_ADS_ADSET_DETAIL.md`. Nothing in this document
changes. The campaign level described here stays the published base; the
ad-set level is reconciled to it and can never demote it.

---

## 1. What is read, and how

Three GET requests per account per run, against
`https://graph.facebook.com/{version}/`:

| Read | Request | Used for |
|---|---|---|
| Account | `act_{id}?fields=account_id,name,currency,timezone_name,timezone_offset_hours_utc,account_status` | the account's currency and time zone |
| Campaign days | `act_{id}/insights` with `level=campaign`, `time_increment=1`, `time_range={"since","until"}`, `fields=campaign_id,campaign_name,spend,impressions,clicks,account_currency,date_start,date_stop`, `limit=500` | the stored rows |
| Account days | the same with `level=account`, `fields=spend,account_currency,date_start,date_stop` | the total each day's campaign rows must add up to |

* **GET only.** `connectors/meta_ads.py::_request` refuses any other method
  before it resolves a credential. Asynchronous report runs are a POST and are
  not used.
* `spend` is a decimal string. It becomes `cost_micros` as
  `Decimal(spend) × 1,000,000`, exactly — never through a float
  (`int(float("8.20") × 1e6)` is 8,199,999). The string is kept in `spend_raw`.
  A missing, non-numeric or negative amount is an error, never a zero.
* **Paging** ends only on a page with no `paging.next`. The next page is our
  own query plus `after=<paging.cursors.after>`. More than 200 pages, a 200
  without a `data` list, a campaign-day returned twice, a row outside the
  requested days, or a row spanning more than one day all fail the read.
* **Today is never fetched.** A range ends on the account's own yesterday.

### The access token

Meta's `paging.next` is a complete URL with the access token in it, and an
exception raised by `requests` prints the URL it was calling. So:

* a token is sent **only** in the `Authorization: Bearer` header;
* `paging.next` is never requested, stored or logged — only its presence is
  read;
* no error text contains the **request URL** or `str(exc)` of a transport
  error. A transport failure is named by its class; an API failure by the HTTP
  status and Meta's `error` fields. `error.code` and `error_subcode` are used
  only as integers. Meta's own `message` text **is** included — it is Meta's
  wording, not ours, so it can say anything Meta chooses to say — after every
  configured token and app secret, and anything shaped like a token parameter,
  has been removed from it, and it is cut to 300 characters.

`tests/test_pr_ads_171a_meta_ads_connector.py::test_47j` pushes two different
tokens through every failure path of both credential sets and finds neither in
any log, error, result or stored value. The fake API refuses a request that
carries a token anywhere but the header, or that calls an account with another
set's token.

### Retries and refusals

| Meta answers | The connector |
|---|---|
| transport error; HTTP 5xx; HTTP 429; `error.code` 4, 17, 32, 613, 80000, 80004; `error.is_transient` | retries — at most 4 attempts, waits 2s, 4s, 8s, or Meta's `estimated_time_to_regain_access` if longer, never more than 60s — then fails `meta_retries_exhausted` |
| `error.code` 190 | fails at once: `Meta access token expired or invalid (HTTP 400, code 190, subcode N)` — `meta_token_rejected` |
| `error.code` 2635 | fails at once: `Meta API version v22.0 is deprecated; Meta says upgrade to v27.0 (HTTP 400, code 2635)` — `meta_api_version_deprecated` |
| any other 4xx | fails at once: `Meta API refused the request: HTTP 403, code 200, …` — recorded for the account as `meta_account_not_readable` |

---

## 2. Configuration

A **credential set** is one access token plus the ad accounts that token reads.

| Variable | |
|---|---|
| `META_ADS_ACCESS_TOKEN` | set 1's token: a System User token with `ads_read` |
| `META_AD_ACCOUNT_ID` | set 1's ad account id, or several separated by commas. Digits; a leading `act_` and spaces are accepted; a repeated id is used once |
| `META_ADS_ACCESS_TOKEN_2`, `META_AD_ACCOUNT_ID_2` (… `_5`) | further sets — for an account in another Business portfolio, which needs its own System User and token |
| `META_AD_ACCOUNT_RETIRED` | optional, not a secret. `<account id>:<last spend day>` pairs, comma-separated — the owner's declaration that an account stopped spending (§6) |
| `META_API_VERSION` | optional. Overrides `meta_ads.api_version` in `config/thresholds.yaml` (`v27.0`) |
| `META_APP_SECRET` (`_2` …) | optional. That set's calls carry `appsecret_proof` (an HMAC of the token) |

| State | What happens |
|---|---|
| nothing set | **not connected**. No network call, no batch, no row, no zero. The sync step is `skipped` and does not vote on the run |
| a set has one of its two variables | that set is not called. The complete sets sync; the run is `partial` and names the set |
| an account id is listed in two sets | nothing syncs: `meta_ads_misconfigured`, naming the id |
| an entry of `META_AD_ACCOUNT_RETIRED` is not `<id>:<YYYY-MM-DD>` | it is ignored, never guessed; the run is `partial` and names it |
| no complete set, a malformed id, or no valid API version | nothing syncs: `meta_ads_misconfigured` |

An account is only ever called with its own set's token. `config_status()`
reports, per set, whether a token is present and which accounts it lists —
never a token.

**An account listed in a broken set is still a configured account.** It does
not sync, and it is never read as removed: every period past its last verified
day is withheld until the set is repaired (§6). If an account list holds an
entry that is not an id, the configured accounts are not fully known and no
Meta total is published at all (`source_unavailable`).

No API version is written in the connector. When Meta retires the configured
one the sync fails with `meta_api_version_deprecated` and the message names the
version to change `meta_ads.api_version` (or `META_API_VERSION`) to.

---

## 3. Storage

Five tables, every one keyed by `account_id`. Written only by the Meta sync.

| Table | Holds |
|---|---|
| `meta_ads_account` | the account as Meta describes it: name, currency, time zone. Never deleted |
| `meta_ads_campaign_daily_spend` | one row per account, campaign and account-local day: `cost_micros`, `spend_raw`, currency, impressions, clicks, name |
| `meta_ads_account_daily_spend` | the account-level total per day |
| `meta_ads_spend_coverage` | the fetch ledger: one row per fetched range, `verified` or `failed`, with the account's zone, pages read, the failure's reason, how many already-verified days changed, and `fetched_at` |
| `meta_ads_account_sync_state` | the latest sync attempt per account: status, reason, and `not_readable_since` |

**A range is replaced, never merged** (`db/writers.py::replace_meta_campaign_spend_chunk`).
Meta restates recent days; a campaign-day that is in one response and not in
the next must stop counting. In one transaction: validate every row; read the
range's per-day totals and stored campaign names; delete the range; insert the
response; replace the account-day rows; record the range `verified` with
`fetched_at = clock_timestamp()`. Any failure rolls all of it back and leaves
the range as it was. The counts returned are the database's own.

* An empty response is a **verified zero** for a range that held no verified
  spend.
* **A day with verified spend is never taken to zero by a re-read.** An empty
  200 at both levels reconciles with itself, so reconciliation cannot tell
  "Meta un-spent the day" from "Meta answered wrongly". A response that would
  leave a day a verified chunk covers, with stored spend above zero, with no
  campaign spend at all is refused (`meta_restatement_implausible`); stored
  rows and the ledger are untouched. Changed amounts, and a campaign-day that
  goes while its day keeps spend, are ordinary restatements and pass. The only
  override is the backfill script's `--accept-zero-restatement`, for an
  operator who has checked in Meta; the daily sync has none.
* A row with no name keeps the newest name stored for that campaign in that
  account.
* A **failed** fetch stores no spend, deletes nothing, and records a `failed`
  ledger row — which never replaces a `verified` row for the same range.
* A day with no verified row is **unknown**, not zero.

---

## 4. The daily sync

`scheduler/incremental_sync.py::_sync_meta_ads_spend` runs right after the FX
step of the daily incremental sync (06:00 UTC). For each configured account:

1. read the account from Meta and store it;
2. the window is the last 28 complete days of **that account's own time zone**
   (`sync.daily_incremental.lookback_days.meta_ads`), ending yesterday there;
3. read every page of campaign days, then the account days;
4. **reconcile** (§5); only then replace the range and record it verified;
5. make the range's spend days convertible to USD (§7).

Each account is independent: one failing does not stop another, and a failure
never deletes or overwrites what an account has stored. One `sync_batches` row
covers the step.

| Outcome | Step status |
|---|---|
| every account verified and convertible to USD | `success` |
| some account failed; or is verified without USD rates; or a credential set is incomplete | `partial` — what was verified is stored |
| no account verified | `failed` — nothing replaced |
| Meta not configured | `skipped` |

`partial` is recorded as `partial` on the batch and in the run — never folded
into `success` or `failed`. Each error names its account id. The proven-through
date on the batch is the **earliest** of the accounts' last days, and is set
only when every account was verified.

No new run type: the step is part of `daily_incremental_sync`, and
`RUN_TYPE_CADENCE` is unchanged.

**The backfill writes no `sync_batches` row and no `runs` row** — like the
Google Ads spend and geo backfills. `sync_state` and the dataset's freshness
belong to the daily step alone, so backfilling one account cannot make a
failed daily feed read fresh, and a partial backfill cannot move the daily
feed's proven-through date. What a backfill did is in the coverage ledger.

### The two production accounts and 06:00 UTC

| | Old account `944370805110952` | New account `4730305973961806` |
|---|---|---|
| Zone | Asia/Amman (UTC+3) | America/Los_Angeles (UTC−8 / −7) |
| Local time at 06:00 UTC | 09:00 today | 22:00 (23:00 in summer) **yesterday** |
| Its yesterday has ended? | yes, at 21:00 UTC | no — it ends at 08:00 UTC (07:00 in summer) |
| The window ends on | the UTC date's yesterday | the day **before** that |

So the Los Angeles account is always one calendar date behind the Amman one at
sync time, and that is correct: the System page says "complete through" each
account's own date. A range that ends on the UTC date's yesterday is not
publishable until the following morning's sync.

---

## 5. Reconciliation

A range is `verified` only when paging was exhausted **and**, for every day,
the campaign rows add up to the account-level row. A day present on one side
only is compared with zero on the other, so days without delivery — no row at
either level — reconcile, and a whole range without delivery is a verified zero.

Tolerance: **one minor unit** of the account currency — 0.01, or 0.001 for the
three-decimal currencies (BHD, IQD, JOD, KWD, LYD, OMR, TND). A larger
difference fails the range with `meta_reconciliation_mismatch` and stores
nothing. This is the guard against a campaign the campaign-level query leaves
out: its spend would be in the account total and missing from the rows.

---

## 6. Several accounts

Each account has its own currency and time zone, read from Meta. Sync,
coverage, reconciliation, the settle rule, FX and status are evaluated per
account.

### Which accounts a range needs

* Every account **listed in any credential set** — complete or broken — and
  every account that is **stored** must be covered for **every** day of the
  range. An account that was never read, that Meta no longer lets us read,
  whose credential set is broken, or that was dropped from the configuration
  therefore withholds every range that reaches past its last verified day.
* **Being absent from the configuration means nothing.** It is not evidence
  that an account stopped spending.
* For all time, each account counts from its own first verified day; the
  payload lists them (`first_verified_days`).
* `not_connected` only when no account is listed and none is stored.

### Retiring an account: the owner's declaration

The one way an account stops being required is an explicit declaration:

```
META_AD_ACCOUNT_RETIRED=944370805110952:2026-12-20
```

`<account id>:<last spend day>`, comma-separated for several; the day is a day
of that account's own time zone. It is the owner saying "this account spent
nothing after that day". The platform cannot read that from Meta, so:

* it is honoured **only for an account that is not being read** — one that is
  in no credential set, or one Meta has started refusing (its sync state has
  `not_readable_since`). A declared account that is configured and still
  answered for keeps being required like any other; the declaration is
  ignored and reported (`retired_declaration_account_still_read`, in the truth
  result and on the System line);
* it is not honoured when the account has stored spend dated **after** the
  declared day (`retired_declaration_contradicted`);
* **declare the true last spend day, even if that day was never read.** With
  the last verified day `L` and the declared day `D`:

  | The range… | Result |
  |---|---|
  | ends on or before `L` | published as measured; nothing rests on the declaration |
  | touches any day after `L` up to and including `D` | withheld (`retired_declaration_not_covered`) — those are days the account is said to have spent on, and they were never read |
  | reaches past `L`, and `D` is on or before `L` | published; the account counts through `L` and the figure rests on the declaration from `L + 1` |
  | starts after `D` (and after `L`) | published; the account has no day in it and the figure rests on the declaration from the day after the later of `L` and `D` |

* an entry that is not `<id>:<YYYY-MM-DD>`, or one id given two different days
  (`act_` in any letter case is the same id — then neither day is honoured),
  is ignored, never guessed. It is reported **by its position in the list
  only**: the text of an entry that failed to parse is never returned, logged
  or shown, since it may be a secret pasted into the wrong variable. The same
  holds for an account list entry that is not an id. An id that was never read
  is ignored and named (`retired_declaration_unknown_account`);
* **a total that rests on a declaration says so at the top level**:
  `owner_declared_retired: [{account_id, last_spend_day, relied_from}]` —
  `relied_from` is the first day the figure rests on the declaration — and
  `owner_declared_retired_disclosure` ("This total assumes ad account … spent
  nothing from … on (declared last spend day …) — the owner's declaration
  (META_AD_ACCOUNT_RETIRED), not a reading from Meta."). Both are empty/None
  when the figure does not rest on one;
* when every account is validly declared and none is configured, a later range
  is a zero that carries the declarations — never an unmarked
  `not_connected`.

**For PR-ADS-171B:** any surface that shows a figure whose
`owner_declared_retired` is non-empty must show
`owner_declared_retired_disclosure` beside it, and 171B must add an audit/AST
guard that fails when a surface reads the figure without the disclosure.
Done: checks 19 (`meta_spend_disclosure_carried`) and 20
(`no_rate_over_meta_spend`) of `scripts/audit_customer_revenue_reader_cutover.py`
(`docs/55` §5).

Backfill every account from the **same** first day. A period is published only
when every account is covered for all of it; an account with no ads in a
month is recorded as a verified zero in seconds.

### An account that can no longer be read

When Meta refuses an account (suspended, closed, permission removed):

* its stored rows and verified ledger rows stay exactly as they are;
* the run is `partial` while it is still configured, with
  `account <id> is not readable; its stored history is kept (HTTP n, code n …)`;
* the System page reads
  `account not readable since <date> (HTTP n, code n); history through <last verified day> is kept`;
* ranges up to its last verified day still publish; ranges past it are
  withheld until the owner declares it retired (above). Taking it out of the
  configuration does not do that.

### A changed currency or time zone

Every stored row is an amount in the account's currency on a day of the
account's zone.

* The account **has stored spend rows**: a different currency or zone from Meta
  is refused (`account_identity_changed`). Nothing is changed; that account's
  sync fails until it is resolved.
* The account has **no stored spend row**: the change is accepted — there is
  nothing to relabel. On a zone change the account's ledger rows (which can
  only say "no spend", on days of the old zone) are deleted in the same
  transaction, so every day is read again in the new zone before it is called
  covered. Backfill that account again afterwards.

So the new account can be moved to UK time while it has no delivery. Do it
before its first ad runs.

---

## 7. USD

| Account currency | USD value |
|---|---|
| USD | the amount itself (rate 1; no rate is looked up or stored) |
| priced by the FX provider (`meta_ads.fx_provider_currencies`; GBP is) | each day's spend × that day's rate from `fx_rates` — the same table and provider as Google Ads' GBP spend |
| not priced by the provider (JOD, AED, SAR …) | none. The spend is stored and published in its own currency; USD is unavailable with `fx_currency_not_supported`. No rate is estimated and no peg is assumed |

A day with spend and no rate has no USD value (`fx_rates_missing`); the range
stays verified and the run is `partial`. A row of zero spend needs no rate.

---

## 8. The truth function

`services/meta_ads_spend_truth_service.build_meta_ads_spend_truth_for_days(start, end, *, settled=False, now=None)`
— the Meta counterpart of the Google Ads one, with the same states and shape.

| State | Published |
|---|---|
| `not_connected` | nothing — no account is relevant to the range |
| `source_unavailable` | nothing — stored spend unreadable, or configuration broken |
| `coverage_incomplete` | nothing — a required day of some account is not verified, or (with `settled`) not held completely |
| `fx_incomplete` | the native total, if the accounts share a currency; USD is None; `fx_reason` |
| `verified_zero_spend` | 0 |
| `verified` | the USD total (and the native total, if one currency) |

* The total is the sum over the relevant accounts, each converted with its own
  currency and — with `settled` — each settled **in its own zone** by the
  shared rule (`google_ads_spend_service.analyze_settled_coverage`). An unknown
  zone settles nothing. The Google Ads zone is never used.
* `accounts` lists each account: id, name, currency, zone, whether it is
  configured, its own state, native and USD spend, the last day it is required
  for, and its latest sync status.
* `time_zones`, `mixed_time_zones` and `mixed_currencies` disclose that one
  calendar date is a different 24 hours in each account, and that accounts in
  different currencies have no native total.
* An unknown currency is None — never a default.

The System page line (`services/system_overview_service._meta_spend_source`)
is one row for all accounts, each listed in its own words.

* An account is judged against **the last day its most recent due sync could
  settle in its own zone** — not against the moment the page is viewed. The
  sync starts at 06:00 UTC and is "due" three hours later
  (`meta_ads.sync_grace_hours`). Judged at page-view time, the Los Angeles
  account would read "late" from 08:00 UTC until the next morning after a
  perfectly clean sync. If a later day is already held, that day is reported.
* The line is `current` only when every syncing account is complete **and
  convertible to US dollars**, no credential set is broken, no declaration is
  unreadable, and no stored account is left unresolved.
* `misconfigured` is shown whatever the other accounts say. Otherwise the line
  carries the most serious account status: `token_rejected`, `not_readable`,
  `late`, `not_proven`, `usd_unavailable` (spend recorded, no US-dollar value
  — never "Complete through"), `account_unresolved` (a stored account that is
  neither configured nor validly declared retired).
* `not_connected` is rendered neutral — the one status that is neither healthy
  nor a warning, and only for this row.

---

## 9. What is known about the live API

### Confirmed from production (owner's Render shell, 2026-11-22)

| Fact | Evidence |
|---|---|
| API version `v27.0` | an unversioned call returned HTTP 400 `(#2635) You are calling a deprecated version of the Ads API. Please upgrade to the latest version: v27.0.`; calls on `v27.0` then returned 200 |
| `Authorization: Bearer` is accepted on the account node and on Insights | both returned 200 with the header and no token in the URL |
| Account fields `name`, `currency`, `timezone_name`, `account_status`, `id` | `{'name': 'Logistaas Limited', 'currency': 'GBP', 'timezone_name': 'America/Los_Angeles', 'account_status': 1, 'id': 'act_…'}` |
| The two accounts | set 1: `4730305973961806` "Logistaas Limited", GBP, America/Los_Angeles, status 1, no delivery. Set 2 (own token, another Business portfolio): `944370805110952` "Logistaas", USD, Asia/Amman, status 1 |
| No delivery is an empty list | Insights `level=account` → `{'data': []}` for the account with no campaigns. Seen for an account that has never spent; it is **not** evidence that an empty list always means no spend, which is why a re-read that would zero a day with verified spend is refused (§3) |
| `spend` is a decimal string; `date_start` / `date_stop` are ISO dates | `[{'spend': '1909.34', 'date_start': '2026-10-23', 'date_stop': '2026-11-21'}]` (old account, `last_30d`, read on 11-22 at about 10:00 Amman: the preset ended yesterday) |

The tests' fake API is built from exactly these two accounts.

### VERIFY — not checkable from the build environment

(The ad-set level has its own list — what remains unverified about
`level=adset`, the `actions` field and the lead action types — in `docs/53`
§9. The items below are unchanged by PR-ADS-171C and all still open.)

Each is either guarded by a failure that says so, or affects only what is
noted. The smoke test (§10) exercises 1–8.

1. `time_range` + `time_increment=1` returns one row per day at both levels
   (only an aggregated `date_preset` read has been seen).
2. `level=campaign` returns `campaign_id`, `campaign_name`, `impressions`,
   `clicks`, `account_currency` and `date_stop` as requested.
3. Whether `level=campaign` includes archived and deleted campaigns' spend.
   No filter parameter is sent, because none could be verified from here. If
   Meta leaves such campaigns out, their spend is in the account total and
   missing from the rows, and reconciliation fails that range (§5) — loudly,
   not silently. **Check it before the backfill** with
   `python -m scripts.meta_ads_smoke_test --month YYYY-MM` for a month that
   contains an archived or deleted campaign (§10 step 3).
4. The per-day campaign rows add up to the account-level row within one minor
   unit. If Meta rounds each row, a day with many campaigns could differ by
   more and fail reconciliation.
5. A row always carries `spend` (a day with impressions and no spend might
   omit it; that fails the read as malformed rather than reading as zero).
6. `limit=500` is accepted on Insights (`meta_ads.page_limit`).
7. `paging.cursors.after` is present whenever `paging.next` is.
8. `effective_status=["ACTIVE"]` on `act_{id}/campaigns` (smoke test only).
9. The throttle codes (4, 17, 32, 613, 80000, 80004), the usage header names
   and that `estimated_time_to_regain_access` is in minutes.
10. What Meta returns for a suspended or closed account. Any 4xx that is not a
    throttle reads as "not readable"; code 190 reads as "token rejected". Both
    keep the history.
11. Whether an existing account's time zone can be changed in place (§6
    handles both answers).
12. The meaning of `account_status` numbers (the smoke test prints the number
    beside a label).
13. Whether `appsecret_proof` is required for these apps (optional here).
14. How far back Insights serves daily data, for the backfill's first day.

---

## 10. Owner steps

The two credential sets are already in Render (2026-11-22):

```
META_ADS_ACCESS_TOKEN   = <System User token, new portfolio>
META_AD_ACCOUNT_ID      = 4730305973961806
META_ADS_ACCESS_TOKEN_2 = <System User token, old portfolio>
META_AD_ACCOUNT_ID_2    = 944370805110952
```

1. **Deploy** this PR. The five tables are created on startup.
2. **Smoke test** (Render shell; reads no database, writes nothing):

   ```
   python -m scripts.meta_ads_smoke_test
   ```

   Expected, with today's figures in place of these:

   ```
   API version      : v27.0 (from config)
   Credential set 1 : token set; accounts 4730305973961806; app secret proof not sent
   Credential set 2 : token set; accounts 944370805110952; app secret proof not sent
   --- Ad account 4730305973961806 ---
   Name             : Logistaas Limited
   Currency         : GBP
   Time zone        : America/Los_Angeles
   Status           : ACTIVE (1)
   Active campaigns : 0
   Last 30 days     : 0.00 GBP on 0 day(s) with spend (2026-10-22 to 2026-11-20)
   Yesterday        : 2026-11-20 (America/Los_Angeles)
   Campaign rows    : 0 in 1 page(s)
   Campaign sum     : 0.00 GBP
   Account total    : 0.00 GBP
   USD conversion   : available
   RESULT           : OK — the campaign rows sum to the account total
   --- Ad account 944370805110952 ---
   Name             : Logistaas
   Currency         : USD
   Time zone        : Asia/Amman
   Status           : ACTIVE (1)
   Active campaigns : <n>
   Last 30 days     : 1909.34 USD on <n> day(s) with spend (2026-10-23 to 2026-11-21)
   Yesterday        : 2026-11-21 (Asia/Amman)
   Campaign rows    : <n> in 1 page(s)
   Campaign sum     : <x> USD
   Account total    : <x> USD
   USD conversion   : not needed (USD account)
   RESULT           : OK — the campaign rows sum to the account total
   === 2 of 2 account(s) OK ===
   ```

   Exit code 0. `Campaign sum` must equal `Account total` on each account. A
   `FAILED` line gives Meta's HTTP status and error code; `MISMATCH` means
   VERIFY item 3 or 4 is false for this account — stop and report it.
3. **Check a month with an archived campaign** — before any backfill. Pick a
   month in which a campaign that is now archived or deleted was running:

   ```
   python -m scripts.meta_ads_smoke_test --month 2026-03
   ```

   Per account it prints the range, the campaign rows, `Campaign sum`,
   `Account total`, `Days compared`, and
   `RESULT : OK — every day's campaign rows sum to the account total`; exit 0.
   A `MISMATCH <date>` line means the campaign-level read is leaving spend out
   for that day (VERIFY item 3 or 4): **do not backfill**; report the output.
   It writes nothing.
4. **Backfill**, both accounts from the same first day (pick the first day
   Meta spend is wanted from):

   ```
   python -m scripts.backfill_meta_ads_spend --from 2025-01-01 --dry-run
   python -m scripts.backfill_meta_ads_spend --from 2025-01-01
   ```

   Re-run with `--resume` after an interruption. Exit 0 = every month of both
   accounts verified. (The backfill does not change the dataset's freshness;
   the next daily sync does.)
4a. **Ad sets** (PR-ADS-171C), after the campaign backfill above:
   `python -m scripts.meta_ads_smoke_test --adsets`, then
   `python -m scripts.backfill_meta_ads_spend --from <day> --level adset`.
   See `docs/53` §10 for the expected output and what to send back.
5. **Check** the System page: the "Meta Ads spend" line should read
   `Complete through <date>` with one line per account, each on its own date.
   The 06:00 UTC sync keeps it current.
6. **Before the new account's first ad**: if its time zone is going to be
   changed to UK time, change it now, then re-run the backfill for it
   (`--account 4730305973961806`). Once it has stored spend a zone change is
   refused.
7. **The old account — keep it configured.** Leave `META_ADS_ACCESS_TOKEN_2`
   and `META_AD_ACCOUNT_ID_2` in place for as long as Meta still answers for
   it. It keeps being measured, including the 28 days of restatements after
   its last ad. Do **not** remove it when the ads stop.

   Only when Meta refuses it for good (the System page reads
   `account not readable since …; history through <day> is kept`, and the
   daily run is `partial`):

   1. set `META_AD_ACCOUNT_RETIRED=944370805110952:<its TRUE last spend day>`.
      Never adjust the day to fit what was read. If the true day is on or
      before the "history through" day shown, every period is published. If
      it is later — the account spent on days that were never read — then
      periods up to the "history through" day are published as measured,
      periods that include any day after it up to the declared day stay
      withheld (that spend is unknown), and periods wholly after the declared
      day are published with the disclosure;
   2. check the System page: the account reads `declared retired by the owner
      (last spend day …); history through … is kept`, with the unread days
      named if there are any;
   3. only then remove `META_ADS_ACCESS_TOKEN_2` and `META_AD_ACCOUNT_ID_2`
      (which ends the `partial` runs).

   While the account is still configured and Meta still answers for it, a
   declaration is ignored and the System page says so: it has to stop being
   read first. Its history stays stored. Totals for periods after it stopped
   are published with the disclosure that they rest on this declaration, from
   which day.

---

## 11. Decisions to review

* **The retirement declaration is the one value taken from the owner rather
  than from Meta** (§6). It is explicit, checked against the ledger, and
  disclosed at the top level of every total that rests on it.
* **A zone change with no stored spend deletes that account's ledger rows.**
  They are re-read in the new zone; until the backfill is re-run, older ranges
  for that account read `coverage_incomplete`.
* **A re-read that would zero a day with verified spend fails that account's
  sync** until someone looks. If Meta ever legitimately removes a whole day's
  spend, the daily run is `partial` for that account until the backfill is run
  with `--accept-zero-restatement`.
* **An account whose currency the FX provider does not price makes the daily
  run `partial` every day.** Neither production account is in that position
  (USD, GBP).
* **Reconciliation tolerance is one minor unit** (VERIFY 4).
* **Any account that is not verified for a day withholds the total for that
  day** — unreadable, in a broken set, never read, or dropped from the
  configuration. That is deliberate: its spend is unknown.
* **The System page allows a sync three hours to finish** before it calls an
  account late (`meta_ads.sync_grace_hours`).
