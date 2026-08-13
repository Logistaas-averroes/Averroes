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
| **CRM & Revenue** | Canonical CRM funnel plus revenue-truth ROAS, deal attribution, unit economics | Leads, Deals, ROAS by Campaign, ROAS by Country, Revenue by Source, GCLID Attribution, Unit Economics |
| **Admin** | System operation, diagnostics, and configuration | Data Runs, System Status, Admin Backfill, Churn Input |

---

## 3. Why Revenue & Attribution Is Separate

In the prior model, Deals and GCLID Attribution were buried in the generic "Evidence" group. This was incorrect from a doctrine perspective:

- **Deals** are the source of revenue truth — they are the denominator for ROAS and LTV.
- **GCLID Attribution** links platform spend to real revenue outcomes.
- **ROAS by Campaign / Country** shows whether ad spend produces profitable customers.
- **Unit Economics** answers: "Are we buying customers profitably?"

These belong together because they all answer the same question: **Is the revenue reality positive?**

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
| Flagged Waste Terms | `waste` | Platform Evidence *(pending PR-ADS-153D consolidation)* |
| Deals | `deals` | CRM & Revenue |
| ROAS by Campaign | `roas-campaigns` | CRM & Revenue |
| ROAS by Country | `roas-countries` | CRM & Revenue |
| GCLID Attribution | `gclid-attribution` | CRM & Revenue |
| Unit Economics | `unit-economics` | CRM & Revenue |
| Data Runs | `scheduler` | Admin |
| System Status | `health` | Admin |
| Admin Backfill | `backfill` | Admin |
| Churn Input | `churn-input` | Admin |

### Backward-Compatible Route Aliases

| Legacy Route | Resolves To |
|-------------|-------------|
| `ngrams` | `search-terms` (activates Patterns tab) |
| `opportunities` | `leads` with the `open_working` working-status filter (PR-ADS-153C) |

### PR-ADS-153C — Lead Intelligence retirement

The **Lead Intelligence** group is removed. Lead Quality and In Progress Leads are
retired as standalone pages and replaced by ONE canonical **Leads** page under
CRM & Revenue:

- **Lead Quality** kept the `leads` route key, so its old URL lands on the new
  canonical page directly.
- **In Progress Leads** (`opportunities`) redirects to `leads` and carries its
  filter intent — that concept is now the `open_working` operational filter.
- **Flagged Waste Terms** keeps its route, backend and durable evidence, and
  moves under Platform Evidence until PR-ADS-153D consolidates it into Search
  Terms + Action Queue.

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
