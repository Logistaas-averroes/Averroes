# 50 — Channel outcomes on the Channels page (PR-ADS-168A)

The Channels page (PR-ADS-162A/162I) answered one question: of the contacts
acquired in this period, how far have they got? This PR adds a second,
separate question on the same page: **which deals closed in the same days,
per channel, and what were they worth?** It reads the closed-won reader
contract (`docs/48`) and publishes nothing the contract has not decided.

## 1. Two cohorts on one page

| Half | Population | Window basis | Ends |
| --- | --- | --- | --- |
| Funnel | contacts **created** in the window | `contact_created_at` (acquisition cohort, `docs/44`) | today |
| Outcomes | won deals **closed** in the window | `deal_close_date` (`metric_family: closed_won_outcomes`) | yesterday (§4) |

They are different people. No rate is computed between them: no
SQL-to-customer, no lead-to-customer, no cost per qualified lead. Percentages
appear only in funnel cells. The table header says which is which —
"Funnel · contacts acquired" over the funnel columns, "Outcomes · deals
closed" and "Paid media · same days" over the new ones — and the customer
stage column reads "Customer stage (contacts)" beside "Customers won (distinct
companies)".

## 2. Deal → channel

A won deal takes channel **X** only when **every** associated contact's
`hubspot_contact_funnel.hs_analytics_source`, normalised by
`channel_funnel_service.source_key` (the funnel's own channel key), is X.
Every other deal sits on the explicit outcome-only row
`__deal_source_unresolved__` ("Source not resolved"), counted by the first
reason that applies:

| Class | Meaning |
| --- | --- |
| `association_lookup_failed` | the ledger's contact lookup failed |
| `no_associated_contact` | the lookup succeeded and found nobody (`association_status = none`) |
| `associated_contacts_not_stored` | the ledger says contacts exist, none are stored |
| `contact_not_in_funnel` | an associated contact has no funnel row |
| `contact_has_no_source` | an associated contact has no original source |
| `contacts_disagree` | the contacts have different original sources |

The source is read in the closed-won universe's own REPEATABLE READ snapshot
(`fetch_closed_won_universe` selects `f.hs_analytics_source` beside
`created_at`). The ledger's `source_primary_raw` is **not** used: when
contacts disagree inside one evidence group it holds the lowest contact id's
source, a choice rather than a measurement.

Channel is HubSpot's original source, unchanged: a deal whose contact came
from a Google ad click (it has a GCLID) but that HubSpot recorded as Direct
Traffic is on Direct Traffic.

Production (read-only, 2026-11-20): all time — 155 resolved, 20
`contacts_disagree`, 14 `no_associated_contact`, 1 `contact_not_in_funnel`;
last 180 days — 15 resolved, none unresolved; last 60 days — 8 resolved.
`scripts/report_attribution_feasibility.py` section 4 measures this per
window.

## 3. The reader contract

`source_attribution_service.build_channel_outcomes(window, now=)` is a
`@single_snapshot` page builder:

* the population is `load_won_deals(window, window_type="evidence",
  bounds=settled bounds)` — `window_type` is new: it reads an evidence key
  through `ccr.window_view(window_type=evidence)`, the membership Campaign
  Evidence and the truth service use;
* each row is `reader_row(base, deals_in_channel, spend_usd=, spend_reason=)`,
  so every row inherits the window's verdict (a withheld window withholds
  every row; an unpriced deal withholds revenue for its row **and** every
  other row and the total, never the counts);
* customers are distinct companies per row and are **never summed**: the
  totals are the window's own `canonical_metrics`;
* the rows, unresolved included, must add back up to the window's published
  deal count with the same deal ids, or every row is withheld with
  `channel_partition_does_not_reconcile`;
* when the deal count is not published, `channels` and `partition` are
  `null` — withheld means absent.

Every known channel has a row (a published 0 is a measurement); the page
shows a row with no contacts in the funnel only when it has closed deals.

## 4. Settled days

Evidence windows end today. Today's deals and today's spend are not settled,
so the outcome and spend half applies the PR-ADS-167A owner decision as the
country pages do: `canonical_contract.resolve_settled_evidence_window` ends
the window at **yesterday** (account-local; the start does not move) through
the same `end_at_settled_day`, and the deals are read over
`settled_revenue_bounds` — the same days the spend covers. The page says:
"Through 19 Nov 2026 — today is not included until tomorrow morning's sync."
The funnel keeps its own cohort (contacts created through today), and the
outcome context line says so.

## 5. Spend

Only Google Ads spend is connected, and it is compared only with the
**"Google Ads (canonical evidence)"** line, never with the Paid Search row.

**The Paid Search row** keeps its deals, revenue and customers. Its two
period ratios are unavailable (`cwt.incompatible_rate(channel_population_is_not_spend_population)`,
the codebase's convention for a rate across two populations), and its spend
cell is unavailable with `spend_shown_on_google_ads_line` — the page shows
no figure a reader could divide Paid Search revenue by. Paid Search is
HubSpot's original source: it holds labels approved as not Google Ads
(PR-ADS-120b) and misses Google Ads deals HubSpot recorded under another
source. Review round 1's probe (g1 mapped $10k, b1 label approved not Google
Ads $50k, d1 GCLID recorded as Direct $20k, spend $4,000) had published
Paid Search $60,000 / 15.0x.

**The Google Ads line** (`outcomes.google_ads_line`,
`source_attribution_service.google_ads_line`) is the platform's Google Ads
evidence, exactly as `revenue_scope.is_google_ads_attributed` and the truth's
partition define it — no narrower:

* numerator: `reader_row` over the truth's `campaign_attributable` +
  `google_ads_unplaced` members, from
  `canonical_revenue_service.google_ads_evidence_members` (each member placed
  by the truth's own `cwt.attribution_bucket` with the snapshot's campaign
  resolver; the per-bucket counts must equal the truth's partition, carried
  on `load_won_deals` as `attribution_partition`, else unavailable with
  `google_ads_evidence_does_not_reconcile`; no resolver → withheld with
  `campaign_identity_unavailable`);
* what that population is: deals with a Google click ID (GCLID) wherever
  HubSpot put them, **plus** deals the deal sync classified as Paid Search
  (`acquisition_group = google_ads`, from the contact's `hs_analytics_source`),
  **minus** labels approved as not Google Ads. It **still counts a Paid
  Search deal as Google Ads evidence unless its label is approved as not
  Google Ads**, so it does not by itself exclude other search engines (round
  2's probe: g1 GCLID $10k + b2 Paid Search, unmapped label, no GCLID $50k →
  2 deals, $60,000, 15.0x);
* `evidence_split`: deals and revenue with a GCLID vs by the Paid Search
  classification only, from the same members, each inheriting the window's
  verdict, null unless it sums to the line. The UI's sub-line reads e.g.
  "7 deals: 6 with a Google click ID, 1 by HubSpot source only";
* denominator: Google Ads spend for the same settled days, with both period
  ratios and the disclosure (round 1's probe: $30,000 / $4,000 = 7.5x);
* it overlaps the channel rows (d1 is on Direct and on the line), so
  `in_channel_partition: false`, `added_to_totals: false`; customers are
  distinct companies.

Spend days and deal days must be one calendar: the evidence window's deals
are on `analysis.account_time.ACCOUNT_TZ` days (the convention of every
evidence window and of the funnel), the settle rule on the configured account
zone. When the configured zone is known and differs, the spend is
unavailable (`account_time_zone_differs_from_evidence_days`), and the spend
block then reads `google_ads_state: unavailable` with no native figure — any
gate that makes the spend unavailable does the same. Production has
one account on Europe/London, the same as `ACCOUNT_TZ`.

* `revenue_spend_truth_service.build_google_ads_spend_truth_for_days(start,
  end, settled=)` runs `revenue_attribution_service.canonical_campaign_spend_block`
  — the block `_build_from_db` itself now calls — and
  `derive_google_ads_spend_truth`: one state machine for business and
  evidence days, proven equal across every business window on PostgreSQL.
* `settled=True` (Channels) additionally applies
  `google_ads_spend_service.analyze_settled_coverage` to the campaign-spend
  ledger chunks (`fetch_spend_coverage` now returns `fetched_at`): before the
  morning sync yesterday's spend is `spend_not_reread_since_day_ended`.
* A verified zero is a published `0`; its ratios are unavailable
  (`no_google_ads_spend_in_window`).
* Every other row: spend `{value: null, status: unavailable, reason:
  channel_spend_not_connected}` and the same reason passed to `reader_row`,
  so both period ratios are unavailable. The unresolved row:
  `deal_source_not_resolved`. Totals: `all_channel_spend_not_connected`, so
  the totals' ratios are unavailable.
* `spend_sources` names Google Ads (connected), Meta (PR-ADS-171B: whatever
  the Meta line found — `connected` True / False / None, shown on the Meta
  line) and LinkedIn, ChatGPT (not connected: `spend_export_not_supplied`).
* **PR-ADS-171B — Meta.** Meta spend is on its own "Meta Ads (spend)" line
  (`outcomes.meta_ads_line`), from the Meta spend truth over the same settled
  dates, each ad account on its own calendar, and compared with nothing.
  Once Meta is set up the Paid Social row's spend cell is
  `spend_shown_on_meta_ads_line` and its ratios are
  `incompatible_rate(channel_population_is_not_meta_spend_population)`; Meta
  not set up, it stays `channel_spend_not_connected`. Paid Social's funnel
  row gains a platform split from drill-down 1. See
  `docs/55_META_SPEND_ON_CHANNELS.md`.

The ratios are the two period operational ratios (`docs/48` §4) with their
disclosure; certified attribution ratios stay unavailable.

## 6. API and UI

`GET /api/channel-funnel` stays one endpoint: `{**build_channel_funnel(window,
now=now), "outcomes": build_channel_outcomes(window, now=now)}` with one
`now`. If the outcomes raise, the funnel is still returned and `outcomes`
says `channel_outcomes_failed`. `channel_funnel_service.py` stays free of
reader terms.

`outcomes` keys: `source`, `rule_version`, `metric_family`, `window_basis`,
`window {window_type, window_key, start, end_exclusive, start_date, end_date,
account_today, through_reason, disclosure}`, `publication`, `totals`,
`channels`, `partition`, `google_ads_line`, `meta_ads_line` (PR-ADS-171B,
`docs/55`), `spend`, `spend_sources`, `notes`. Never
`company_ids_by_deal`.

`renderChannelFunnel` adds an outcome strip (Closed-won deals, Customers won,
Revenue, Google Ads spend — and, PR-ADS-171B, a Meta Ads spend cell from the
Meta line) via `canonicalMetricHtml`. Once Meta is set up, the Paid Social
row's spend reads `spend_shown_on_meta_ads_line` and its ratios
`channel_population_is_not_meta_spend_population` (`docs/55`); and the outcome and
paid-media columns via `rowMetricHtml` with `{windowType: "evidence",
windowKey}` (admins get View blockers into the workbench). Drill rows carry
no outcome figure; the unresolved row's funnel cells read "n/a — not a
contact source"; the "(no source recorded)" funnel row's outcome cells read
"n/a — counted under Source not resolved". Each half renders when the other
is withheld.

## 7. Audits

* Reader cutover audit: the `source_attribution_service` INVENTORY row names
  the Channels surface; an evidence-window pass for 30d/60d/180d/all_time
  compares `load_won_deals(k, window_type="evidence")` with
  `get_closed_won_truth([("evidence", k)])` and an independent restatement of
  the evidence window (check 17), and runs `channel_outcomes` through checks
  8, 9 and 18 against the settled publication.
* Cross-page parity: `EVIDENCE_WINDOW_CONSUMERS` declares
  `channel_outcomes` (evidence windows are not comparable with a business
  window; not in `METRIC_IDENTITIES`) and the report lists it.
* SQL doctrine registry: the change adds no SQL occurrence (the scanner finds
  none in the new code), so no rule was needed.

## 8. Known gaps

1. Paid Search revenue is HubSpot's original source: it holds labels
   approved as not Google Ads and misses Google Ads deals HubSpot recorded
   elsewhere (GCLID → Direct). Its ratios and spend cell are unavailable;
   the Google Ads line carries the ratio over the platform's Google Ads
   evidence — which still counts Paid Search deals from other engines unless
   their label is approved as not Google Ads. The split shows how much rests
   on a click ID.
2. Closed deals whose contacts were acquired before the funnel window appear
   on channel rows the funnel shows with 0 contacts.
3. The settle rule on campaign spend applies to Channels only; the mart
   (and the Dashboard source breakdowns, `build_revenue_by_source`) still
   judge campaign spend coverage by chunk status (167A known gap 2).
4. ~~Revenue by Source is not removed here.~~ **PR-ADS-168B removed it**
   (`docs/56_REVENUE_BY_SOURCE_REMOVED.md`): the page, `/api/revenue-by-source`,
   `/api/revenue-performance/source-platform-detail` and the mart `source`
   view are deleted, `#/revenue-by-source` lands on this page, and its Source
   Attribution Backfill panel moved to Revenue Health. Channels is now the one
   page for closed-won deals, customers and revenue per channel.
