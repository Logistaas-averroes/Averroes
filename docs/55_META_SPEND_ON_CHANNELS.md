# 55 — Meta spend on the Channels page and the Dashboard (PR-ADS-171B)

PR-ADS-171A stored Meta spend and gave it one truth function
(`docs/52`); PR-ADS-171C added the ad-set level (`docs/53`). This PR puts the
spend on two pages — the Channels page and the Dashboard's Channels tab —
and splits HubSpot's Paid Social contacts by platform. It joins the two
**nowhere**: no ratio, no column and no key puts Meta spend beside a HubSpot
count.

## 1. The "Meta Ads (spend)" line

`services/meta_ads_spend_line_service.py` builds one line from
`meta_ads_spend_truth_service.build_meta_ads_spend_truth_for_days` — the only
reader of Meta spend totals — and republishes its decision.

| Truth state | `spend` (USD) | `native_spend` | Accounts' figures |
|---|---|---|---|
| `verified` / `verified_zero_spend` | published (a zero is `0`) | published when the truth gives a native total (one currency with spend) | each account's native and USD |
| `fx_incomplete` | withheld, `fx_rates_missing` / `fx_currency_not_supported` | published only if the truth publishes it (one currency) | none |
| `coverage_incomplete` | withheld, `meta_ads_spend_coverage_incomplete`, with the truth's `withheld_detail` | none | none |
| `not_connected` | unavailable, `meta_ads_not_connected` | none | none |
| `source_unavailable`, an unknown state, a raised error | unavailable, `meta_ads_spend_source_unavailable` / `meta_ads_spend_line_failed` | none | none |
| the page has no settled day | unavailable, `no_settled_day_in_window` (no truth read) | none | none |

* **Withheld means absent.** Unless the total is published, no spend figure
  is on the line — not the total, not any account's (one account's spend
  beside a withheld total is a partial, not the total). Each account still
  shows its id, name, currency, time zone, state and the last day verified in
  its own zone (`verified_through`).
* **The owner's retirement declaration** (`docs/52` §6). The line carries
  the truth's `owner_declared_retired` and
  `owner_declared_retired_disclosure` whenever the figure rests on one, and
  both pages show the disclosure beside the figure. A marker without its
  disclosure withholds the figure (`meta_retired_disclosure_missing`) — in
  the service and again in the page (`metaLineSpendMetric`).
* **`connected`**: `True` (Meta set up), `False` (`not_connected`), `None`
  (could not tell: an unreadable store or a broken configuration). False and
  None are different claims; `spend_sources` repeats them.
* **No comparison.** `compared_with_outcomes: false`, and `comparison` is
  `incompatible_rate(channel_population_is_not_meta_spend_population)`. The
  line has no deals, customers, revenue, contacts, SQLs or ratio key.
* Not in the channel partition, not added to the totals.

### Which days

The page's window is a range of calendar dates; the line passes exactly
those settled dates to the truth with `settled=True`. **No second date
rule**: the truth reads each ad account on its own calendar
(`meta_ads_account.time_zone_name`) and settles each in its own zone
(PR-ADS-171A). The line discloses it (`day_basis_disclosure`): "Each ad
account is read on its own calendar days: 944370805110952 on Asia/Amman;
4730305973961806 on America/Los_Angeles. The same date is a different 24
hours in each account. These are not the Europe/London days the rest of this
page counts on; nothing on the page is divided by this spend."

Why that is exact and the Google Ads line's zone rule (`docs/50` §5,
`account_time_zone_differs_from_evidence_days`) does not apply: Meta reports
spend per day only in the account's own zone, so "Meta spend on date D" has
one meaning — the account's D. The Google Ads line divides spend by deals
counted on Europe/London days, so a zone difference there changes a ratio;
nothing divides by the Meta line, so the offset changes no figure the page
computes. An account whose zone is unknown settles nothing: the total is
withheld.

| Page | Dates passed |
|---|---|
| Channels | the evidence window ended at yesterday (`canonical_contract.resolve_settled_evidence_window`, `docs/50` §4): `start_date` … London's yesterday |
| Dashboard → Channels | the business window ended at yesterday when open (`canonical_contract.resolve_settled_window`, PR-ADS-167A); a closed window unchanged; the first day of a quarter or year has no settled day |

**What this means in production today.** At the 06:00 UTC sync, Los Angeles'
yesterday has not ended (it ends at 08:00 UTC, 07:00 in summer), so the
4730305973961806 account is verified one date behind London's yesterday,
every morning. Every window that ends on London's yesterday is therefore
**withheld**: "account 4730305973961806 is not verified for every day through
2026-11-21 (verified through 2026-11-20)". That is the truth: Meta has not
been asked about that account's 21st. It ends when the owner moves the
account to UK time (it has no spend, so the change is accepted; then re-run
its backfill — `docs/52` §10 step 6).

## 2. Paid Social by platform

`channel_funnel_service.build_funnel` gives the `PAID_SOCIAL` channel a
`platform_split`, from each contact's `hs_analytics_source_data_1`
(Original Source Drill-Down 1) as stored, compared whole after the funnel's
own `drilldown_key` (trimmed, lower case):

| Platform | Drill-down 1 |
|---|---|
| `meta` — "Meta (facebook + instagram)" | `facebook`, `instagram` |
| `linkedin` — "LinkedIn" | `linkedin` |
| `other` — "Other drill-down value" | any other value, listed (first 8, and how many) |
| `unknown` — "Drill-down not recorded (unknown)" | no value — **never Meta** |

No substring match: `facebook ads`, `metadata` and `fb` are `other`. (The
older source classifier, `analysis.source_classification._social_platform`,
matches substrings; it is used by Revenue by Source and the Dashboard's
platform matrix and is unchanged.) Each row has the funnel's own columns
(contacts, MQL, SQL, Opportunity, Customer stage), computed from the
contacts, not from the capped drill-down list, and `reconcile` requires the
four rows to add back up to the channel. The page shows them as sub-rows of
Paid Social, always visible; their outcome cells read "n/a — deals are
counted per channel, not per platform".

**Drill-down 1 was already stored and synced**: `hubspot_contact_funnel.
hs_analytics_source_data_1` exists, the funnel sync writes it, and
`fetch_channel_funnel_contacts` already selected it. No sync or schema change.

Production (read-only, 2026-11-22, ~6 months): 1,187 Paid Social contacts —
facebook ~1,162, instagram 4, linkedin 21. Drill-down 2 holds the Meta
campaign name as HubSpot recorded it: "lead generation | 2026" (992) is a name
with no spend in Meta — a renamed campaign — which is why no ratio is joined
yet.

## 3. The Paid Social row and the other surfaces

* **Channels, Paid Social row.** Meta not set up (`connected: false`): as
  before, `channel_spend_not_connected`. Otherwise its spend cell is
  `spend_shown_on_meta_ads_line` and both period ratios are
  `incompatible_rate(channel_population_is_not_meta_spend_population)`. A
  Meta-specific reason was needed: `channel_population_is_not_spend_population`
  is the same idea, but its page wording names Google Ads.
* **Channels strip** gains a "Meta Ads spend" cell (the disclosure in its
  sub-line when a declaration is relied on). The Google Ads cell's sub-line
  now reads "compared only with the Google Ads line" (was "the only connected
  spend source").
* **When the outcome table is withheld**, the line renders as its own panel.
* **Dashboard → Channels** (`build_dashboard_channels`) carries
  `meta_ads_line`, rendered as a "Meta Ads (spend)" panel after the platform
  matrix in the tab's own panel style; `truth_status.meta_ads_spend`
  (`ready` / `partial` / `not_connected` / `unavailable`) is a footer chip.
  When Meta is set up, the Paid Social channel row and its `meta` platform row
  read "Revenue-only — Meta spend is on its own line, not matched to these
  deals" instead of "no connected spend source"; their spend and ratio stay
  null. The "Unavailable" decision card and the unavailable list say the
  same. `non_google_spend` stays `unavailable` (no spend outside Google Ads is
  compared with revenue).
* **Dashboard → Overview** shows no channel spend figure (its source mix
  carries a status only), so it is unchanged. The System page is unchanged.

## 4. Cost per lead

Not shown. 171C's `meta_reported_leads` is publishable only once its lead
action type is verified and observed (`docs/53`), and a "Meta-reported
leads" figure beside the Channels table would read as a HubSpot count. It
can be added later, Meta spend ÷ Meta's own count only, on its own gate.

## 5. Guards

`scripts/audit_customer_revenue_reader_cutover.py`:

* **19 `meta_spend_disclosure_carried`** — every production module that
  calls `build_meta_ads_spend_truth_for_days` is in `META_SPEND_READERS` and
  carries `owner_declared_retired_disclosure`; every module that builds the
  line is in `META_LINE_PAGES`; `metaLineSpendMetric` withholds a figure whose
  declaration has no disclosure, every renderer reads the figure through it,
  and nothing in `static/app.js` reads the line's `.spend` around it. On each
  page payload that carries `meta_ads_line`, a published figure resting on a
  declaration must carry its disclosure.
* **20 `no_rate_over_meta_spend`** — the line carries no key naming a HubSpot
  count, a deal, revenue or a ratio; a withheld line carries no figure; the
  comparison is never published; and no page text labels Meta spend per
  lead, contact, SQL, deal or customer.
* Check 18 runs over both payloads unchanged.

## 6. Tests

`tests/test_pr_ads_171b_meta_on_channels.py` (38) and
`tests/test_pr_ads_171b_meta_on_channels_pg_integration.py` (4, in the CI
PostgreSQL step and its did-run list). `tests/test_pr_ads_168a_channel_outcomes.py`
gains an autouse fixture that states the Meta state its tests assume (not
set up, nothing stored); without it the store is unreadable in those tests
and the line correctly reads "could not tell".

## 7. Known gaps

1. While the Los Angeles account is on Los Angeles time, every Channels and
   Dashboard window ending yesterday is withheld (§1).
2. The Dashboard's platform matrix still uses the substring classifier for
   its `meta` platform; the Channels split is exact. They can disagree on a
   drill-down such as `facebook ads`.
3. Meta spend and HubSpot Paid Social are not joined. A join needs Meta's
   campaign ids on the contacts (or a reviewed name map that survives a
   rename) — a later PR.
