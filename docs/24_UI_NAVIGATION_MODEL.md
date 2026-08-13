# 24 — UI Navigation Model

> PR-ADS-080B — Revenue-First Menu Restructure & ROAS Frontend

---

## 1. Navigation Principles

1. **Revenue truth first** — The menu hierarchy visually prioritizes revenue reality over platform metrics: Revenue truth > lead quality > platform metrics.
2. **Group by operator intent** — Pages are organised into sections that answer different operator questions (daily work, evidence, lead quality, revenue attribution, admin).
3. **Label clarity** — Visible labels use plain language, not internal system names.
4. **Route stability** — `data-page` route keys are immutable unless a dedicated migration PR updates every reference across frontend and backend.
5. **Admin quieting** — Admin/diagnostic pages are visually de-emphasised but not hidden.
6. **No accordion complexity** — The sidebar is a flat grouped list, not a collapsible tree.

---

## 2. Sidebar Groups

| Group | Purpose | Pages |
|-------|---------|-------|
| **Command Center** | Daily operator pages | Dashboard, Action Queue, Reports |
| **Platform Evidence** | Raw ad platform data and analytics (active source: **Google Ads API** — see PR-ADS-105; Windsor is legacy) | Campaigns, Search Terms, Keywords, Countries |
| **CRM & Revenue** | Canonical CRM funnel plus revenue-truth ROAS, deal attribution, unit economics | Leads, Deals, ROAS by Campaign, ROAS by Country, Revenue by Source, Unit Economics |
| **Admin** | System operation, diagnostics, and configuration | Data Runs, System Status, Revenue Health, Admin Backfill, GCLID Attribution, Churn Input |

---

## 3. Why Revenue & Attribution Is Separate

In the prior model, Deals and GCLID Attribution were buried in the generic "Evidence" group. This was incorrect from a doctrine perspective:

- **Deals** are the source of revenue truth — they are the denominator for ROAS and LTV.
- **ROAS by Campaign / Country** shows whether ad spend produces profitable customers.
- **Unit Economics** answers: "Are we buying customers profitably?"

GCLID Attribution was originally placed here for the same reason — it links
platform spend to real revenue outcomes. PR-ADS-153D moved it to Admin: the
LINKAGE is revenue truth, but the page itself is raw click-level forensics used
to diagnose that linkage, not a destination an executive reads. Its output
already reaches CRM & Revenue through ROAS and Revenue by Source.

The remaining pages belong together because they all answer the same question: **Is the revenue reality positive?**

Placing them after Platform Evidence and Lead Intelligence creates a clear reading order:
1. What did the platform do? (Platform Evidence)
2. What leads came in? (Lead Intelligence)
3. Did those leads produce profitable revenue? (Revenue & Attribution)

---

## 4. Page Route Map

| Menu Label | Route Key | Section |
|-----------|-----------|---------|
| Dashboard | `dashboard` | Command Center |
| Action Queue | `action-queue` | Command Center |
| Reports | `reports` | Command Center |
| Campaigns | `campaigns` | Platform Evidence |
| Search Terms | `search-terms` | Platform Evidence |
| Keywords | `keywords` | Platform Evidence |
| Countries | `geo` | Platform Evidence |
| Leads | `leads` | CRM & Revenue |
| Deals | `deals` | CRM & Revenue |
| ROAS by Campaign | `roas-campaigns` | CRM & Revenue |
| ROAS by Country | `roas-countries` | CRM & Revenue |
| Unit Economics | `unit-economics` | CRM & Revenue |
| Data Runs | `scheduler` | Admin |
| System Status | `health` | Admin |
| Admin Backfill | `backfill` | Admin |
| GCLID Attribution | `gclid-attribution` | Admin *(forensics, PR-ADS-153D)* |
| Churn Input | `churn-input` | Admin |

### Backward-Compatible Route Aliases

| Legacy Route | Resolves To |
|-------------|-------------|
| `ngrams` | `search-terms` (activates Patterns tab) |
| `opportunities` | `leads` with the `open_working` working-status filter (PR-ADS-153C) |
| `waste` | `search-terms` (activates Flagged tab) (PR-ADS-153D) |

### PR-ADS-153C — Lead Intelligence retirement

The **Lead Intelligence** group is removed. Lead Quality and In Progress Leads are
retired as standalone pages and replaced by ONE canonical **Leads** page under
CRM & Revenue:

- **Lead Quality** kept the `leads` route key, so its old URL lands on the new
  canonical page directly.
- **In Progress Leads** (`opportunities`) redirects to `leads` and carries its
  filter intent — that concept is now the `open_working` operational filter.
- **Flagged Waste Terms** kept its route, backend and durable evidence, and
  moved under Platform Evidence until PR-ADS-153D consolidated it (below).

### PR-ADS-153D — Search-term waste consolidation

**Flagged Waste Terms** is retired as a standalone destination. Its nav item,
page markup and loader are deleted; `#/waste` redirects to
`#/search-terms?tab=flagged`, carrying the old page's intent rather than merely
its URL. Investigation lives in Search Terms → Flagged; the actionable subset
lives in the Action Queue.

**N-Grams / Patterns** has one normal home — Search Terms → Patterns. `#/ngrams`
already redirected there and still does; there is no everyday nav item for it.

**GCLID Attribution** moves from CRM & Revenue to **Admin**. The PR-ADS-153A
audit classified it as a forensic/diagnostic capability, not an everyday
executive destination — the everyday CRM & Revenue section should not require
users to inspect raw click-level attribution. Its route key, backend and
attribution logic are unchanged; this is a navigation/ownership correction only.

Full contract: `docs/34_SEARCH_TERM_WASTE_CONSOLIDATION.md`.

---

## 5. Route Stability Rule

> Visible page names may change, but `data-page` route keys must remain stable unless a dedicated migration PR updates every reference.

Route keys are used in:
- `data-page` attributes in HTML
- `PAGE_DATASET_MAP` in `app.js`
- URL hash routing (if ever added)
- Test assertions
- Backend freshness mapping

Changing a route key without updating all references will break navigation, freshness displays, and tests.

---

## 6. Search Terms Tab Model

The "Search Terms" menu item contains two tabs within a single page:

- **Tab 1: Search Terms** — Shows the search term universe table (route key: `search-terms`).
- **Tab 2: Patterns** — Shows the search pattern analysis (formerly the `ngrams` page).

The `ngrams` route alias still works — it navigates to `search-terms` and auto-activates the Patterns tab.

---

## 7. Admin vs Operator Pages

**Operator pages** (Command Center, Platform Evidence, Lead Intelligence, Revenue & Attribution):
- Visible to all authenticated users
- Daily-use, evidence, or revenue-analysis pages
- Standard visual weight

**Admin pages** (Admin group):
- `nav-health-item` and `nav-backfill-item` may have visibility logic tied to user role
- Churn Input requires admin role
- Visually quieter (`.nav-item--admin` class)
- System Status and Admin Backfill require admin role in current implementation

---

## 8. Future UX Follow-ups

Planned for subsequent PRs:

- **PR-ADS-070** — Empty State & Page Explanation Upgrade ✅ Complete
- **PR-ADS-080C** — GCLID Attribution UI, full attribution pipeline frontend
- **PR-ADS-071** — Page Help Panels
- Sidebar collapse/expand toggle for smaller screens
- Page-level breadcrumbs
- Keyboard shortcut navigation (Cmd+K style)
- Badge counts on Action Queue and Review items
