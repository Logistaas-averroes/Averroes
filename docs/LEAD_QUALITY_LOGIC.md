# Lead Quality Logic
## How the system classifies leads — explicit rules, no black box

This document defines every rule used to classify a paid search contact's quality.
All rules are deterministic. Every classification can be explained in one sentence.

---

## The Truth Layer

Lead quality classification in Phase 1 is based entirely on HubSpot MQL status.
There is no algorithmic scoring. There is no machine learning. There is no inference.

**Why:** The MDR team has already done the classification. They spoke to or reviewed the lead. Their verdict is more accurate than any pattern-matching algorithm we could build.

Our job is to aggregate their verdicts and surface the patterns.

---

## Classification Rules

### Confirmed Qualified
**Condition:** `mql_status` is one of:
- `CLOSED - Sales Qualified`
- `CLOSED - Deal Created`

**Why these two:** An MDR confirmed this person is a real freight forwarder with genuine buying intent. A deal was opened. This is the only category that represents confirmed pipeline.

**What it is NOT:** `OPEN - Meeting Booked` is NOT confirmed qualified. A meeting booked means interest, not qualification. Include it in "in progress" only.

---

### In Progress — Potential Pipeline
**Condition:** `mql_status` is one of:
- `OPEN - Meeting Booked`
- `OPEN - Pending Meeting`

**Why these two:** An MDR has secured engagement. This is a positive signal but not a confirmed SQL. Do not count as pipeline. Do count as "things are moving."

---

### Unknown — No Verdict Yet
**Condition:** `mql_status` is `OPEN - Connecting`

**Why:** The MDR is still trying to reach this person. No information. Do not assume junk. Do not assume qualified. This is genuinely unknown. In a 30-day window, a significant portion of contacts will be in this state — that is normal.

**Important:** Do not penalise campaigns for having a high "connecting" rate if it's within the first 7 days of the lead's creation. MDRs need time.

---

### Confirmed Junk
**Condition:** `mql_status` is one of:
- `CLOSED - Job Seeker` — Person submitted form looking for employment
- `DICARDED` — No viable lead action possible. Note: one R, not two.

**Signal contamination risk:** Every confirmed junk lead that had a GCLID has already told Google Ads: "this click was a conversion." If there are 10 junk leads from a campaign, Google Ads has received 10 false positive signals. The algorithm is now slightly more calibrated toward finding more people like those 10 junk leads.

---

### Wrong Fit
**Condition:** `mql_status` is one of:
- `CLOSED - Bad Product Fit` — Real person, wrong company type or size
- `CLOSED - Sales Disqualified` — Reached, but does not qualify for Logistaas

**Difference from junk:** These leads were real enough to be reached. The person exists and responded. They're just not the right buyer. This is less damaging to signal quality than pure junk (job seekers, fraud) but still represents wasted spend.

---

## Fraud Detection (Supplementary — Not the Primary Classification)

In addition to MQL status, flag these patterns for human review:

**Name-based fraud flags:**
- Contact name is literally "Sir/Madam" — confirmed bot/scam pattern in live data
- Contact name starts with "--" (e.g., "-- Erick") — unusual, flag for review

**Email-based fraud flags:**
- Email contains "wellsfargo" — confirmed fraud in live data
- Email contains "elonmusk" — confirmed fraud in live data
- Email contains "applyfunds" — confirmed fraud in live data
- Email ends in `.edu.co` or `.edu.` — student domain

**Important:** These are flags for human review. They do NOT automatically classify a contact as junk. An MDR must verify. The system surfaces them; humans decide.

---

## What "Junk Rate" Means in the Campaign Truth Table

```
Junk rate = (Confirmed Junk) / (Total contacts with MQL status verdict) × 100
```

Contacts with `OPEN - Connecting` are excluded from this calculation. They have no verdict yet.

**Why exclude "connecting":** Including them would make every new campaign look like it has a low junk rate, simply because the MDR team hasn't reached them yet. The junk rate should reflect verdicts that have been made, not the absence of verdicts.

**Example:**
```
Campaign: global - competitors
Total contacts: 41
Contacts with verdict: 18 (the other 23 are OPEN - Connecting)
Confirmed junk: 6 (DICARDED + Job Seeker)
Wrong fit: 3
Qualified: 1 (Sales Qualified — Mark Wong, Hong Kong)
Junk rate: 6/18 = 33%
```

---

## CPQL Calculation

```
CPQL = Campaign 30-day spend / Confirmed Qualified leads count
```

**Only calculated when:** At least 1 confirmed qualified lead exists.

**When no qualified leads exist:** CPQL is listed as `N/A`, not zero, not infinity. There is no price at which this campaign has proven it can generate qualified leads. Stating a number would be misleading.

**Spend data source:** Windsor.ai campaign-level spend. If Windsor data is unavailable, note this clearly and omit the spend column.

---

## Data Gap Handling for Lead Quality

**If a campaign has very few contacts (fewer than 5):**
Note: "Insufficient sample size — results not statistically meaningful."

**If all contacts are "OPEN - Connecting" with no verdicts:**
Note: "No MDR verdicts yet for this campaign. Contacts are too recent or MDR outreach is pending. Revisit next week."

**If HubSpot MQL status is missing from contacts:**
Note: "MQL status field not populated for X contacts. Quality breakdown is partial."

---

## The Three Campaigns That Need Immediate Attention (From Live Data)

These are findings from the April 2026 live data audit. These are examples of what the lead quality analysis will surface when it runs:

**`mexico,chile` campaign — 39% junk rate**
Evidence: Ricardo explicitly searched "software logistica gratis" (free logistics software). Multiple DICARDED contacts. Camilo Toro: Bad Product Fit. Multiple `.edu` email domains.
Pattern: Spanish-language free-intent is triggering this campaign heavily.

**`europe low cpc-new` campaign — 42% junk rate**
Evidence: zigzagtrans.bg@gmail.com (DICARDED, Bulgaria), malikmalikalihassan518@gmail.com (DICARDED, Romania), high proportion of generic email addresses, Nigatu (DICARDED, Ethiopia via "europa" campaign — geography mismatch).
Pattern: This campaign is serving ads to people who found it accidentally, not intentionally.

**`global - competitors` campaign — 29% junk rate, spend spike +38%**
Evidence: wellsfargobankc09@gmail.com (fraud, Nigeria, shipsy keyword), elonmusks445588@gmail.com (fraud, Nigeria, gofreight keyword), davidskarosi@gmail.com (DICARDED, Nigeria). High Nigeria concentration with low quality.
Pattern: Nigeria is dominating this campaign's volume with junk signal. The spend spike is making this worse.
