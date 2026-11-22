# 52 — Meta Ads spend connector (PR-ADS-171A)

Read-only. Averroes reads how much each Meta (Facebook/Instagram) ad account
spent per campaign per day, stores it, and can say for any range of days
whether that spend may be published and what it is. Nothing is ever written to
Meta.

This PR adds the data and its truth function. **No page shows Meta spend yet**
— the Channels page is PR-ADS-171B. The System page gains one line.

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
* no error text contains a URL or `str(exc)` of a transport error. A transport
  failure is named by its class; an API failure by the HTTP status and the
  fields of Meta's `error` object, with every configured token and app secret
  removed from any text Meta sent back.

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
| `META_API_VERSION` | optional. Overrides `meta_ads.api_version` in `config/thresholds.yaml` (`v27.0`) |
| `META_APP_SECRET` (`_2` …) | optional. That set's calls carry `appsecret_proof` (an HMAC of the token) |

| State | What happens |
|---|---|
| nothing set | **not connected**. No network call, no batch, no row, no zero. The sync step is `skipped` and does not vote on the run |
| a set has one of its two variables | that set is not called. The complete sets sync; the run is `partial` and names the set |
| an account id is listed in two sets | nothing syncs: `meta_ads_misconfigured`, naming the id |
| no complete set, a malformed id, or no valid API version | nothing syncs: `meta_ads_misconfigured` |

An account is only ever called with its own set's token. `config_status()`
reports, per set, whether a token is present and which accounts it lists —
never a token.

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

* An empty response is a **verified zero** for the range.
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
`RUN_TYPE_CADENCE` is unchanged. The backfill writes `sync_batches` rows only.

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

* A **configured** account must be covered for **every** day of the range. One
  that was never read, or that Meta no longer lets us read, therefore withholds
  every range that reaches past its last verified day.
* An account that is **stored but no longer configured** keeps counting: it
  must be covered for every day of the range up to its **last verified day**,
  and after that day it contributes nothing and requires nothing.
* For all time, each account counts from its own first verified day.
* With nothing configured, stored history still counts for the days it covers;
  a range no stored account reaches is `not_connected`.

**Removing an account from the configuration is the owner's statement that it
has stopped spending.** The platform cannot check that: after an account is
removed, days after its last verified day are treated as holding no spend for
it. The payload discloses it (`accounts[].configured: false`,
`required_through`). Remove an account only once it is closed.

Backfill every account from the **same** first day. A period is published only
when every configured account is covered for all of it; an account with no ads
in a month is recorded as a verified zero in seconds.

### An account that can no longer be read

When Meta refuses an account (suspended, closed, permission removed):

* its stored rows and verified ledger rows stay exactly as they are;
* the run is `partial` while it is still configured, with
  `account <id> is not readable; its stored history is kept (HTTP n, code n …)`;
* the System page reads
  `account not readable since <date> (HTTP n, code n); history through <last verified day> is kept`;
* ranges up to its last verified day still publish; ranges past it are
  withheld until the owner removes the account from its list. Its history then
  stays stored and keeps counting, as above.

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
is one row for all accounts, each listed in its own words. It is `current`
only when every configured account is complete through its own yesterday;
otherwise it carries the worst account's status (`token_rejected`,
`not_readable`, `late`, `not_proven`), or `misconfigured`. `not_connected` is
rendered neutral — the one status that is neither healthy nor a warning, and
only for this row.

---

## 9. What is known about the live API

### Confirmed from production (owner's Render shell, 2026-11-22)

| Fact | Evidence |
|---|---|
| API version `v27.0` | an unversioned call returned HTTP 400 `(#2635) You are calling a deprecated version of the Ads API. Please upgrade to the latest version: v27.0.`; calls on `v27.0` then returned 200 |
| `Authorization: Bearer` is accepted on the account node and on Insights | both returned 200 with the header and no token in the URL |
| Account fields `name`, `currency`, `timezone_name`, `account_status`, `id` | `{'name': 'Logistaas Limited', 'currency': 'GBP', 'timezone_name': 'America/Los_Angeles', 'account_status': 1, 'id': 'act_…'}` |
| The two accounts | set 1: `4730305973961806` "Logistaas Limited", GBP, America/Los_Angeles, status 1, no delivery. Set 2 (own token, another Business portfolio): `944370805110952` "Logistaas", USD, Asia/Amman, status 1 |
| No delivery is an empty list | Insights `level=account` → `{'data': []}` |
| `spend` is a decimal string; `date_start` / `date_stop` are ISO dates | `[{'spend': '1909.34', 'date_start': '2026-10-23', 'date_stop': '2026-11-21'}]` (old account, `last_30d`, read on 11-22 at about 10:00 Amman: the preset ended yesterday) |

The tests' fake API is built from exactly these two accounts.

### VERIFY — not checkable from the build environment

Each is either guarded by a failure that says so, or affects only what is
noted. The smoke test (§10) exercises 1–8.

1. `time_range` + `time_increment=1` returns one row per day at both levels
   (only an aggregated `date_preset` read has been seen).
2. `level=campaign` returns `campaign_id`, `campaign_name`, `impressions`,
   `clicks`, `account_currency` and `date_stop` as requested.
3. Whether `level=campaign` includes archived and deleted campaigns' spend.
   If it does not, reconciliation fails that range (§5) — loudly, not silently.
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
3. **Backfill**, both accounts from the same first day (pick the first day
   Meta spend is wanted from):

   ```
   python -m scripts.backfill_meta_ads_spend --from 2025-01-01 --dry-run
   python -m scripts.backfill_meta_ads_spend --from 2025-01-01
   ```

   Re-run with `--resume` after an interruption. Exit 0 = every month of both
   accounts verified.
4. **Check** the System page: the "Meta Ads spend" line should read
   `Complete through <date>` with one line per account, each on its own date.
   The next 06:00 UTC sync keeps it current.
5. **Before the new account's first ad**: if its time zone is going to be
   changed to UK time, change it now, then re-run the backfill for it
   (`--account 4730305973961806`). Once it has stored spend a zone change is
   refused.
6. **When the old account is closed for good**: after its last successful
   sync, remove `META_ADS_ACCESS_TOKEN_2` and `META_AD_ACCOUNT_ID_2` from
   Render. Its history stays stored and keeps counting through its last
   verified day. Until then the daily run is `partial` and ranges past that
   day are withheld.

---

## 11. Decisions to review

* **A removed account is assumed to have stopped spending** (§6). This is the
  one place a value is taken from the owner's action rather than from Meta.
* **A zone change with no stored spend deletes that account's ledger rows.**
  They are re-read in the new zone; until the backfill is re-run, older ranges
  for that account read `coverage_incomplete`.
* **An account whose currency the FX provider does not price makes the daily
  run `partial` every day.** Neither production account is in that position
  (USD, GBP).
* **Reconciliation tolerance is one minor unit** (VERIFY 4).
* **A configured-but-unreadable account withholds recent ranges for every
  account's total** until it is removed. That is deliberate: its recent spend
  is unknown.
