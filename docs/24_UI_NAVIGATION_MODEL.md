# 24 — UI Navigation Model

> PR-ADS-069 — Sidebar UX Grouping & Page Rename

---

## 1. Navigation Principles

1. **Group by operator intent** — Pages are organised into sections that answer different operator questions (daily work, evidence, review, admin).
2. **Label clarity** — Visible labels use plain language, not internal system names.
3. **Route stability** — `data-page` route keys are immutable unless a dedicated migration PR updates every reference across frontend and backend.
4. **Admin quieting** — Admin/diagnostic pages are visually de-emphasised but not hidden.
5. **No accordion complexity** — The sidebar is a flat grouped list, not a collapsible tree.

---

## 2. Sidebar Groups

| Group | Purpose | Pages |
|-------|---------|-------|
| **Command Center** | Daily operator pages | Dashboard, Action Queue, Reports |
| **Evidence** | Raw and analytical evidence | Campaigns, Search Term Universe, Search Pattern Analysis, Keyword Performance, Country Performance, Lead Quality, Deals, GCLID Attribution |
| **Review & Quality** | Human-review and exception queues | Flagged Waste Terms, In Progress Leads |
| **Admin** | System operation/diagnostics | Data Runs, System Status, Admin Backfill, Historical Trends |

---

## 3. Page Rename Map

| Old Label | New Label | Route Key (unchanged) |
|-----------|-----------|----------------------|
| Search Terms | Search Term Universe | `search-terms` |
| N-Grams | Search Pattern Analysis | `ngrams` |
| Keywords | Keyword Performance | `keywords` |
| Geo | Country Performance | `geo` |
| Waste Terms | Flagged Waste Terms | `waste` |
| Scheduler | Data Runs | `scheduler` |
| System Health | System Status | `health` |
| Historical Backfill | Admin Backfill | `backfill` |
| Historical Intelligence | Historical Trends | `historical-intelligence` |

---

## 4. Route Stability Rule

> Visible page names may change, but `data-page` route keys must remain stable unless a dedicated migration PR updates every reference.

Route keys are used in:
- `data-page` attributes in HTML
- `PAGE_DATASET_MAP` in `app.js`
- URL hash routing (if ever added)
- Test assertions
- Backend freshness mapping

Changing a route key without updating all references will break navigation, freshness displays, and tests.

---

## 5. Admin vs Operator Pages

**Operator pages** (Command Center, Evidence, Review & Quality):
- Visible to all authenticated users
- Daily-use or evidence pages
- Standard visual weight

**Admin pages** (Admin group):
- `nav-health-item` and `nav-backfill-item` may have visibility logic tied to user role
- Visually quieter (`.nav-item--admin` class)
- System Status and Admin Backfill require admin role in current implementation

---

## 6. Future UX Follow-ups

Planned for subsequent PRs:

- **PR-ADS-070** — Empty State & Page Explanation Upgrade ✅ Complete
  - Every page now has `PAGE_EXPLANATIONS` config, context chips, and contextual empty states
  - See `docs/25_EMPTY_STATE_AND_PAGE_EXPLANATION_MODEL.md` for full pattern documentation
- **PR-ADS-071** — Page Help Panels
- Sidebar collapse/expand toggle for smaller screens
- Page-level breadcrumbs
- Keyboard shortcut navigation (Cmd+K style)
- Badge counts on Action Queue and Review items
- Accordion sections (only if user testing shows benefit)
