# Lead Quality Logic
## Every classification rule — explicit, transparent, verifiable

---

## The Principle

Lead quality in Phase 1 is based entirely on HubSpot MQL status. No algorithmic scoring. No machine learning. No inference.

The MDR team has spoken to or reviewed the lead. Their verdict in `mql_status` is more accurate than any pattern we could build. Our job is to aggregate their verdicts and surface the patterns — not replicate their judgment.

---

## Classification Rules

### Confirmed Qualified
**Condition:** `mql_status` is `CLOSED - Sales Qualified` or `CLOSED - Deal Created`
**What it means:** An MDR confirmed this is a real freight forwarder with buying intent. A deal was opened. This is pipeline.
**What it is NOT:** `OPEN - Meeting Booked` is not confirmed qualified. A meeting booked is positive signal, not qualification.

### In Progress
**Condition:** `mql_status` is `OPEN - Meeting Booked` or `OPEN - Pending Meeting`
**What it means:** Genuine engagement. MDR has a meeting. Positive signal but not yet qualified.

### Unknown
**Condition:** `mql_status` is `OPEN - Connecting`
**What it means:** MDR has not yet reached this person. We have no information. Do not assume junk. Do not assume qualified.
**Important:** Within the first 7 days of a contact's creation, all contacts are treated as unknown regardless of other signals. MDRs need time to reach people.

### Confirmed Junk
**Condition:** `mql_status` is `CLOSED - Job Seeker` or `DICARDED`
**What it means:** Zero pipeline value. MDR confirmed this person is not a buyer.
**Signal contamination:** Each junk contact that had a GCLID has already sent Google Ads a false positive. The algorithm is now slightly more calibrated toward finding more people like this. This is why junk rate matters beyond the individual lead.
**Note:** `DICARDED` is spelled with one R. This is how it appears in HubSpot. Preserve exactly.

### Wrong Fit
**Condition:** `mql_status` is `CLOSED - Bad Product Fit` or `CLOSED - Sales Disqualified`
**What it means:** Real person, reached, but wrong company type or size. Less damaging to algorithm signal than pure junk, but still wasted spend.

---

## Junk Rate Calculation

```
Junk rate = Confirmed Junk / (Qualified + In Progress + Junk + Wrong Fit) × 100
```

Contacts with `OPEN - Connecting` are excluded from the denominator. They have no verdict yet. Including them would artificially deflate junk rates for new campaigns — misleading.

**Example:**
```
Campaign: global - competitors
Total contacts: 41
No verdict yet (connecting): 23
Verdicted contacts: 18
  Confirmed qualified: 1
  In progress: 0
  Junk: 6
  Wrong fit: 3
  Unknown (too recent): 8

Junk rate = 6 / 18 = 33%
```

---

## CPQL

```
CPQL = 30-day spend / Confirmed qualified leads
```

**Shown as N/A when zero confirmed SQLs exist.** There is no price at which this campaign has proven it can generate qualified leads. A number would imply otherwise. Show N/A.

**Only calculated with real spend data from Windsor.ai.** If Windsor data is unavailable, note clearly and omit spend column.

---

## Fraud Detection (Supplementary Flags)

These patterns flag contacts for human review. They do NOT automatically classify as junk. An MDR must verify.

**Confirmed fraud patterns from live account:**
- Email contains: `wellsfargo`, `elonmusk`, `applyfunds`, `kisscomp`, `palettarosina`
- Contact name is literally: `Sir/Madam`, `-- Erick`, `CARGO` (all caps generic)
- Email domain contains: `.edu.co`, `.edu.` (student institutions)

---

## Campaigns Needing Immediate Attention (From Live Data)

### `mexico,chile` — 39% estimated junk rate
Ricardo searched "software logistica gratis" (free logistics software). Multiple DICARDED contacts. `mariana.lozanoc@utadeo.edu.co` — confirmed university email. Spanish free-intent is dominating.

### `europe low cpc-new` — 42% estimated junk rate
High proportion of generic Gmail addresses from Eastern Europe/Africa. `zigzagtrans.bg@gmail.com` (Bulgaria, DICARDED), `malikmalikalihassan518@gmail.com` (Romania, DICARDED). High accidental match rate.

### `global - competitors` — 29% estimated junk rate + spend spike
`wellsfargobankc09@gmail.com` (fraud, Nigeria), `elonmusks445588@gmail.com` (fraud, Nigeria). Nigeria is generating high volume with very low quality. The 38% spend spike means the algorithm is accelerating toward this junk.

### `gulf` — Best campaign
Ameer (UAE, meeting booked), Haridas (Oman, Sales Qualified), Malik Khalid (UAE, connecting), Nur (Saudi, connecting). Clean signal. Real freight forwarders.
